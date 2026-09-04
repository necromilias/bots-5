"""Enforce Phase 2 conversation and lifecycle integrity at the SQLite boundary."""

import json

from alembic import op
import sqlalchemy as sa

from bots5.domain.clock import parse_utc
from bots5.domain.models import AttemptState, MessageRole, MessageState


revision = "0003_integrity_boundaries"
down_revision = "0002_conversation_lineage"
branch_labels = None
depends_on = None


_MESSAGE_TERMINAL_STATES = {
    MessageState.COMPLETE.value,
    MessageState.INCOMPLETE.value,
    MessageState.TRUNCATED.value,
    MessageState.FAILED.value,
    MessageState.ABORTED.value,
}
_ATTEMPT_TERMINAL_STATES = {
    AttemptState.COMPLETE.value,
    AttemptState.INCOMPLETE.value,
    AttemptState.FAILED.value,
    AttemptState.ABORTED.value,
}


def _fail(message: str) -> None:
    raise RuntimeError(f"cannot establish conversation integrity boundary: {message}")


def _snapshot_is_consistent(snapshot_text: object, expected: dict[str, str]) -> bool:
    try:
        snapshot = json.loads(snapshot_text)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(snapshot, dict):
        return False
    return all(key not in snapshot or snapshot[key] == value for key, value in expected.items())


