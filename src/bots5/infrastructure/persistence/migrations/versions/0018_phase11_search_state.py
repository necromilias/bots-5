"""Phase 11 fork R-15: the faithful search-state restore plane.

Adds the persisted per-window search presentation state to
``workspace_windows``:

- ``search_open``         whether the search dock was open at last save,
- ``search_query``        the last executed literal search query,
- ``search_filters_json`` the complete, faithful encoding of ALL THIRTEEN
                          ``SearchFilters`` fields (chat_id, document_kinds,
                          roles, message_states, backend_ids, provider_ids,
                          models, connection_ids, model_entry_ids,
                          active_branch_only, include_archived, after,
                          before),
- ``search_cursor``       the result pagination cursor of the last page.

The columns ride the existing ``workspace_windows`` row lifecycle: deleting a
window's row (its registered close path when restore-on-close is not wanted)
drops its search state with it, and no orphan table is introduced.

``downgrade`` removes the four columns again, restoring the 0017 schema
exactly.  Every original column definition is preserved verbatim because the
batch operation copies the reflected table and only the four new columns are
dropped.
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_phase11_search_state"
down_revision = "0017_phase11_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_windows") as batch:
        batch.add_column(
            sa.Column(
                "search_open",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("search_query", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("search_filters_json", sa.Text(), nullable=True)
        )
        batch.add_column(sa.Column("search_cursor", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspace_windows") as batch:
        batch.drop_column("search_cursor")
        batch.drop_column("search_filters_json")
        batch.drop_column("search_query")
        batch.drop_column("search_open")
