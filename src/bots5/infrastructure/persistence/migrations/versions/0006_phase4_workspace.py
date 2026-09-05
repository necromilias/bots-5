"""Add bounded Phase 4 workspace state and the active-chat invariant."""

from collections import defaultdict
from datetime import UTC, datetime
from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "0006_phase4_workspace"
down_revision = "0005_generation_outcomes"
branch_labels = None
depends_on = None


def _reconcile_duplicate_active_attempts(connection) -> None:
    """Retire legacy same-chat concurrency before adding the Phase 4 index.

    Phase 3 permitted more than one running attempt in a chat.  Keep the
    deterministically newest attempt running so the existing application
    restart reconciliation can handle it, and terminalize older attempts in
    the same chat without deleting their messages or partial output.
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, assistant_message_id, remote_outcome_unknown "
            "FROM generation_attempts "
            "WHERE state = 'running' "
            "ORDER BY chat_id, started_at, id"
        )
    ).mappings().all()
    by_chat: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_chat[str(row["chat_id"])].append(dict(row))
    retired = [row for attempts in by_chat.values() if len(attempts) > 1 for row in attempts[:-1]]
    if not retired:
        return

    guard = import_module(
        "bots5.infrastructure.persistence.migrations.versions.0004_integrity_guard_function"
    )
    outcomes = import_module(
        "bots5.infrastructure.persistence.migrations.versions.0005_generation_outcomes"
    )
    guard._drop_guard_triggers(connection)
    ended_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        for row in retired:
            attempt_result = connection.execute(
                sa.text(
                    "UPDATE generation_attempts SET state = 'aborted', "
                    "ended_at = :ended_at, error_type = 'aborted', "
                    "error_message = 'generation was interrupted before application restart', "
                    "remote_outcome_unknown = COALESCE(remote_outcome_unknown, 1) "
                    "WHERE id = :attempt_id AND state = 'running'"
                ),
                {"ended_at": ended_at, "attempt_id": row["id"]},
            )
            if attempt_result.rowcount != 1:
                raise RuntimeError(
                    f"cannot reconcile historical generation attempt: {row['id']}"
                )
            message_result = connection.execute(
                sa.text(
                    "UPDATE messages SET state = 'aborted' "
                    "WHERE id = :message_id AND state = 'streaming'"
                ),
                {"message_id": row["assistant_message_id"]},
            )
            if message_result.rowcount != 1:
                raise RuntimeError(
                    "cannot reconcile historical assistant message: "
                    f"{row['assistant_message_id']}"
                )
    finally:
        guard._create_guard_triggers(connection)
        outcomes._replace_attempt_triggers(connection)


def upgrade() -> None:
    _reconcile_duplicate_active_attempts(op.get_bind())
    op.create_table(
        "workspace_windows",
        sa.Column("window_id", sa.String(length=128), primary_key=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("geometry_json", sa.Text(), nullable=True),
        sa.Column("selected_chat_id", sa.String(length=64), nullable=True),
        sa.Column("rail_collapsed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("restore_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["selected_chat_id"], ["chats.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ux_generation_attempts_active_chat",
        "generation_attempts",
        ["chat_id"],
        unique=True,
        sqlite_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    raise RuntimeError("Phase 4 workspace state cannot be downgraded")
