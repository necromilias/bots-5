"""Add the bounded Phase 8 inspector restoration state."""

from alembic import op
import sqlalchemy as sa


revision = "0011_phase8_inspector_state"
down_revision = "0010_phase7_search_navigation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_windows",
        sa.Column("inspector_open", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("workspace_windows", sa.Column("inspector_message_id", sa.String(length=64), nullable=True))
    op.add_column("workspace_windows", sa.Column("inspector_leaf_message_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Phase 8 inspector state cannot be downgraded")
