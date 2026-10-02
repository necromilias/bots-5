"""Phase 11 M3 integrity repairs and the lossless deletion path.

Everything here lands in one NEW revision because each underlying definition
is owned by a hash-frozen earlier migration that must not be edited:

1. F5 pin integrity (repair): 0013's ``phase11_is_pinned_check`` trigger is
   ``BEFORE UPDATE OF is_pinned`` ONLY, so a raw INSERT could still store an
   out-of-range pin value.  This revision adds the missing INSERT-time guard.
2. F4 folder integrity (repair): ``chats.folder_id`` had no referential
   enforcement at all.  This revision adds INSERT/UPDATE guards that refuse a
   folder_id which does not reference an existing ``folders`` row.
3. F7 deletion path (enablement): the frozen trigger set made BOTH halves of
   F7 impossible — no settled message row could ever become a ``'deleted'``
   tombstone, and no chat with messages could ever be deleted (deleting the
   chat cascades into ``messages``, and the cascade fires
   ``messages_delete_immutable``; the messages themselves self-reference via
   ``parent_id``/``supersedes_id``; attempts, their attachment links and
   context plans each carry their own immutability guards).  This revision
   recreates those guards so that
   - the ONLY permitted content/state change for a settled message is the
     exact tombstone shape (state ``'deleted'`` AND content ``''``), and
   - message rows, attempt rows, their attachment LINK rows, and context
     plan rows may only be deleted while a phase 11 chat-deletion admission
     is armed for exactly their chat (a connection-local arm, never a
     blanket permission — without the arm every one of these deletes is
     still refused with its original abort message).

The design sentence this implements is "whole-chat deletion is the ordinary
physically destructive chat operation" while "individual deletion is
lossless/reconstructable": per-message deletion stays a tombstone, and the
physical destruction is bounded to ONE admitted chat deletion at a time.

Every original trigger definition is restored verbatim by ``downgrade``, so a
0017 -> 0016 downgrade reproduces the previous schema exactly.
"""

from alembic import op


revision = "0017_phase11_integrity"
down_revision = "0016_phase11_workspace_state"
branch_labels = None
depends_on = None


# --- Frozen 0016-era definitions, restored verbatim by downgrade() ----------

_ORIGINAL_MESSAGES_DELETE_IMMUTABLE = (
    "CREATE TRIGGER messages_delete_immutable BEFORE DELETE ON messages "
    "BEGIN SELECT RAISE(ABORT, 'individual messages are immutable'); END"
)

_ORIGINAL_ATTEMPTS_DELETE_IMMUTABLE = (
    "CREATE TRIGGER generation_attempts_delete_immutable BEFORE DELETE ON generation_attempts "
    "BEGIN SELECT RAISE(ABORT, 'generation attempts are immutable'); END"
)

_ORIGINAL_TERMINAL_IMMUTABLE = (
    "CREATE TRIGGER messages_terminal_immutable "
    "BEFORE UPDATE OF content, state ON messages "
    "WHEN OLD.state <> 'streaming' "
    "BEGIN SELECT RAISE(ABORT, 'terminal message is immutable'); END"
)

