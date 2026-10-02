"""Add Phase 11 duplicate admission guard for preserving message states."""

from alembic import op


revision = "0015_phase11_duplicate_admission"
down_revision = "0014_phase11_message_tombstone"
branch_labels = None
depends_on = None

# Current DDL for the trigger (with duplicate admission guard and 'deleted' state)
CURRENT_TRIGGER_DDL = """CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages
    WHEN bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND bots5_duplicate_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND (
      typeof(NEW.sequence) <> 'integer' OR typeof(NEW.revision) <> 'integer'
      OR NEW.sequence < 1 OR NEW.role NOT IN ('user', 'assistant')
      OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted', 'deleted')
      OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted', 'deleted'))
      OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0))
      OR NEW.revision < 1
    ) BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END"""

# Pre-0014 DDL for the trigger (without duplicate admission guard, without 'deleted' state)
ORIGINAL_TRIGGER_DDL = """CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages
    WHEN bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND (
      typeof(NEW.sequence) <> 'integer' OR typeof(NEW.revision) <> 'integer'
      OR NEW.sequence < 1 OR NEW.role NOT IN ('user', 'assistant')
      OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted')
      OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted'))
      OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0))
      OR NEW.revision < 1
    ) BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END"""

# 0014 DDL for the trigger (without duplicate admission guard, WITH 'deleted' state)
TRIGGER_0014_DDL = """CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages
    WHEN bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND (
      typeof(NEW.sequence) <> 'integer' OR typeof(NEW.revision) <> 'integer'
      OR NEW.sequence < 1 OR NEW.role NOT IN ('user', 'assistant')
      OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted', 'deleted')
      OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted', 'deleted'))
      OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0))
      OR NEW.revision < 1
    ) BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END"""


def upgrade() -> None:
    # Drop the existing trigger (0014 version)
    op.execute("DROP TRIGGER IF EXISTS messages_validate_insert")
    # Create the new trigger with duplicate admission guard
    op.execute(CURRENT_TRIGGER_DDL)


def downgrade() -> None:
    # Drop the current trigger (with duplicate admission guard)
    op.execute("DROP TRIGGER IF EXISTS messages_validate_insert")
    # Recreate the 0014 trigger (with 'deleted' but without duplicate admission guard)
    op.execute(TRIGGER_0014_DDL)
