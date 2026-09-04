"""Replace the writable lifecycle marker with a connection-local guard."""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "0004_integrity_guard_function"
down_revision = "0003_integrity_boundaries"
branch_labels = None
depends_on = None


_REPLACED_TRIGGERS = (
    "messages_validate_insert",
    "messages_validate_update",
    "generation_attempt_validate_insert",
    "generation_attempt_validate_update",
    "chats_validate_insert",
    "chats_revision_monotonic",
)
_ADDED_TRIGGERS = (
    "chats_timestamps_insert",
    "chats_timestamps_update",
    "messages_timestamps_insert",
    "messages_active_head_parent_guard",
    "messages_delete_immutable",
    "generation_attempts_delete_immutable",
    "chats_head_update_guard",
)


def _drop_guard_triggers(connection) -> None:
    for trigger in (*_REPLACED_TRIGGERS, *_ADDED_TRIGGERS):
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))


def _create_guard_triggers(connection) -> None:
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_timestamps_insert BEFORE INSERT ON chats "
            "WHEN bots5_valid_timestamp(NEW.created_at) = 0 "
            "OR bots5_valid_timestamp(NEW.updated_at) = 0 "
            "BEGIN SELECT RAISE(ABORT, 'chat timestamps are invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_timestamps_update BEFORE UPDATE OF created_at, updated_at ON chats "
            "WHEN bots5_valid_timestamp(NEW.created_at) = 0 "
            "OR bots5_valid_timestamp(NEW.updated_at) = 0 "
            "BEGIN SELECT RAISE(ABORT, 'chat timestamps are invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_timestamps_insert BEFORE INSERT ON messages "
            "WHEN bots5_valid_timestamp(NEW.created_at) = 0 "
            "BEGIN SELECT RAISE(ABORT, 'message timestamp is invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_active_head_parent_guard BEFORE INSERT ON messages "
            "WHEN NEW.parent_id IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM chats WHERE id = NEW.chat_id AND head_message_id = NEW.parent_id) "
            "AND bots5_internal_transition(NEW.id, NULL, 'start-user') = 0 "
            "BEGIN SELECT RAISE(ABORT, 'message cannot be inserted beneath the active head'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages "
            "WHEN typeof(NEW.sequence) <> 'integer' "
            "OR typeof(NEW.revision) <> 'integer' "
            "OR NEW.sequence < 1 "
            "OR NEW.role NOT IN ('user', 'assistant') "
            "OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted') "
            "OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted')) "
            "OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' "
            "OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0)) "
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
            "AND bots5_internal_transition(NEW.id, NULL, 'finalize-message') = 0) "
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
            "WHEN bots5_internal_transition(NEW.assistant_message_id, NEW.id, 'start-attempt') = 0 "
            "OR NEW.state NOT IN ('running', 'complete', 'incomplete', 'failed', 'aborted') "
            "OR (NEW.state = 'running' AND NEW.ended_at IS NOT NULL) "
            "OR (NEW.state <> 'running' AND NEW.ended_at IS NULL) "
            "OR bots5_valid_timestamp(NEW.started_at) = 0 "
            "OR (NEW.ended_at IS NOT NULL AND bots5_valid_timestamp(NEW.ended_at) = 0) "
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
            "OR (NEW.ended_at IS NOT NULL AND bots5_valid_timestamp(NEW.ended_at) = 0) "
            "OR (OLD.state <> 'running' AND (NEW.state <> OLD.state OR NEW.ended_at IS NOT OLD.ended_at "
            "OR NEW.error_type IS NOT OLD.error_type OR NEW.error_message IS NOT OLD.error_message)) "
            "OR (OLD.state = 'running' AND NEW.state <> 'running' "
            "AND bots5_internal_transition(NEW.assistant_message_id, NEW.id, 'finalize-attempt') = 0) "
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
            "WHEN typeof(NEW.revision) <> 'integer' OR NEW.revision < 0 "
            "BEGIN SELECT RAISE(ABORT, 'chat revision is invalid'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_revision_monotonic BEFORE UPDATE OF revision ON chats "
            "WHEN typeof(OLD.revision) <> 'integer' OR typeof(NEW.revision) <> 'integer' "
            "OR NEW.revision < OLD.revision OR NEW.revision > OLD.revision + 1 "
            "OR (NEW.revision <> OLD.revision AND "
            "bots5_internal_transition(NEW.head_message_id, NEW.id, 'advance-chat') = 0) "
            "BEGIN SELECT RAISE(ABORT, 'chat revision must advance monotonically by one'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER chats_head_update_guard BEFORE UPDATE OF head_message_id ON chats "
            "WHEN NEW.head_message_id IS NOT OLD.head_message_id "
            "AND bots5_internal_transition(NEW.head_message_id, NEW.id, 'advance-chat') = 0 "
            "BEGIN SELECT RAISE(ABORT, 'chat head must advance through the application'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER messages_delete_immutable BEFORE DELETE ON messages "
            "BEGIN SELECT RAISE(ABORT, 'individual messages are immutable'); END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER generation_attempts_delete_immutable BEFORE DELETE ON generation_attempts "
            "BEGIN SELECT RAISE(ABORT, 'generation attempts are immutable'); END"
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    integrity = import_module(
        "bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries"
    )
    integrity._validate_existing_state(connection)
    _drop_guard_triggers(connection)
    op.drop_table("lifecycle_transitions")
    _create_guard_triggers(connection)


def downgrade() -> None:
    raise RuntimeError("connection-local integrity guards cannot be downgraded")