_ORIGINAL_VALIDATE_UPDATE = (
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

_ORIGINAL_MESSAGE_ATTACHMENT_DELETE_GUARD = (
    "CREATE TRIGGER phase6_message_attachment_delete_guard\n"
    "    BEFORE DELETE ON message_attachments\n"
    "    BEGIN SELECT RAISE(ABORT, 'Phase 6 message attachment reference is immutable'); END"
)

_ORIGINAL_ATTEMPT_ATTACHMENT_DELETE_GUARD = (
    "CREATE TRIGGER phase6_attempt_attachment_delete_guard\n"
    "    BEFORE DELETE ON attempt_attachments\n"
    "    BEGIN SELECT RAISE(ABORT, 'Phase 6 attempt attachment reference is immutable'); END"
)

_ORIGINAL_CONTEXT_PLAN_DELETE_GUARD = (
    "CREATE TRIGGER phase6_context_plan_delete_guard\n"
    "    BEFORE DELETE ON context_plans\n"
    "    BEGIN SELECT RAISE(ABORT, 'Phase 6 context plan is immutable'); END"
)


_ARM = "bots5_phase11_chat_message_delete_allowed"


# --- 0017 definitions -------------------------------------------------------


def _tombstone_validate_update() -> str:
    """messages_validate_update with the F7 tombstone target admitted.

    'deleted' is added to the global and per-role state lists exactly the way
    the frozen 0014 revision added it on the INSERT side.  The streaming
    branches deliberately do NOT gain 'deleted': a message that is still being
    generated cannot be tombstoned mid-flight (mirror duplicate_chat's refusal
    of running generations).
    """
    return (
        "CREATE TRIGGER messages_validate_update BEFORE UPDATE OF state, content ON messages "
        "WHEN NEW.role NOT IN ('user', 'assistant') "
        "OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted', 'deleted') "
        "OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted', 'deleted')) "
        "OR (NEW.role = 'assistant' AND NEW.state NOT IN ('streaming', 'complete', 'incomplete', 'truncated', 'failed', 'aborted', 'deleted')) "
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


def upgrade() -> None:
    # --- F7 enablement: recreate the frozen deletion guards with the --------
    # connection-local chat-deletion admission.
    op.execute("DROP TRIGGER IF EXISTS messages_delete_immutable")
    op.execute(
        "CREATE TRIGGER messages_delete_immutable BEFORE DELETE ON messages "
        f"WHEN {_ARM}(OLD.chat_id) <> 1 "
        "BEGIN SELECT RAISE(ABORT, 'individual messages are immutable'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS generation_attempts_delete_immutable")
    op.execute(
        "CREATE TRIGGER generation_attempts_delete_immutable BEFORE DELETE ON generation_attempts "
        f"WHEN {_ARM}(OLD.chat_id) <> 1 "
        "BEGIN SELECT RAISE(ABORT, 'generation attempts are immutable'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS messages_terminal_immutable")
    op.execute(
        "CREATE TRIGGER messages_terminal_immutable "
        "BEFORE UPDATE OF content, state ON messages "
        "WHEN OLD.state <> 'streaming' "
        "AND (NEW.state <> 'deleted' OR NEW.content <> '') "
        "BEGIN SELECT RAISE(ABORT, 'terminal message is immutable'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS messages_validate_update")
    op.execute(_tombstone_validate_update())

    # Attachment LINK rows and context plans join the same admission: a link
    # or plan resolves to its owning chat, and only an armed deletion of that
    # exact chat may remove it.
    op.execute("DROP TRIGGER IF EXISTS phase6_message_attachment_delete_guard")
    op.execute(
        "CREATE TRIGGER phase6_message_attachment_delete_guard "
        "BEFORE DELETE ON message_attachments "
        f"WHEN {_ARM}((SELECT chat_id FROM messages WHERE id = OLD.message_id)) <> 1 "
        "BEGIN SELECT RAISE(ABORT, 'Phase 6 message attachment reference is immutable'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS phase6_attempt_attachment_delete_guard")
    op.execute(
        "CREATE TRIGGER phase6_attempt_attachment_delete_guard "
        "BEFORE DELETE ON attempt_attachments "
        f"WHEN {_ARM}((SELECT chat_id FROM generation_attempts WHERE id = OLD.attempt_id)) <> 1 "
        "BEGIN SELECT RAISE(ABORT, 'Phase 6 attempt attachment reference is immutable'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS phase6_context_plan_delete_guard")
    op.execute(
        "CREATE TRIGGER phase6_context_plan_delete_guard "
        "BEFORE DELETE ON context_plans "
        f"WHEN {_ARM}((SELECT chat_id FROM generation_attempts WHERE id = OLD.attempt_id)) <> 1 "
        "BEGIN SELECT RAISE(ABORT, 'Phase 6 context plan is immutable'); END"
    )

    # --- F5 repair: pin values are range-checked at INSERT time too --------
    op.execute(
        "CREATE TRIGGER phase11_chat_pin_insert_guard "
        "BEFORE INSERT ON chats "
        "WHEN NEW.is_pinned NOT IN (0, 1) "
        "BEGIN SELECT RAISE(ABORT, 'is_pinned must be 0 or 1'); END"
    )

    # --- F4 repair: chats.folder_id references an existing folder ----------
    op.execute(
        "CREATE TRIGGER phase11_folder_reference_insert_guard "
        "BEFORE INSERT ON chats "
        "WHEN NEW.folder_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM folders WHERE id = NEW.folder_id) "
        "BEGIN SELECT RAISE(ABORT, 'chat folder_id does not reference a folder'); END"
    )
    op.execute(
        "CREATE TRIGGER phase11_folder_reference_update_guard "
        "BEFORE UPDATE OF folder_id ON chats "
        "WHEN NEW.folder_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM folders WHERE id = NEW.folder_id) "
        "BEGIN SELECT RAISE(ABORT, 'chat folder_id does not reference a folder'); END"
    )


def downgrade() -> None:
    # Drop every object 0017 created or replaced.
    op.execute("DROP TRIGGER IF EXISTS phase11_folder_reference_update_guard")
    op.execute("DROP TRIGGER IF EXISTS phase11_folder_reference_insert_guard")
    op.execute("DROP TRIGGER IF EXISTS phase11_chat_pin_insert_guard")
    op.execute("DROP TRIGGER IF EXISTS phase6_context_plan_delete_guard")
    op.execute("DROP TRIGGER IF EXISTS phase6_attempt_attachment_delete_guard")
    op.execute("DROP TRIGGER IF EXISTS phase6_message_attachment_delete_guard")
    op.execute("DROP TRIGGER IF EXISTS messages_validate_update")
    op.execute("DROP TRIGGER IF EXISTS messages_terminal_immutable")
    op.execute("DROP TRIGGER IF EXISTS generation_attempts_delete_immutable")
    op.execute("DROP TRIGGER IF EXISTS messages_delete_immutable")

    # Restore the frozen 0016-era definitions verbatim.
    op.execute(_ORIGINAL_VALIDATE_UPDATE)
    op.execute(_ORIGINAL_TERMINAL_IMMUTABLE)
    op.execute(_ORIGINAL_ATTEMPTS_DELETE_IMMUTABLE)
    op.execute(_ORIGINAL_MESSAGES_DELETE_IMMUTABLE)
    op.execute(_ORIGINAL_MESSAGE_ATTACHMENT_DELETE_GUARD)
    op.execute(_ORIGINAL_ATTEMPT_ATTACHMENT_DELETE_GUARD)
    op.execute(_ORIGINAL_CONTEXT_PLAN_DELETE_GUARD)
