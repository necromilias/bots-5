"""Add append-only conversation lineage and active branch state."""

from collections import defaultdict
import json

from alembic import op
import sqlalchemy as sa

from bots5.domain.clock import parse_utc
from bots5.domain.models import AttemptState, MessageRole, MessageState

revision = "0002_conversation_lineage"
down_revision = "0001_desktop_state"
branch_labels = None
depends_on = None


def _fail(message: str) -> None:
    raise RuntimeError(f"cannot backfill conversation lineage: {message}")


def _validate_foreign_keys(connection) -> None:
    violation = connection.execute(sa.text("PRAGMA foreign_key_check")).first()
    if violation is not None:
        _fail(
            "legacy database has a foreign-key violation: "
            f"table={violation[0]} rowid={violation[1]} "
            f"parent={violation[2]} foreign_key={violation[3]}"
        )


def _validate_legacy_lineage(connection) -> dict[str, list[dict[str, object]]]:
    _validate_foreign_keys(connection)
    chat_rows = connection.execute(
        sa.text("SELECT id, created_at, updated_at FROM chats")
    ).mappings().all()
    for row in chat_rows:
        for field in ("created_at", "updated_at"):
            try:
                parse_utc(row[field])
            except (AttributeError, TypeError, ValueError) as exc:
                _fail(f"chat {row['id']} has invalid {field}")

    message_rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, parent_id, sequence, role, state, created_at "
            "FROM messages ORDER BY chat_id, sequence, id"
        )
    ).mappings().all()
    by_chat: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in message_rows:
        by_chat[str(row["chat_id"])].append(dict(row))

    for rows in by_chat.values():
        previous_id: str | None = None
        for row in rows:
            if type(row["sequence"]) is not int:
                _fail(f"message {row['id']} has an invalid sequence")
            try:
                sequence = int(row["sequence"])
            except (TypeError, ValueError) as exc:
                _fail(f"message {row['id']} has an invalid sequence")
            if sequence < 1:
                _fail(f"message {row['id']} has invalid sequence {row['sequence']}")
            if row["role"] not in {"user", "assistant"}:
                _fail(f"message {row['id']} has invalid role {row['role']}")
            if row["state"] not in {state.value for state in MessageState}:
                _fail(f"message {row['id']} has invalid state {row['state']}")
            if row["role"] == MessageRole.USER.value and row["state"] not in {
                MessageState.SENDING.value,
                MessageState.SENT.value,
                MessageState.FAILED.value,
                MessageState.ABORTED.value,
            }:
                _fail(f"user message {row['id']} has invalid state {row['state']}")
            if row["role"] == MessageRole.ASSISTANT.value and row["state"] not in {
                MessageState.STREAMING.value,
                MessageState.COMPLETE.value,
                MessageState.INCOMPLETE.value,
                MessageState.TRUNCATED.value,
                MessageState.FAILED.value,
                MessageState.ABORTED.value,
            }:
                _fail(f"assistant message {row['id']} has invalid state {row['state']}")
            try:
                parse_utc(row["created_at"])
            except (AttributeError, TypeError, ValueError) as exc:
                _fail(f"message {row['id']} has an invalid created_at timestamp")
            parent_id = row["parent_id"]
            if previous_id is None:
                if parent_id is not None:
                    _fail(f"root message {row['id']} has parent {parent_id}")
            elif parent_id not in (None, previous_id):
                _fail(
                    f"message {row['id']} points to {parent_id}, "
                    f"expected {previous_id}"
                )
            previous_id = str(row["id"])

    attempt_rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, user_message_id, assistant_message_id, state, "
            "request_snapshot, started_at, ended_at FROM generation_attempts"
        )
    ).mappings().all()
    message_by_id = {
        str(row["id"]): row
        for row in message_rows
    }
    terminal_attempt_states = {
        AttemptState.COMPLETE.value,
        AttemptState.INCOMPLETE.value,
        AttemptState.FAILED.value,
        AttemptState.ABORTED.value,
    }
    for row in attempt_rows:
        if row["state"] not in {state.value for state in AttemptState}:
            _fail(f"generation attempt {row['id']} has invalid state {row['state']}")
        try:
            parse_utc(row["started_at"])
            if row["ended_at"] is not None:
                parse_utc(row["ended_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"generation attempt {row['id']} has an invalid timestamp")
        if row["state"] == AttemptState.RUNNING.value and row["ended_at"] is not None:
            _fail(f"running generation attempt {row['id']} has an end time")
        if row["state"] in terminal_attempt_states and row["ended_at"] is None:
            _fail(f"terminal generation attempt {row['id']} has no end time")
        try:
            snapshot = json.loads(row["request_snapshot"])
        except (TypeError, json.JSONDecodeError) as exc:
            _fail(f"generation attempt {row['id']} has invalid request JSON")
        if not isinstance(snapshot, dict):
            _fail(f"generation attempt {row['id']} request snapshot is not an object")
        user = message_by_id.get(str(row["user_message_id"]))
        assistant = message_by_id.get(str(row["assistant_message_id"]))
        if (
            user is None
            or assistant is None
            or user["chat_id"] != row["chat_id"]
            or assistant["chat_id"] != row["chat_id"]
            or user["role"] != MessageRole.USER.value
            or assistant["role"] != MessageRole.ASSISTANT.value
            or assistant["parent_id"] != user["id"]
        ):
            _fail(f"generation attempt {row['id']} has invalid message references")

    invalid_attempt = connection.execute(
        sa.text(
            "SELECT a.id FROM generation_attempts AS a "
            "LEFT JOIN messages AS u ON u.id = a.user_message_id "
            "LEFT JOIN messages AS s ON s.id = a.assistant_message_id "
            "WHERE u.id IS NULL OR s.id IS NULL "
            "OR u.chat_id <> a.chat_id OR s.chat_id <> a.chat_id "
            "OR u.role <> 'user' OR s.role <> 'assistant'"
        )
    ).first()
    if invalid_attempt is not None:
        _fail(f"generation attempt {invalid_attempt[0]} has invalid message references")
    duplicate_attempt = connection.execute(
        sa.text(
            "SELECT assistant_message_id FROM generation_attempts "
            "GROUP BY assistant_message_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_attempt is not None:
        _fail(
            "assistant message has multiple generation attempts: "
            f"{duplicate_attempt[0]}"
        )
    return by_chat


def upgrade() -> None:
    connection = op.get_bind()
    by_chat = _validate_legacy_lineage(connection)

    with op.batch_alter_table("chats") as batch:
        batch.add_column(sa.Column("head_message_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=True))

    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("lineage_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("supersedes_id", sa.String(length=64), nullable=True))

    connection.execute(
        sa.text("UPDATE messages SET lineage_id = id, revision = 1")
    )

    for chat_id, rows in by_chat.items():
        if not rows:
            continue
        previous_id: str | None = None
        for row in rows:
            parent_id = row["parent_id"]
            if previous_id is None:
                if parent_id is not None:
                    _fail(f"root message {row['id']} has parent {parent_id}")
            elif parent_id not in (None, previous_id):
                _fail(
                    f"message {row['id']} points to {parent_id}, "
                    f"expected {previous_id}"
                )
            previous_id = str(row["id"])

        head_id = rows[-1]["id"]
        chat_revision = int(rows[-1]["sequence"])
        connection.execute(
            sa.text(
                "UPDATE chats SET head_message_id = :head_message_id, "
                "revision = :revision WHERE id = :chat_id"
            ),
            {
                "head_message_id": head_id,
                "revision": chat_revision,
                "chat_id": chat_id,
            },
        )

    connection.execute(
        sa.text("UPDATE chats SET revision = 0 WHERE revision IS NULL")
    )

    with op.batch_alter_table("messages", recreate="always") as batch:
        batch.alter_column("lineage_id", nullable=False)
        batch.alter_column("revision", nullable=False)
        batch.create_check_constraint(
            "ck_messages_revision_positive",
            "revision > 0",
        )
        batch.create_foreign_key(
            "fk_messages_supersedes",
            "messages",
            ["supersedes_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("chats", recreate="always") as batch:
        batch.alter_column("revision", nullable=False)
        batch.create_check_constraint("ck_chats_revision_nonnegative", "revision >= 0")
        batch.create_foreign_key(
            "fk_chats_head_message",
            "messages",
            ["head_message_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Reapply the derived values after the final table exists, keeping the
    # backfill correct even if a SQLite batch-copy omits populated values.
    for chat_id, rows in by_chat.items():
        previous_id: str | None = None
        for row in rows:
            if previous_id is not None and row["parent_id"] is None:
                connection.execute(
                    sa.text("UPDATE messages SET parent_id = :parent_id WHERE id = :id"),
                    {"parent_id": previous_id, "id": row["id"]},
                )
            previous_id = str(row["id"])
        connection.execute(
            sa.text(
                "UPDATE chats SET head_message_id = :head_message_id, "
                "revision = :revision WHERE id = :chat_id"
            ),
            {
                "head_message_id": rows[-1]["id"],
                "revision": int(rows[-1]["sequence"]),
                "chat_id": chat_id,
            },
        )

    _validate_foreign_keys(connection)

    op.create_index(
        "ix_messages_chat_lineage_revision",
        "messages",
        ["chat_id", "lineage_id", "revision"],
        unique=True,
    )
    op.create_index(
        "ix_messages_chat_parent_sequence",
        "messages",
        ["chat_id", "parent_id", "sequence"],
    )
    op.create_index(
        "ux_generation_attempts_assistant",
        "generation_attempts",
        ["assistant_message_id"],
        unique=True,
    )

    op.execute(
        sa.text(
            "CREATE TRIGGER messages_immutable_identity "
            "BEFORE UPDATE OF id, chat_id, parent_id, sequence, role, created_at, "
            "lineage_id, revision, supersedes_id ON messages "
            "BEGIN SELECT RAISE(ABORT, 'message lineage identity is immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER messages_terminal_immutable "
            "BEFORE UPDATE OF content, state ON messages "
            "WHEN OLD.state <> 'streaming' "
            "BEGIN SELECT RAISE(ABORT, 'terminal message is immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_snapshot_immutable "
            "BEFORE UPDATE OF id, chat_id, user_message_id, assistant_message_id, "
            "backend_id, model, request_snapshot, started_at ON generation_attempts "
            "BEGIN SELECT RAISE(ABORT, 'generation request snapshot is immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER messages_parent_same_chat "
            "BEFORE INSERT ON messages "
            "WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.parent_id AND chat_id = NEW.chat_id) "
            "BEGIN SELECT RAISE(ABORT, 'message parent must be in the same chat'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER messages_supersedes_same_chat "
            "BEFORE INSERT ON messages "
            "WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.supersedes_id AND chat_id = NEW.chat_id) "
            "BEGIN SELECT RAISE(ABORT, 'superseded message must be in the same chat'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER chats_head_same_chat "
            "BEFORE INSERT ON chats "
            "WHEN NEW.head_message_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.head_message_id AND chat_id = NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'chat head must be in the same chat'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER chats_head_same_chat_update "
            "BEFORE UPDATE OF head_message_id ON chats "
            "WHEN NEW.head_message_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.head_message_id AND chat_id = NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'chat head must be in the same chat'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_messages_same_chat "
            "BEFORE INSERT ON generation_attempts "
            "WHEN NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.user_message_id AND chat_id = NEW.chat_id) "
            "OR NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.assistant_message_id AND chat_id = NEW.chat_id) "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt messages must be in the same chat'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER messages_revision_consistency "
            "BEFORE INSERT ON messages "
            "WHEN length(NEW.lineage_id) = 0 "
            "OR (NEW.supersedes_id IS NULL AND NEW.revision <> 1) "
            "OR (NEW.supersedes_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages AS old WHERE old.id = NEW.supersedes_id "
            "AND old.chat_id = NEW.chat_id AND old.lineage_id = NEW.lineage_id "
            "AND old.revision + 1 = NEW.revision AND old.role = NEW.role)) "
            "BEGIN SELECT RAISE(ABORT, 'message revision lineage is inconsistent'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_roles "
            "BEFORE INSERT ON generation_attempts "
            "WHEN NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.user_message_id AND role = 'user') "
            "OR NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.assistant_message_id AND role = 'assistant') "
            "OR NEW.user_message_id = NEW.assistant_message_id "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt message roles are invalid'); END"
        )
    )


def downgrade() -> None:
    raise RuntimeError("conversation lineage cannot be downgraded without data loss")
