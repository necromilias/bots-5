"""Add Phase 9 Slice B Archive-import durable state."""

from alembic import op

from bots5.infrastructure.persistence.phase9_schema import DDL


revision = "0012_phase9_archive_import"
down_revision = "0011_phase8_inspector_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Phase 9 Archive import cannot be downgraded")
