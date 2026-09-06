"""Add bounded Phase 5 provider/model configuration authority."""

from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

from bots5.core.secrets import secret_key_forbidden_sql_expression


revision = "0007_phase5_provider_model_configuration"
down_revision = "0006_phase4_workspace"
branch_labels = None
depends_on = None


FAKE_CONNECTION_ID = "01900000-0000-7000-8000-000000000005"
FAKE_MODEL_ENTRY_ID = "01900000-0000-7000-8000-000000000006"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _create_tables() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.String(length=256), nullable=False),
        sa.Column("backend_type", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("credential_source", sa.String(length=64), nullable=False),
        sa.Column("credential_reference", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("catalogue_revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.UniqueConstraint("name_key", name="ux_provider_connections_name_key"),
        sa.CheckConstraint("backend_type IN ('fake', 'openai_compatible_http')", name="ck_provider_connection_backend"),
        sa.CheckConstraint("profile IN ('generic', 'openrouter')", name="ck_provider_connection_profile"),
        sa.CheckConstraint("credential_source IN ('none', 'environment', 'secret_service')", name="ck_provider_connection_credential_source"),
        sa.CheckConstraint("profile <> 'openrouter' OR credential_source <> 'none'", name="ck_provider_connection_openrouter_auth"),
        sa.CheckConstraint("revision > 0 AND catalogue_revision >= 0", name="ck_provider_connection_revisions"),
    )

    op.create_index("ix_provider_connections_enabled", "provider_connections", ["enabled", "retired"])

    op.create_table(
        "model_catalogue_entries",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("provider_model_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("availability", sa.String(length=64), nullable=False),
        sa.Column("discovery_revision", sa.Integer(), nullable=True),
        sa.Column("discovered_at", sa.String(length=40), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["provider_connections.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("connection_id", "provider_model_id", name="ux_model_catalogue_connection_model"),
        sa.CheckConstraint("origin IN ('manual', 'discovered', 'manual_confirmed')", name="ck_model_catalogue_origin"),
        sa.CheckConstraint("availability IN ('available', 'unavailable', 'stale', 'disconnected')", name="ck_model_catalogue_availability"),
        sa.CheckConstraint("revision > 0", name="ck_model_catalogue_revision"),
    )
    op.create_index("ix_model_catalogue_connection_availability", "model_catalogue_entries", ["connection_id", "availability"])

    op.create_table(
        "capability_facts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("capability_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=True),
        sa.Column("value", sa.Integer(), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("observed_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["model_entry_id"], ["model_catalogue_entries.id"], ondelete="CASCADE"),
        sa.CheckConstraint("state IN ('supported', 'unsupported', 'unknown')", name="ck_capability_fact_state"),
        sa.CheckConstraint("source IN ('manual', 'confirmed_endpoint', 'provider_metadata', 'trusted_registry', 'heuristic', 'unknown')", name="ck_capability_fact_source"),
        sa.CheckConstraint(
            "source NOT IN ('confirmed_endpoint', 'provider_metadata') OR source_revision IS NOT NULL",
            name="ck_capability_fact_source_revision",
        ),
    )
    op.create_index(
        "ix_capability_facts_model_key",
        "capability_facts",
        ["model_entry_id", "capability_key"],
    )
    op.create_table(
        "capability_overrides",
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("capability_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["model_entry_id"], ["model_catalogue_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("model_entry_id", "capability_key"),
        sa.CheckConstraint("state IN ('supported', 'unsupported', 'unknown')", name="ck_capability_override_state"),
        sa.CheckConstraint(
            "reason IS NULL OR (typeof(reason) = 'text' AND length(reason) <= 256)",
            name="ck_capability_override_reason",
        ),
    )
    op.create_table(
        "capability_observations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("capability_key", sa.String(length=128), nullable=False),
        sa.Column("observed_state", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["model_entry_id"], ["model_catalogue_entries.id"], ondelete="CASCADE"),
        sa.CheckConstraint("observed_state IN ('supported', 'unsupported', 'unknown')", name="ck_capability_observation_state"),
    )
    op.create_table(
        "application_generation_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_model_entry_id", sa.String(length=64), nullable=True),
        sa.Column("temperature", sa.Text(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("timeout_seconds", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["default_model_entry_id"], ["model_catalogue_entries.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("id = 1", name="ck_application_generation_config_singleton"),
        sa.CheckConstraint("max_output_tokens > 0", name="ck_application_generation_config_output"),
        sa.CheckConstraint("reasoning_effort IS NULL OR reasoning_effort = 'none'", name="ck_application_generation_config_reasoning"),
    )
    op.create_table(
        "model_generation_config",
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("temperature", sa.Text(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("timeout_seconds", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["model_entry_id"], ["model_catalogue_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("model_entry_id"),
    )
    op.create_table(
        "chat_model_generation_config",
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("model_entry_id", sa.String(length=64), nullable=False),
        sa.Column("temperature", sa.Text(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("timeout_seconds", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_entry_id"], ["model_catalogue_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id", "model_entry_id"),
    )

    op.create_table(
        "chat_model_selection",
        sa.Column("chat_id", sa.String(length=64), primary_key=True),
        sa.Column("model_entry_id", sa.String(length=64), nullable=True),
        sa.Column("selection_required", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_entry_id"], ["model_catalogue_entries.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "(selection_required = 1 AND model_entry_id IS NULL) OR "
            "(selection_required = 0 AND model_entry_id IS NOT NULL)",
            name="ck_chat_model_selection_consistency",
        ),
    )


def _install_attempt_attribution_guards(connection=None) -> None:
    """Close the additive attribution columns at the SQLite DML boundary."""
    connection = connection or op.get_bind()
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_attempt_attribution_insert
        BEFORE INSERT ON generation_attempts
        WHEN (
          json_extract(NEW.request_snapshot, '$.snapshot_version') = 2
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
          COALESCE(json_extract(NEW.request_snapshot, '$.snapshot_version'), 0) <> 2
          AND (NEW.connection_id IS NOT NULL OR NEW.model_entry_id IS NOT NULL)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is inconsistent'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_attempt_attribution_update
        BEFORE UPDATE OF connection_id, model_entry_id ON generation_attempts
        WHEN (
          json_extract(NEW.request_snapshot, '$.snapshot_version') = 2
          AND (
            NEW.connection_id IS NOT OLD.connection_id
            OR NEW.model_entry_id IS NOT OLD.model_entry_id
          )
        ) OR (
          COALESCE(json_extract(NEW.request_snapshot, '$.snapshot_version'), 0) <> 2
          AND (NEW.connection_id IS NOT NULL OR NEW.model_entry_id IS NOT NULL)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is immutable'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_model_catalogue_delete_referenced
        BEFORE DELETE ON model_catalogue_entries
        WHEN EXISTS (
            SELECT 1 FROM generation_attempts AS a
            WHERE a.model_entry_id = OLD.id
        )
        BEGIN SELECT RAISE(ABORT, 'referenced Phase 5 model catalogue entry is immutable'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_model_catalogue_identity_referenced
        BEFORE UPDATE OF connection_id, provider_model_id ON model_catalogue_entries
        WHEN EXISTS (
            SELECT 1 FROM generation_attempts AS a
            WHERE a.model_entry_id = OLD.id
        )
        AND (
            NEW.connection_id IS NOT OLD.connection_id
            OR NEW.provider_model_id IS NOT OLD.provider_model_id
        )
        BEGIN SELECT RAISE(ABORT, 'referenced Phase 5 model identity is immutable'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_delete_referenced
        BEFORE DELETE ON provider_connections
        WHEN EXISTS (
            SELECT 1 FROM generation_attempts AS a
            WHERE a.connection_id = OLD.id
        )
        BEGIN SELECT RAISE(ABORT, 'referenced Phase 5 provider connection is immutable'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_retirement_guard
        BEFORE UPDATE OF retired ON provider_connections
        WHEN NEW.retired = 1
          AND OLD.retired = 0
          AND EXISTS (
            SELECT 1
            FROM application_generation_config AS a
            JOIN model_catalogue_entries AS m ON m.id = a.default_model_entry_id
            WHERE a.id = 1 AND m.connection_id = NEW.id
          )
        BEGIN SELECT RAISE(ABORT, 'retiring the application-default connection requires a replacement'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_resurrection_guard
        BEFORE UPDATE OF retired ON provider_connections
        WHEN OLD.retired = 1 AND NEW.retired = 0
        BEGIN SELECT RAISE(ABORT, 'retired provider connection cannot be resurrected'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_identity_guard
        BEFORE UPDATE OF backend_type, profile, endpoint, credential_source,
            credential_reference ON provider_connections
        WHEN (
            NEW.backend_type IS NOT OLD.backend_type
            OR NEW.profile IS NOT OLD.profile
            OR NEW.endpoint IS NOT OLD.endpoint
            OR NEW.credential_source IS NOT OLD.credential_source
            OR NEW.credential_reference IS NOT OLD.credential_reference
        )
        AND bots5_phase5_connection_identity_update_allowed(
            NEW.id, OLD.revision, NEW.revision,
            OLD.catalogue_revision, NEW.catalogue_revision
        ) = 0
        BEGIN SELECT RAISE(ABORT, 'provider connection identity requires core authority'); END
    """))


def _install_phase5_row_guards(connection=None) -> None:
    """Reject direct SQLite writes that would bypass Phase 5 value validation."""
    connection = connection or op.get_bind()
    forbidden_key_expression = secret_key_forbidden_sql_expression("key")

    def safe_json_object(column: str) -> str:
        return (
            f"json_valid({column}) = 1 AND json_type({column}) = 'object' AND NOT EXISTS ("
            f"SELECT 1 FROM json_tree({column}) WHERE key IS NOT NULL AND {forbidden_key_expression})"
        )

    def safe_capability_provenance(column: str) -> str:
        return (
            f"({safe_json_object(column)} AND NOT EXISTS ("
            f"SELECT 1 FROM json_each({column}) "
            "WHERE key NOT IN ('reason', 'field', 'catalogue_revision')) "
            f"AND (json_type({column}, '$.reason') IS NULL OR json_type({column}, '$.reason') IN ('text', 'null')) "
            f"AND (json_type({column}, '$.field') IS NULL OR json_type({column}, '$.field') IN ('text', 'null')) "
            f"AND (json_type({column}, '$.catalogue_revision') IS NULL OR json_type({column}, '$.catalogue_revision') IN ('integer', 'null')) "
            f"AND (json_type({column}, '$.reason') IS NULL OR length(json_extract({column}, '$.reason')) <= 256) "
            f"AND (json_type({column}, '$.field') IS NULL OR length(json_extract({column}, '$.field')) <= 256) "
            f"AND (json_type({column}, '$.catalogue_revision') IS NULL OR json_extract({column}, '$.catalogue_revision') >= 0))"
        )

    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_validate_insert
        BEFORE INSERT ON provider_connections
        WHEN bots5_valid_phase5_connection(
            NEW.name, NEW.name_key, NEW.backend_type, NEW.profile, NEW.endpoint,
            NEW.credential_source, NEW.credential_reference, NEW.enabled,
            NEW.retired, NEW.revision, NEW.catalogue_revision
        ) = 0
        BEGIN SELECT RAISE(ABORT, 'Phase 5 provider connection is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_validate_update
        BEFORE UPDATE OF name, name_key, backend_type, profile, endpoint,
            credential_source, credential_reference, enabled, retired,
            revision, catalogue_revision ON provider_connections
        WHEN bots5_valid_phase5_connection(
            NEW.name, NEW.name_key, NEW.backend_type, NEW.profile, NEW.endpoint,
            NEW.credential_source, NEW.credential_reference, NEW.enabled,
            NEW.retired, NEW.revision, NEW.catalogue_revision
        ) = 0
        BEGIN SELECT RAISE(ABORT, 'Phase 5 provider connection is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_provider_connection_catalogue_revision_guard
        BEFORE UPDATE OF catalogue_revision ON provider_connections
        WHEN NEW.catalogue_revision < OLD.catalogue_revision
        BEGIN SELECT RAISE(ABORT, 'Phase 5 provider connection catalogue revision regressed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_application_default_model_validate_insert
        BEFORE INSERT ON application_generation_config
        WHEN NEW.default_model_entry_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1
            FROM model_catalogue_entries AS m
            JOIN provider_connections AS p ON p.id = m.connection_id
            WHERE m.id = NEW.default_model_entry_id
              AND m.availability = 'available'
              AND p.enabled = 1
              AND p.retired = 0
          )
        BEGIN SELECT RAISE(ABORT, 'application default model must be available'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_application_default_model_validate_update
        BEFORE UPDATE OF default_model_entry_id ON application_generation_config
        WHEN NEW.default_model_entry_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1
            FROM model_catalogue_entries AS m
            JOIN provider_connections AS p ON p.id = m.connection_id
            WHERE m.id = NEW.default_model_entry_id
              AND m.availability = 'available'
              AND p.enabled = 1
              AND p.retired = 0
          )
        BEGIN SELECT RAISE(ABORT, 'application default model must be available'); END
    """))
    for table_name, temperature_required, max_output_required in (
        ("application_generation_config", 1, 1),
        ("model_generation_config", 0, 0),
        ("chat_model_generation_config", 0, 0),
    ):
        trigger_suffix = table_name.removesuffix("_generation_config")
        connection.execute(sa.text(f"""
            CREATE TRIGGER phase5_{trigger_suffix}_settings_validate_insert
            BEFORE INSERT ON {table_name}
            WHEN bots5_valid_phase5_settings(
                NEW.temperature, NEW.max_output_tokens, NEW.reasoning_effort,
                NEW.timeout_seconds, {temperature_required}, {max_output_required}
            ) = 0
            OR typeOf(NEW.revision) <> 'integer'
            OR NEW.revision < 1
            OR bots5_valid_timestamp(NEW.updated_at) = 0
            BEGIN SELECT RAISE(ABORT, 'Phase 5 generation settings are malformed'); END
        """))
        connection.execute(sa.text(f"""
            CREATE TRIGGER phase5_{trigger_suffix}_settings_validate_update
            BEFORE UPDATE OF temperature, max_output_tokens, reasoning_effort,
                timeout_seconds, revision, updated_at ON {table_name}
            WHEN bots5_valid_phase5_settings(
                NEW.temperature, NEW.max_output_tokens, NEW.reasoning_effort,
                NEW.timeout_seconds, {temperature_required}, {max_output_required}
            ) = 0
            OR typeOf(NEW.revision) <> 'integer'
            OR NEW.revision < 1
            OR bots5_valid_timestamp(NEW.updated_at) = 0
            BEGIN SELECT RAISE(ABORT, 'Phase 5 generation settings are malformed'); END
        """))
    connection.execute(sa.text(f"""
        CREATE TRIGGER phase5_model_catalogue_metadata_validate_insert
        BEFORE INSERT ON model_catalogue_entries
        WHEN NOT ({safe_json_object('NEW.metadata_json')})
        BEGIN SELECT RAISE(ABORT, 'Phase 5 model catalogue metadata is malformed'); END
    """))
    connection.execute(sa.text(f"""
        CREATE TRIGGER phase5_model_catalogue_metadata_validate_update
        BEFORE UPDATE OF metadata_json ON model_catalogue_entries
        WHEN NOT ({safe_json_object('NEW.metadata_json')})
        BEGIN SELECT RAISE(ABORT, 'Phase 5 model catalogue metadata is malformed'); END
    """))
    connection.execute(sa.text(f"""
        CREATE TRIGGER phase5_capability_fact_provenance_validate_insert
        BEFORE INSERT ON capability_facts
        WHEN NOT ({safe_capability_provenance('NEW.provenance_json')})
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability provenance is malformed'); END
    """))
    connection.execute(sa.text(f"""
        CREATE TRIGGER phase5_capability_fact_provenance_validate_update
        BEFORE UPDATE OF provenance_json ON capability_facts
        WHEN NOT ({safe_capability_provenance('NEW.provenance_json')})
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability provenance is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_fact_truth_validate_insert
        BEFORE INSERT ON capability_facts
        WHEN NEW.source = 'manual'
          OR NEW.capability_key NOT IN (
              'generation.streaming', 'request.temperature', 'request.max_output_tokens',
              'request.reasoning_effort.none', 'limits.context_tokens', 'limits.output_tokens',
              'telemetry.usage', 'telemetry.reasoning_tokens', 'telemetry.cost',
              'telemetry.request_id', 'telemetry.returned_model'
          )
          OR (NEW.source_revision IS NOT NULL AND (
              typeof(NEW.source_revision) <> 'integer' OR NEW.source_revision < 0
          ))
          OR (NEW.source IN ('confirmed_endpoint', 'provider_metadata') AND NEW.source_revision IS NULL)
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.observed_at) IS NOT NEW.observed_at
          OR (NEW.value IS NOT NULL AND (
              typeof(NEW.value) <> 'integer' OR NEW.value < 0
          ))
          OR (NEW.state IN ('unsupported', 'unknown') AND NEW.value IS NOT NULL)
          OR (NEW.capability_key NOT IN ('request.max_output_tokens', 'limits.context_tokens', 'limits.output_tokens')
              AND NEW.value IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability fact truth is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_fact_truth_validate_update
        BEFORE UPDATE OF capability_key, state, source, source_revision, value, observed_at ON capability_facts
        WHEN NEW.source = 'manual'
          OR NEW.capability_key NOT IN (
              'generation.streaming', 'request.temperature', 'request.max_output_tokens',
              'request.reasoning_effort.none', 'limits.context_tokens', 'limits.output_tokens',
              'telemetry.usage', 'telemetry.reasoning_tokens', 'telemetry.cost',
              'telemetry.request_id', 'telemetry.returned_model'
          )
          OR (NEW.source_revision IS NOT NULL AND (
              typeof(NEW.source_revision) <> 'integer' OR NEW.source_revision < 0
          ))
          OR (NEW.source IN ('confirmed_endpoint', 'provider_metadata') AND NEW.source_revision IS NULL)
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.observed_at) IS NOT NEW.observed_at
          OR (NEW.value IS NOT NULL AND (
              typeof(NEW.value) <> 'integer' OR NEW.value < 0
          ))
          OR (NEW.state IN ('unsupported', 'unknown') AND NEW.value IS NOT NULL)
          OR (NEW.capability_key NOT IN ('request.max_output_tokens', 'limits.context_tokens', 'limits.output_tokens')
              AND NEW.value IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability fact truth is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_override_truth_validate_insert
        BEFORE INSERT ON capability_overrides
        WHEN NEW.capability_key NOT IN (
              'generation.streaming', 'request.temperature', 'request.max_output_tokens',
              'request.reasoning_effort.none', 'limits.context_tokens', 'limits.output_tokens',
              'telemetry.usage', 'telemetry.reasoning_tokens', 'telemetry.cost',
              'telemetry.request_id', 'telemetry.returned_model'
          )
          OR typeOf(NEW.revision) <> 'integer'
          OR NEW.revision < 1
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at
          OR (NEW.value IS NOT NULL AND (
              typeof(NEW.value) <> 'integer' OR NEW.value < 0
          ))
          OR (NEW.state IN ('unsupported', 'unknown') AND NEW.value IS NOT NULL)
          OR (NEW.capability_key NOT IN ('request.max_output_tokens', 'limits.context_tokens', 'limits.output_tokens')
              AND NEW.value IS NOT NULL)
          OR (NEW.reason IS NOT NULL AND (
              typeof(NEW.reason) <> 'text' OR length(NEW.reason) > 256
          ))
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability override truth is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_override_truth_validate_update
        BEFORE UPDATE OF capability_key, state, value, reason, revision, updated_at ON capability_overrides
        WHEN NEW.capability_key NOT IN (
              'generation.streaming', 'request.temperature', 'request.max_output_tokens',
              'request.reasoning_effort.none', 'limits.context_tokens', 'limits.output_tokens',
              'telemetry.usage', 'telemetry.reasoning_tokens', 'telemetry.cost',
              'telemetry.request_id', 'telemetry.returned_model'
          )
          OR typeOf(NEW.revision) <> 'integer'
          OR NEW.revision < 1
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.updated_at) IS NOT NEW.updated_at
          OR (NEW.value IS NOT NULL AND (
              typeof(NEW.value) <> 'integer' OR NEW.value < 0
          ))
          OR (NEW.state IN ('unsupported', 'unknown') AND NEW.value IS NOT NULL)
          OR (NEW.capability_key NOT IN ('request.max_output_tokens', 'limits.context_tokens', 'limits.output_tokens')
              AND NEW.value IS NOT NULL)
          OR (NEW.reason IS NOT NULL AND (
              typeof(NEW.reason) <> 'text' OR length(NEW.reason) > 256
          ))
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability override truth is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_observation_truth_validate_insert
        BEFORE INSERT ON capability_observations
        WHEN NEW.capability_key NOT IN (
              'generation.streaming', 'request.temperature', 'request.max_output_tokens',
              'request.reasoning_effort.none', 'limits.context_tokens', 'limits.output_tokens',
              'telemetry.usage', 'telemetry.reasoning_tokens', 'telemetry.cost',
              'telemetry.request_id', 'telemetry.returned_model'
          )
          OR NEW.observed_state NOT IN ('supported', 'unsupported', 'unknown')
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.observed_at) IS NOT NEW.observed_at
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability observation truth is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_observation_truth_validate_update
        BEFORE UPDATE OF capability_key, observed_state, observed_at ON capability_observations
        WHEN NEW.capability_key NOT IN (
              'generation.streaming', 'request.temperature', 'request.max_output_tokens',
              'request.reasoning_effort.none', 'limits.context_tokens', 'limits.output_tokens',
              'telemetry.usage', 'telemetry.reasoning_tokens', 'telemetry.cost',
              'telemetry.request_id', 'telemetry.returned_model'
          )
          OR NEW.observed_state NOT IN ('supported', 'unsupported', 'unknown')
          OR strftime('%Y-%m-%dT%H:%M:%fZ', NEW.observed_at) IS NOT NEW.observed_at
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability observation truth is malformed'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_fact_identity_insert
        BEFORE INSERT ON capability_facts
        WHEN EXISTS (
          SELECT 1 FROM capability_facts AS f
          WHERE f.model_entry_id = NEW.model_entry_id
            AND f.capability_key = NEW.capability_key
            AND f.source = NEW.source
            AND (f.source_revision IS NEW.source_revision OR f.source_revision = NEW.source_revision)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability fact identity already exists'); END
    """))
    connection.execute(sa.text("""
        CREATE TRIGGER phase5_capability_fact_identity_update
        BEFORE UPDATE OF model_entry_id, capability_key, source, source_revision ON capability_facts
        WHEN EXISTS (
          SELECT 1 FROM capability_facts AS f
          WHERE f.id <> OLD.id
            AND f.model_entry_id = NEW.model_entry_id
            AND f.capability_key = NEW.capability_key
            AND f.source = NEW.source
            AND (f.source_revision IS NEW.source_revision OR f.source_revision = NEW.source_revision)
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 5 capability fact identity already exists'); END
    """))


def _seed_fake_state(connection) -> None:
    now = _now()
    connection.execute(
        sa.text(
            "INSERT INTO provider_connections "
            "(id, name, name_key, backend_type, profile, endpoint, credential_source, "
            "credential_reference, enabled, retired, revision, catalogue_revision, created_at, updated_at) "
            "VALUES (:id, :name, :name_key, 'fake', 'generic', NULL, 'none', NULL, 1, 0, 1, 1, :now, :now)"
        ),
        {"id": FAKE_CONNECTION_ID, "name": "Built-in fake", "name_key": "built-in fake", "now": now},
    )
    connection.execute(
        sa.text(
            "INSERT INTO model_catalogue_entries "
            "(id, connection_id, provider_model_id, display_name, origin, availability, "
            "discovery_revision, discovered_at, metadata_json, revision, created_at, updated_at) "
            "VALUES (:id, :connection_id, 'fake-v0.1', 'fake-v0.1', 'manual', 'available', "
            "1, :now, '{}', 1, :now, :now)"
        ),
        {"id": FAKE_MODEL_ENTRY_ID, "connection_id": FAKE_CONNECTION_ID, "now": now},
    )
    connection.execute(
        sa.text(
            "INSERT INTO application_generation_config "
            "(id, default_model_entry_id, temperature, max_output_tokens, reasoning_effort, "
            "timeout_seconds, revision, updated_at) VALUES (1, :model, '0.0', 1024, NULL, NULL, 1, :now)"
        ),
        {"model": FAKE_MODEL_ENTRY_ID, "now": now},
    )
    for key, state, value in (
        ("generation.streaming", "supported", None),
        ("request.temperature", "supported", None),
        ("request.max_output_tokens", "supported", 16384),
        ("request.reasoning_effort.none", "supported", None),
        ("telemetry.request_id", "supported", None),
        ("telemetry.returned_model", "supported", None),
    ):
        connection.execute(
            sa.text(
                "INSERT INTO capability_facts "
                "(id, model_entry_id, capability_key, state, source, source_revision, value, provenance_json, observed_at) "
                "VALUES (:id, :model, :key, :state, 'trusted_registry', 1, :value, '{}', :now)"
            ),
            {"id": f"{FAKE_MODEL_ENTRY_ID}-cap-{key.replace('.', '-')}", "model": FAKE_MODEL_ENTRY_ID, "key": key, "state": state, "value": value, "now": now},
        )


def upgrade() -> None:
    connection = op.get_bind()
    op.add_column("generation_attempts", sa.Column("connection_id", sa.String(length=64), nullable=True))
    op.add_column("generation_attempts", sa.Column("model_entry_id", sa.String(length=64), nullable=True))
    op.create_index("ix_generation_attempts_connection_id", "generation_attempts", ["connection_id"])
    op.create_index("ix_generation_attempts_model_entry_id", "generation_attempts", ["model_entry_id"])
    _create_tables()
    _install_attempt_attribution_guards()
    _install_phase5_row_guards(connection)
    _seed_fake_state(connection)


def downgrade() -> None:
    raise RuntimeError("Phase 5 provider/model configuration cannot be downgraded")