def _validate_existing_state(connection) -> None:
    chats = {
        str(row["id"]): dict(row)
        for row in connection.execute(
            sa.text("SELECT id, revision, head_message_id, created_at, updated_at FROM chats")
        ).mappings()
    }
    for chat_id, row in chats.items():
        try:
            if type(row["revision"]) is not int:
                raise ValueError("revision is not an integer")
            revision = int(row["revision"])
            parse_utc(row["created_at"])
            parse_utc(row["updated_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"chat {chat_id} has invalid revision or timestamp")
        if revision < 0:
            _fail(f"chat {chat_id} has negative revision")

    message_rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, parent_id, sequence, role, state, created_at, "
            "lineage_id, revision, supersedes_id FROM messages"
        )
    ).mappings().all()
    messages = {str(row["id"]): dict(row) for row in message_rows}
    for message_id, row in messages.items():
        try:
            if type(row["sequence"]) is not int or type(row["revision"]) is not int:
                raise ValueError("sequence or revision is not an integer")
            sequence = int(row["sequence"])
            revision = int(row["revision"])
            parse_utc(row["created_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"message {message_id} has invalid sequence, revision, or timestamp")
        if sequence < 1 or revision < 1:
            _fail(f"message {message_id} has a nonpositive sequence or revision")
        if row["role"] not in {role.value for role in MessageRole}:
            _fail(f"message {message_id} has invalid role {row['role']}")
        if row["state"] not in {state.value for state in MessageState}:
            _fail(f"message {message_id} has invalid state {row['state']}")
        if not row["lineage_id"]:
            _fail(f"message {message_id} has an empty lineage")
        chat = chats.get(str(row["chat_id"]))
        if chat is None:
            _fail(f"message {message_id} references a missing chat")
        if row["parent_id"] is not None:
            if str(row["parent_id"]) == message_id:
                _fail(f"message {message_id} cannot parent itself")
            parent = messages.get(str(row["parent_id"]))
            if parent is None or parent["chat_id"] != row["chat_id"]:
                _fail(f"message {message_id} has a cross-chat parent")
        if row["supersedes_id"] is None:
            if revision != 1:
                _fail(f"message {message_id} has a root revision other than 1")
        else:
            old = messages.get(str(row["supersedes_id"]))
            if (
                old is None
                or old["chat_id"] != row["chat_id"]
                or old["lineage_id"] != row["lineage_id"]
                or int(old["revision"]) + 1 != revision
                or old["role"] != row["role"]
            ):
                _fail(f"message {message_id} has an inconsistent superseded revision")
        if row["role"] == MessageRole.USER.value and row["state"] not in {
            MessageState.SENDING.value,
            MessageState.SENT.value,
            MessageState.FAILED.value,
            MessageState.ABORTED.value,
        }:
            _fail(f"user message {message_id} has invalid state {row['state']}")
        if row["role"] == MessageRole.ASSISTANT.value and row["state"] not in {
            MessageState.STREAMING.value,
            MessageState.COMPLETE.value,
            MessageState.INCOMPLETE.value,
            MessageState.TRUNCATED.value,
            MessageState.FAILED.value,
            MessageState.ABORTED.value,
        }:
            _fail(f"assistant message {message_id} has invalid state {row['state']}")

    for message_id in messages:
        seen: set[str] = set()
        current_id: str | None = message_id
        while current_id is not None:
            if current_id in seen:
                _fail(f"message lineage contains a parent cycle at {current_id}")
            seen.add(current_id)
            parent_id = messages[current_id]["parent_id"]
            current_id = None if parent_id is None else str(parent_id)

    for chat_id, row in chats.items():
        head_id = row["head_message_id"]
        chat_messages = [message for message in messages.values() if message["chat_id"] == chat_id]
        if head_id is None:
            if row["revision"] != 0:
                _fail(f"chat {chat_id} has a nonzero revision without an active head")
            if chat_messages:
                _fail(f"chat {chat_id} has messages but no active head")
        elif row["revision"] < 1:
            _fail(f"chat {chat_id} has an active head with zero revision")
        elif not chat_messages:
            _fail(f"chat {chat_id} has an active head but no messages")
        if head_id is not None and (
            str(head_id) not in messages or messages[str(head_id)]["chat_id"] != chat_id
        ):
            _fail(f"chat {chat_id} has a cross-chat or missing head")
        if head_id is not None:
            head = messages[str(head_id)]
            if head["role"] != MessageRole.ASSISTANT.value:
                _fail(f"chat {chat_id} has a non-assistant active head")
            if any(message["parent_id"] == head_id for message in messages.values()):
                _fail(f"chat {chat_id} active head has child messages")

    attempt_rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
            "state, request_snapshot, started_at, ended_at FROM generation_attempts"
        )
    ).mappings().all()
    attempt_by_assistant: dict[str, dict[str, object]] = {}
    for row in attempt_rows:
        attempt_id = str(row["id"])
        if row["state"] not in {state.value for state in AttemptState}:
            _fail(f"generation attempt {attempt_id} has invalid state {row['state']}")
        try:
            parse_utc(row["started_at"])
            if row["ended_at"] is not None:
                parse_utc(row["ended_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"generation attempt {attempt_id} has an invalid timestamp")
        if row["state"] == AttemptState.RUNNING.value and row["ended_at"] is not None:
            _fail(f"running generation attempt {attempt_id} has an end time")
        if row["state"] in _ATTEMPT_TERMINAL_STATES and row["ended_at"] is None:
            _fail(f"terminal generation attempt {attempt_id} has no end time")
        expected = {
            "attempt_id": attempt_id,
            "chat_id": str(row["chat_id"]),
            "user_message_id": str(row["user_message_id"]),
            "backend_id": str(row["backend_id"]),
            "model": str(row["model"]),
        }
        if not _snapshot_is_consistent(row["request_snapshot"], expected):
            _fail(f"generation attempt {attempt_id} has an invalid request snapshot")
        user = messages.get(str(row["user_message_id"]))
        assistant = messages.get(str(row["assistant_message_id"]))
        if (
            user is None
            or assistant is None
            or user["chat_id"] != row["chat_id"]
            or assistant["chat_id"] != row["chat_id"]
            or user["role"] != MessageRole.USER.value
            or assistant["role"] != MessageRole.ASSISTANT.value
            or assistant["parent_id"] != user["id"]
        ):
            _fail(f"generation attempt {attempt_id} has an invalid user/assistant turn")
        assistant_id = str(row["assistant_message_id"])
        if assistant_id in attempt_by_assistant:
            _fail(f"assistant message {assistant_id} has multiple attempts")
        attempt_by_assistant[assistant_id] = dict(row)
        if row["state"] == AttemptState.RUNNING.value and assistant["state"] != MessageState.STREAMING.value:
            _fail(f"running attempt {attempt_id} does not have a streaming assistant")
        if row["state"] == AttemptState.COMPLETE.value and assistant["state"] != MessageState.COMPLETE.value:
            _fail(f"complete attempt {attempt_id} does not have a complete assistant")
        if row["state"] == AttemptState.INCOMPLETE.value and assistant["state"] not in {
            MessageState.INCOMPLETE.value,
            MessageState.TRUNCATED.value,
        }:
            _fail(f"incomplete attempt {attempt_id} has an invalid assistant state")
        if row["state"] == AttemptState.FAILED.value and assistant["state"] != MessageState.FAILED.value:
            _fail(f"failed attempt {attempt_id} does not have a failed assistant")
        if row["state"] == AttemptState.ABORTED.value and assistant["state"] != MessageState.ABORTED.value:
            _fail(f"aborted attempt {attempt_id} does not have an aborted assistant")

    for message_id, row in messages.items():
        if row["role"] == MessageRole.ASSISTANT.value and message_id not in attempt_by_assistant:
            _fail(f"assistant message {message_id} has no generation attempt")


_TRIGGERS = (
    "messages_immutable_identity",
    "messages_terminal_immutable",
    "generation_attempt_snapshot_immutable",
    "messages_parent_same_chat",
    "messages_supersedes_same_chat",
    "chats_head_same_chat",
    "chats_head_same_chat_update",
    "generation_attempt_messages_same_chat",
    "messages_revision_consistency",
    "generation_attempt_roles",
    "messages_validate_insert",
    "messages_validate_update",
    "generation_attempt_validate_insert",
    "generation_attempt_validate_update",
    "chats_validate_insert",
    "chats_revision_monotonic",
)


def _drop_triggers(connection) -> None:
    for trigger in _TRIGGERS:
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))


