"""Add Phase 11 organisation: folders, pins, and archive-pin integration."""

from alembic import op
import sqlalchemy as sa


revision = "0013_phase11_organisation"
down_revision = "0012_phase9_archive_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create folders table
    op.create_table(
        "folders",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
    )

    # Add folder_id column to chats using batch mode (without recreate first)
    # This avoids the trigger issue during rename
    with op.batch_alter_table("chats") as batch:
        batch.add_column(sa.Column("folder_id", sa.String(64), nullable=True))

    # Add is_pinned column
    with op.batch_alter_table("chats") as batch:
        batch.add_column(sa.Column("is_pinned", sa.Boolean, nullable=False, server_default="0"))

    # Create index
    op.execute("CREATE INDEX ix_chats_folder_id ON chats(folder_id)")

    # Note: SQLite does not support adding foreign key constraints via ALTER TABLE.
    # The folder_id column is added but the FK constraint cannot be added on existing tables.
    # The referential integrity is maintained at the application level.

    # Trigger: archive clears pin (one-way)
    op.execute("""
        CREATE TRIGGER phase11_archive_clears_pin
        BEFORE UPDATE OF archived_at ON chats
        WHEN OLD.is_pinned = 1 AND NEW.archived_at IS NOT NULL
        BEGIN
            UPDATE chats SET is_pinned = 0 WHERE id = NEW.id;
        END
    """)

    # Trigger: ensure is_pinned is 0 or 1
    op.execute("""
        CREATE TRIGGER phase11_is_pinned_check
        BEFORE UPDATE OF is_pinned ON chats
        WHEN NEW.is_pinned NOT IN (0, 1)
        BEGIN
            SELECT RAISE(ABORT, 'is_pinned must be 0 or 1');
        END
    """)

    # Trigger: when a folder is deleted, unfile all chats in that folder
    op.execute("""
        CREATE TRIGGER phase11_folder_delete_unfiles_members
        BEFORE DELETE ON folders
        BEGIN
            UPDATE chats SET folder_id = NULL WHERE folder_id = OLD.id;
        END
    """)


def downgrade() -> None:
    # Drop triggers.  Every trigger created in upgrade() must be dropped here:
    # leaving `phase11_folder_delete_unfiles_members` behind would point at a
    # table this downgrade is about to delete.
    op.execute("DROP TRIGGER IF EXISTS phase11_is_pinned_check")
    op.execute("DROP TRIGGER IF EXISTS phase11_archive_clears_pin")
    op.execute("DROP TRIGGER IF EXISTS phase11_folder_delete_unfiles_members")

    # Drop index
    op.execute("DROP INDEX IF EXISTS ix_chats_folder_id")

    # Drop columns using batch mode
    with op.batch_alter_table("chats") as batch:
        batch.drop_column("is_pinned")
        batch.drop_column("folder_id")

    # Drop folders table
    op.drop_table("folders")
