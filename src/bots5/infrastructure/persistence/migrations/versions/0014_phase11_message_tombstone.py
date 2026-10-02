"""Add Phase 11 message tombstone state for deletion."""

from alembic import op


revision = "0014_phase11_message_tombstone"
down_revision = "0013_phase11_organisation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop and recreate messages_validate_insert trigger to include 'deleted' state
    op.execute("DROP TRIGGER IF EXISTS messages_validate_insert")
    op.execute("""
        CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages
        WHEN bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND (
          typeof(NEW.sequence) <> 'integer' OR typeof(NEW.revision) <> 'integer'
          OR NEW.sequence < 1 OR NEW.role NOT IN ('user', 'assistant')
          OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted', 'deleted')
          OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted', 'deleted'))
          OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0))
          OR NEW.revision < 1
        ) BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END
    """)


def downgrade() -> None:
    # Drop and recreate messages_validate_insert trigger without 'deleted' state
    op.execute("DROP TRIGGER IF EXISTS messages_validate_insert")
    op.execute("""
        CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages
        WHEN bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND (
          typeof(NEW.sequence) <> 'integer' OR typeof(NEW.revision) <> 'integer'
          OR NEW.sequence < 1 OR NEW.role NOT IN ('user', 'assistant')
          OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted')
          OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted'))
          OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0))
          OR NEW.revision < 1
        ) BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END
    """)