def _create_triggers(connection) -> None:
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_immutable_identity "
            "BEFORE UPDATE OF id, chat_id, parent_id, sequence, role, created_at, "
            "lineage_id, revision, supersedes_id ON messages "
            "BEGIN SELECT RAISE(ABORT, 'message lineage identity is immutable'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_terminal_immutable "
            "BEFORE UPDATE OF content, state ON messages "
            "WHEN OLD.state <> 'streaming' "
            "BEGIN SELECT RAISE(ABORT, 'terminal message is immutable'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_snapshot_immutable "
            "BEFORE UPDATE OF id, chat_id, user_message_id, assistant_message_id, "
            "backend_id, model, request_snapshot, started_at ON generation_attempts "
            "BEGIN SELECT RAISE(ABORT, 'generation request snapshot is immutable'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_parent_same_chat BEFORE INSERT ON messages "
            "WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.parent_id AND chat_id = NEW.chat_id) "
            "BEGIN SELECT RAISE(ABORT, 'message parent must be in the same chat'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_supersedes_same_chat BEFORE INSERT ON messages "
            "WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.supersedes_id AND chat_id = NEW.chat_id) "
            "BEGIN SELECT RAISE(ABORT, 'superseded message must be in the same chat'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_head_same_chat BEFORE INSERT ON chats "
            "WHEN NEW.head_message_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.head_message_id AND chat_id = NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'chat head must be in the same chat'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_head_same_chat_update BEFORE UPDATE OF head_message_id ON chats "
            "WHEN NEW.head_message_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.head_message_id AND chat_id = NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'chat head must be in the same chat'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_messages_same_chat "
            "BEFORE INSERT ON generation_attempts "
            "WHEN NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.user_message_id AND chat_id = NEW.chat_id) "
            "OR NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.assistant_message_id AND chat_id = NEW.chat_id) "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt messages must be in the same chat'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_revision_consistency BEFORE INSERT ON messages "
            "WHEN NEW.lineage_id IS NULL OR length(NEW.lineage_id) = 0 "
            "OR (NEW.supersedes_id IS NULL AND NEW.revision <> 1) "
            "OR (NEW.supersedes_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM messages AS old WHERE old.id = NEW.supersedes_id "
            "AND old.chat_id = NEW.chat_id AND old.lineage_id = NEW.lineage_id "
            "AND old.revision + 1 = NEW.revision AND old.role = NEW.role)) "
            "BEGIN SELECT RAISE(ABORT, 'message revision lineage is inconsistent'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_roles BEFORE INSERT ON generation_attempts "
            "WHEN NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.user_message_id AND role = 'user') "
            "OR NOT EXISTS (SELECT 1 FROM messages WHERE id = NEW.assistant_message_id AND role = 'assistant') "
            "OR NEW.user_message_id = NEW.assistant_message_id "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt message roles are invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages "
            "WHEN NEW.sequence < 1 "
            "OR NEW.role NOT IN ('user', 'assistant') "
            "OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted') "
            "OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted')) "
            "OR (NEW.role = 'assistant' AND NEW.state NOT IN ('streaming', 'complete', 'incomplete', 'truncated', 'failed', 'aborted')) "
            "OR NEW.revision < 1 "
            "BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_validate_update BEFORE UPDATE OF state, content ON messages "
            "WHEN NEW.role NOT IN ('user', 'assistant') "
            "OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted') "
            "OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted')) "
            "OR (NEW.role = 'assistant' AND NEW.state NOT IN ('streaming', 'complete', 'incomplete', 'truncated', 'failed', 'aborted')) "
            "OR (OLD.state = 'streaming' AND NEW.state NOT IN ('streaming', 'complete', 'incomplete', 'truncated', 'failed', 'aborted')) "
            "OR (OLD.state = 'streaming' AND NEW.state <> 'streaming' "
            "AND NOT EXISTS (SELECT 1 FROM lifecycle_transitions WHERE message_id = NEW.id)) "
            "OR (OLD.state = 'streaming' AND NEW.state = 'complete' AND NOT EXISTS ("
            "SELECT 1 FROM generation_attempts WHERE assistant_message_id = NEW.id AND state = 'complete')) "
            "OR (OLD.state = 'streaming' AND NEW.state IN ('incomplete', 'truncated') AND NOT EXISTS ("
            "SELECT 1 FROM generation_attempts WHERE assistant_message_id = NEW.id AND state = 'incomplete')) "
            "OR (OLD.state = 'streaming' AND NEW.state = 'failed' AND NOT EXISTS ("
            "SELECT 1 FROM generation_attempts WHERE assistant_message_id = NEW.id AND state = 'failed')) "
            "OR (OLD.state = 'streaming' AND NEW.state = 'aborted' AND NOT EXISTS ("
            "SELECT 1 FROM generation_attempts WHERE assistant_message_id = NEW.id AND state = 'aborted')) "
            "BEGIN SELECT RAISE(ABORT, 'message lifecycle transition is invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_validate_insert BEFORE INSERT ON generation_attempts "
            "WHEN NEW.state NOT IN ('running', 'complete', 'incomplete', 'failed', 'aborted') "
            "OR (NEW.state = 'running' AND NEW.ended_at IS NOT NULL) "
            "OR (NEW.state <> 'running' AND NEW.ended_at IS NULL) "
            "OR json_valid(NEW.request_snapshot) = 0 "
            "OR (json_type(NEW.request_snapshot, '$.attempt_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.attempt_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.attempt_id') <> NEW.id)) "
            "OR (json_type(NEW.request_snapshot, '$.chat_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.chat_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.chat_id') <> NEW.chat_id)) "
            "OR (json_type(NEW.request_snapshot, '$.user_message_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.user_message_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.user_message_id') <> NEW.user_message_id)) "
            "OR (json_type(NEW.request_snapshot, '$.backend_id') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.backend_id') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.backend_id') <> NEW.backend_id)) "
            "OR (json_type(NEW.request_snapshot, '$.model') = 'null' OR "
            "(json_type(NEW.request_snapshot, '$.model') IS NOT NULL AND json_extract(NEW.request_snapshot, '$.model') <> NEW.model)) "
            "OR NOT EXISTS (SELECT 1 FROM messages AS u JOIN messages AS a ON a.id = NEW.assistant_message_id "
            "WHERE u.id = NEW.user_message_id AND u.chat_id = NEW.chat_id AND a.chat_id = NEW.chat_id "
            "AND u.role = 'user' AND a.role = 'assistant' AND a.parent_id = u.id) "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt fields are invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempt_validate_update BEFORE UPDATE OF state, ended_at, error_type, error_message ON generation_attempts "
            "WHEN NEW.state NOT IN ('running', 'complete', 'incomplete', 'failed', 'aborted') "
            "OR (NEW.state = 'running' AND NEW.ended_at IS NOT NULL) "
            "OR (NEW.state <> 'running' AND NEW.ended_at IS NULL) "
            "OR (OLD.state <> 'running' AND (NEW.state <> OLD.state OR NEW.ended_at IS NOT OLD.ended_at "
            "OR NEW.error_type IS NOT OLD.error_type OR NEW.error_message IS NOT OLD.error_message)) "
            "OR (OLD.state = 'running' AND NEW.state <> 'running' "
            "AND NOT EXISTS (SELECT 1 FROM lifecycle_transitions WHERE attempt_id = NEW.id)) "
            "OR (OLD.state = 'running' AND NEW.state <> 'running' AND NOT EXISTS ("
            "SELECT 1 FROM messages WHERE id = NEW.assistant_message_id AND role = 'assistant' "
            "AND parent_id = NEW.user_message_id AND state = 'streaming')) "
            "OR (NEW.state = 'running' AND OLD.state <> 'running') "
            "BEGIN SELECT RAISE(ABORT, 'generation attempt lifecycle transition is invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_validate_insert BEFORE INSERT ON chats "
            "WHEN NEW.revision < 0 "
            "BEGIN SELECT RAISE(ABORT, 'chat revision is invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_revision_monotonic BEFORE UPDATE OF revision ON chats "
            "WHEN NEW.revision < OLD.revision OR NEW.revision > OLD.revision + 1 "
            "BEGIN SELECT RAISE(ABORT, 'chat revision must advance monotonically by one'); END"
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    _validate_existing_state(connection)
    _drop_triggers(connection)
    op.create_table(
        "lifecycle_transitions",
        sa.Column("message_id", sa.String(length=64), primary_key=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
    )
    _create_triggers(connection)


def downgrade() -> None:
    raise RuntimeError("conversation integrity boundaries cannot be downgraded without data loss")
