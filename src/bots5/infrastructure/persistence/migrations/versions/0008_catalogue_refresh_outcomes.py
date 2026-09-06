"""Persist the last explicit model catalogue refresh outcome."""

from alembic import op
import sqlalchemy as sa


revision = "0008_catalogue_refresh_outcomes"
down_revision = "0007_phase5_provider_model_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalogue_refresh_state",
        sa.Column("connection_id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("refresh_revision", sa.Integer(), nullable=False),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["provider_connections.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('never', 'succeeded', 'failed')",
            name="ck_catalogue_refresh_status",
        ),
        sa.CheckConstraint(
            "refresh_revision >= 0",
            name="ck_catalogue_refresh_revision",
        ),
        sa.CheckConstraint(
            "failure_class IS NULL OR failure_class IN ('transport', 'timeout', 'provider_http', 'protocol', 'unknown')",
            name="ck_catalogue_refresh_failure_class",
        ),
        sa.CheckConstraint(
            "failure_message IS NULL OR failure_message IN ('provider is unreachable', 'provider discovery timed out', 'provider returned an HTTP error', 'provider returned an invalid model catalogue', 'provider discovery failed')",
            name="ck_catalogue_refresh_failure_message",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_class IS NOT NULL AND failure_message IS NOT NULL) "
            "OR (status <> 'failed' AND failure_class IS NULL AND failure_message IS NULL)",
            name="ck_catalogue_refresh_failure_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'never' AND refresh_revision = 0) "
            "OR (status <> 'never' AND refresh_revision > 0)",
            name="ck_catalogue_refresh_revision_consistency",
        ),
        sa.CheckConstraint(
            "(failure_class = 'transport' AND failure_message = 'provider is unreachable') "
            "OR (failure_class = 'timeout' AND failure_message = 'provider discovery timed out') "
            "OR (failure_class = 'provider_http' AND failure_message = 'provider returned an HTTP error') "
            "OR (failure_class = 'protocol' AND failure_message = 'provider returned an invalid model catalogue') "
            "OR (failure_class = 'unknown' AND failure_message = 'provider discovery failed') "
            "OR (failure_class IS NULL AND failure_message IS NULL)",
            name="ck_catalogue_refresh_failure_diagnostic",
        ),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO catalogue_refresh_state "
            "(connection_id, status, refresh_revision, failure_class, failure_message, updated_at) "
            "SELECT id, 'never', 0, NULL, NULL, updated_at FROM provider_connections"
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER phase5_catalogue_refresh_state_insert
            AFTER INSERT ON provider_connections
            WHEN NOT EXISTS (
                SELECT 1 FROM catalogue_refresh_state
                WHERE connection_id = NEW.id
            )
            BEGIN
                INSERT INTO catalogue_refresh_state
                    (connection_id, status, refresh_revision, failure_class, failure_message, updated_at)
                VALUES (NEW.id, 'never', 0, NULL, NULL, NEW.updated_at);
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER phase5_catalogue_refresh_state_delete_guard
            BEFORE DELETE ON catalogue_refresh_state
            BEGIN
                SELECT RAISE(ABORT, 'Phase 5 catalogue refresh state is immutable');
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS phase5_provider_connection_catalogue_revision_guard;
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER phase5_provider_connection_catalogue_revision_guard
            BEFORE UPDATE OF catalogue_revision ON provider_connections
            WHEN NEW.catalogue_revision < OLD.catalogue_revision
              OR (
                NEW.catalogue_revision > OLD.catalogue_revision
                AND bots5_phase5_catalogue_refresh_allowed(
                    NEW.id,
                    OLD.catalogue_revision,
                    (SELECT refresh_revision FROM catalogue_refresh_state WHERE connection_id = NEW.id),
                    NEW.catalogue_revision
                ) = 0
                AND bots5_phase5_connection_identity_update_allowed(
                    NEW.id, OLD.revision, NEW.revision,
                    OLD.catalogue_revision, NEW.catalogue_revision
                ) = 0
              )
            BEGIN
                SELECT RAISE(ABORT, 'provider connection catalogue revision requires core authority');
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER phase5_catalogue_refresh_revision_guard
            BEFORE UPDATE ON catalogue_refresh_state
            BEGIN
                SELECT RAISE(ABORT, 'Phase 5 catalogue refresh state requires core authority')
                WHERE bots5_phase5_catalogue_refresh_allowed(
                    NEW.connection_id,
                    (SELECT catalogue_revision - 1 FROM provider_connections WHERE id = NEW.connection_id),
                    OLD.refresh_revision,
                    NEW.refresh_revision
                ) = 0;
                SELECT RAISE(ABORT, 'Phase 5 catalogue refresh revision exceeds connection revision')
                WHERE NEW.refresh_revision > (
                    SELECT catalogue_revision FROM provider_connections
                    WHERE id = NEW.connection_id
                );
            END
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("Phase 5 catalogue refresh outcomes cannot be downgraded")
