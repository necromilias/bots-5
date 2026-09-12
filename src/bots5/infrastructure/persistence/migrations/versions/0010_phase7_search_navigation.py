"""Add Phase 7 FTS5 search, archive state, and source coordination."""

from alembic import op
import sqlalchemy as sa

from bots5.infrastructure.persistence.phase7_schema import ensure_phase7_schema


revision = "0010_phase7_search_navigation"
down_revision = "0009_phase6_context_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    op.add_column("chats", sa.Column("archived_at", sa.Text(), nullable=True))
    ensure_phase7_schema(connection)


def downgrade() -> None:
    raise RuntimeError("Phase 7 search/navigation schema cannot be downgraded")
