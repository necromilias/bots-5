"""Install the additive Phase 6 context and attachment tables."""

from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime

from bots5.infrastructure.persistence.phase6_schema import ensure_phase6_schema, install_phase6_attempt_guards


revision = "0009_phase6_context_attachments"
down_revision = "0008_catalogue_refresh_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_phase6_schema(op.get_bind())
    connection = op.get_bind()
    connection.execute(sa.text("DROP TRIGGER IF EXISTS phase5_attempt_attribution_insert"))
    connection.execute(sa.text("DROP TRIGGER IF EXISTS phase5_attempt_attribution_update"))
    connection.execute(sa.text("DROP TRIGGER IF EXISTS generation_attempt_phase5_completion_insert"))
    connection.execute(sa.text("DROP TRIGGER IF EXISTS generation_attempt_phase5_completion_update"))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_attempt_attribution_insert
        BEFORE INSERT ON generation_attempts
        WHEN (
          json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3)
          AND (
            NEW.connection_id IS NULL OR NEW.model_entry_id IS NULL
            OR json_extract(NEW.request_snapshot, '$.connection_id') IS NOT NEW.connection_id
            OR json_extract(NEW.request_snapshot, '$.model_entry_id') IS NOT NEW.model_entry_id
            OR NOT EXISTS (
              SELECT 1 FROM model_catalogue_entries AS m
              JOIN provider_connections AS p ON p.id = m.connection_id
              WHERE m.id = NEW.model_entry_id
                AND m.connection_id = NEW.connection_id
                AND m.provider_model_id = json_extract(NEW.request_snapshot, '$.model')
                AND p.name = json_extract(NEW.request_snapshot, '$.connection_name')
                AND p.revision = json_extract(NEW.request_snapshot, '$.connection_revision')
                AND p.catalogue_revision = json_extract(NEW.request_snapshot, '$.catalogue_revision')
                AND p.endpoint IS json_extract(NEW.request_snapshot, '$.endpoint')
                AND p.credential_source = json_extract(NEW.request_snapshot, '$.credential_source')
                AND p.credential_reference IS json_extract(NEW.request_snapshot, '$.credential_reference')
                AND p.id = json_extract(NEW.request_snapshot, '$.connection_id')
            )
          )
        ) OR (
          COALESCE(json_extract(NEW.request_snapshot, '$.snapshot_version'), 0) NOT IN (2, 3)
          AND (NEW.connection_id IS NOT NULL OR NEW.model_entry_id IS NOT NULL)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is inconsistent'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_attempt_attribution_update
        BEFORE UPDATE OF connection_id, model_entry_id ON generation_attempts
        WHEN (
          json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3)
          AND (NEW.connection_id IS NOT OLD.connection_id OR NEW.model_entry_id IS NOT OLD.model_entry_id)
        ) OR (
          COALESCE(json_extract(NEW.request_snapshot, '$.snapshot_version'), 0) NOT IN (2, 3)
          AND (NEW.connection_id IS NOT NULL OR NEW.model_entry_id IS NOT NULL)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is immutable'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER generation_attempt_phase5_completion_insert
        BEFORE INSERT ON generation_attempts
        WHEN json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3) AND (
          (NEW.state = 'complete' AND (NEW.finish_reason IS NOT 'stop' OR NEW.remote_outcome_unknown IS NOT 0)) OR
          (NEW.state = 'incomplete' AND NEW.finish_reason IS 'stop') OR
          (NEW.state IN ('running', 'failed', 'aborted') AND NEW.finish_reason IS NOT NULL)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 generation finish_reason is inconsistent with state'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER generation_attempt_phase5_completion_update
        BEFORE UPDATE OF state, finish_reason, request_snapshot ON generation_attempts
        WHEN json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3) AND (
          (NEW.state = 'complete' AND (NEW.finish_reason IS NOT 'stop' OR NEW.remote_outcome_unknown IS NOT 0)) OR
          (NEW.state = 'incomplete' AND NEW.finish_reason IS 'stop') OR
          (NEW.state IN ('running', 'failed', 'aborted') AND NEW.finish_reason IS NOT NULL)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 generation finish_reason is inconsistent with state'); END
    """))
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    connection.execute(
        sa.text(
            "INSERT INTO capability_facts "
            "(id, model_entry_id, capability_key, state, source, source_revision, value, provenance_json, observed_at) "
            "SELECT model_catalogue_entries.id || '-phase6-context', model_catalogue_entries.id, "
            "'limits.context_tokens', 'supported', 'trusted_registry', 1, 32768, "
            "'{\"field\":\"built-in deterministic context window\"}', :now "
            "FROM model_catalogue_entries JOIN provider_connections "
            "ON provider_connections.id = model_catalogue_entries.connection_id "
            "WHERE provider_connections.backend_type = 'fake' "
            "AND NOT EXISTS (SELECT 1 FROM capability_facts f WHERE f.model_entry_id = model_catalogue_entries.id "
            "AND f.capability_key = 'limits.context_tokens')"
        ),
        {"now": now},
    )
    install_phase6_attempt_guards(connection)


def downgrade() -> None:
    raise RuntimeError("Phase 6 context and attachment schema cannot be downgraded")
