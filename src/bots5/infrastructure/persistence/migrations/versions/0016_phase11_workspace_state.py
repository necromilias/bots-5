"""Add Phase 11 workspace/settings persistence: maximize, scroll, drafts, dock blob, keybindings, font/scale."""

from alembic import op
import sqlalchemy as sa


revision = "0016_phase11_workspace_state"
down_revision = "0015_phase11_duplicate_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add maximize state and scroll position to workspace_windows
    with op.batch_alter_table("workspace_windows") as batch:
        batch.add_column(sa.Column("maximized", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("transcript_scroll_position", sa.Integer(), nullable=True))

    # Create chat_drafts table.  A draft belongs to one chat and must be removed with it.
    op.create_table(
        "chat_drafts",
        sa.Column("chat_id", sa.String(64), primary_key=True),
        sa.Column("draft_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
    )

    # Create dock_layout table for QMainWindow.saveState() opaque blob
    op.create_table(
        "dock_layout",
        sa.Column("window_id", sa.String(128), primary_key=True),
        sa.Column("dock_state_blob", sa.BLOB(), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )

    # Create keybinding_overrides table
    op.create_table(
        "keybinding_overrides",
        sa.Column("action_id", sa.String(256), primary_key=True),
        sa.Column("shortcut", sa.String(64), nullable=False, server_default=""),
        sa.Column("conflict_detected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )

    # Create font_scale_settings table
    op.create_table(
        "font_scale_settings",
        sa.Column("id", sa.String(1), primary_key=True, server_default="1"),
        sa.Column("scale_factor", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("ui_font_family", sa.Text(), nullable=True),
        sa.Column("transcript_font_family", sa.Text(), nullable=True),
        sa.Column("code_font_family", sa.Text(), nullable=True),
        sa.Column("base_font_size_pt", sa.Float(), nullable=False, server_default="11.0"),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.CheckConstraint("id = '1'", name="ck_font_scale_single_row"),
    )


def downgrade() -> None:
    # Drop font_scale_settings table
    op.drop_table("font_scale_settings")

    # Drop keybinding_overrides table
    op.drop_table("keybinding_overrides")

    # Drop dock_layout table
    op.drop_table("dock_layout")

    # Drop chat_drafts table
    op.drop_table("chat_drafts")

    # Remove maximize and scroll columns from workspace_windows
    with op.batch_alter_table("workspace_windows") as batch:
        batch.drop_column("transcript_scroll_position")
        batch.drop_column("maximized")
