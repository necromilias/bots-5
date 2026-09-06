from __future__ import annotations

import json
import re
import sqlite3
import tempfile

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, text, update
from uuid6 import uuid7

from bots5.core.errors import RevisionConflict, StateError
from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.models import (
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
    WorkspaceWindowState,
)
from bots5.domain.provider import (
    CAPABILITY_KEYS,
    CATALOGUE_REFRESH_FAILURE_MESSAGES,
    CapabilitySource,
    CapabilityState,
    CatalogueRefreshFailureClass,
    CatalogueRefreshStatus,
    validate_capability_value,
)

from .schema import (
    application_generation_config,
    capability_facts,
    capability_observations,
    capability_overrides,
    chat_model_generation_config,
    chat_model_selection,
    catalogue_refresh_state,
    chats,
    generation_attempts,
    messages,
    model_catalogue_entries,
    model_generation_config,
    provider_connections,
    workspace_windows,
)
from .phase3_validation import (
    PHASE3_BACKEND_ID,
    is_phase3_record,
    validate_remote_outcome_transition,
    validate_outcome_fields,
    validate_request_snapshot,
)
from .transition_guard import (
    arm_transition,
    clear_transition,
    install_transition_guard,
)
from .phase5_store import (
    Phase5StoreMixin,
    _capability_provenance,
    _connection,
    _json_object,
    _model,
    _settings,
)


_MESSAGE_STATES = {state.value for state in MessageState}
_MESSAGE_TERMINAL_STATES = {
    MessageState.COMPLETE,
    MessageState.INCOMPLETE,
    MessageState.TRUNCATED,
    MessageState.FAILED,
    MessageState.ABORTED,
}
_ATTEMPT_TERMINAL_STATES = {
    AttemptState.COMPLETE,
    AttemptState.INCOMPLETE,
    AttemptState.FAILED,
    AttemptState.ABORTED,
}
_ATTEMPT_TO_MESSAGE_STATE = {
    AttemptState.COMPLETE: MessageState.COMPLETE,
    AttemptState.INCOMPLETE: MessageState.INCOMPLETE,
    AttemptState.FAILED: MessageState.FAILED,
    AttemptState.ABORTED: MessageState.ABORTED,
}
_PHASE3_COLUMN_TYPES = {
    "provider_id": "VARCHAR(64)",
    "returned_model": "TEXT",
    "request_id": "TEXT",
    "finish_reason": "VARCHAR(128)",
    "prompt_tokens": "INTEGER",
    "completion_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "known_cost_usd": "TEXT",
    "remote_outcome_unknown": "BOOLEAN",
}
_PHASE4_WORKSPACE_COLUMN_TYPES = {
    "window_id": "VARCHAR(128)",
    "ordinal": "INTEGER",
    "geometry_json": "TEXT",
    "selected_chat_id": "VARCHAR(64)",
    "rail_collapsed": "BOOLEAN",
    "restore_open": "BOOLEAN",
    "updated_at": "VARCHAR(40)",
}
_PHASE4_WORKSPACE_NOT_NULL = {
    "ordinal",
    "rail_collapsed",
    "restore_open",
    "updated_at",
}
_PHASE4_ACTIVE_INDEX_PREDICATE = re.compile(
    r"\(?\s*(?i:state|\"state\"|`state`|\[state\])\s*=\s*'running'\s*\)?",
)
_PHASE5_TABLES = {
    "catalogue_refresh_state",
    "provider_connections",
    "model_catalogue_entries",
    "capability_facts",
    "capability_overrides",
    "capability_observations",
    "application_generation_config",
    "model_generation_config",
    "chat_model_generation_config",
    "chat_model_selection",
}
_PHASE5_ATTEMPT_COLUMN_TYPES = {
    "connection_id": "VARCHAR(64)",
    "model_entry_id": "VARCHAR(64)",
}
_PHASE5_NOT_NULL_COLUMNS = {
    "provider_connections": frozenset({
        "id", "name", "name_key", "backend_type", "profile", "credential_source",
        "enabled", "retired", "revision", "catalogue_revision", "created_at", "updated_at",
    }),
    "model_catalogue_entries": frozenset({
        "id", "connection_id", "provider_model_id", "display_name", "origin", "availability",
        "metadata_json", "revision", "created_at", "updated_at",
    }),
    "capability_facts": frozenset({
        "id", "model_entry_id", "capability_key", "state", "source", "provenance_json", "observed_at",
    }),
    "capability_overrides": frozenset({
        "model_entry_id", "capability_key", "state", "revision", "updated_at",
    }),
    "capability_observations": frozenset({
        "id", "model_entry_id", "capability_key", "observed_state", "detail", "observed_at",
    }),
    "application_generation_config": frozenset({
        "id", "temperature", "max_output_tokens", "revision", "updated_at",
    }),
    "model_generation_config": frozenset({
        "model_entry_id", "revision", "updated_at",
    }),
    "chat_model_generation_config": frozenset({
        "chat_id", "model_entry_id", "revision", "updated_at",
    }),
    "chat_model_selection": frozenset({
        "chat_id", "selection_required", "revision", "updated_at",
    }),
    "catalogue_refresh_state": frozenset({
        "connection_id", "status", "refresh_revision", "updated_at",
    }),
}
_PHASE5_PRIMARY_KEYS = {
    "catalogue_refresh_state": ("connection_id",),
    "provider_connections": ("id",),
    "model_catalogue_entries": ("id",),
    "capability_facts": ("id",),
    "capability_overrides": ("model_entry_id", "capability_key"),
    "capability_observations": ("id",),
    "application_generation_config": ("id",),
    "model_generation_config": ("model_entry_id",),
    "chat_model_generation_config": ("chat_id", "model_entry_id"),
    "chat_model_selection": ("chat_id",),
}
_PHASE5_FOREIGN_KEYS = {
    "catalogue_refresh_state": (("connection_id", "provider_connections", "id", "RESTRICT"),),
    "model_catalogue_entries": (("connection_id", "provider_connections", "id", "RESTRICT"),),
    "capability_facts": (("model_entry_id", "model_catalogue_entries", "id", "CASCADE"),),
    "capability_overrides": (("model_entry_id", "model_catalogue_entries", "id", "CASCADE"),),
    "capability_observations": (("model_entry_id", "model_catalogue_entries", "id", "CASCADE"),),
    "application_generation_config": (("default_model_entry_id", "model_catalogue_entries", "id", "RESTRICT"),),
    "model_generation_config": (("model_entry_id", "model_catalogue_entries", "id", "CASCADE"),),
    "chat_model_generation_config": (
        ("chat_id", "chats", "id", "CASCADE"),
        ("model_entry_id", "model_catalogue_entries", "id", "CASCADE"),
    ),
    "chat_model_selection": (
        ("chat_id", "chats", "id", "CASCADE"),
        ("model_entry_id", "model_catalogue_entries", "id", "RESTRICT"),
    ),
}
_PHASE5_TRIGGER_MARKERS = {
    "phase5_attempt_attribution_insert": (
        "before insert on generation_attempts",
        "$.snapshot_version",
        "phase 5 attempt attribution is inconsistent",
    ),
    "phase5_attempt_attribution_update": (
        "before update of connection_id, model_entry_id on generation_attempts",
        "$.snapshot_version",
        "phase 5 attempt attribution is immutable",
    ),
    "phase5_model_catalogue_delete_referenced": (
        "before delete on model_catalogue_entries",
        "referenced phase 5 model catalogue entry is immutable",
    ),
    "phase5_model_catalogue_identity_referenced": (
        "before update of connection_id, provider_model_id on model_catalogue_entries",
        "referenced phase 5 model identity is immutable",
    ),
    "phase5_provider_connection_delete_referenced": (
        "before delete on provider_connections",
        "referenced phase 5 provider connection is immutable",
    ),
    "phase5_provider_connection_retirement_guard": (
        "before update of retired on provider_connections",
        "retiring the application-default connection requires a replacement",
    ),
    "phase5_provider_connection_resurrection_guard": (
        "before update of retired on provider_connections",
        "retired provider connection cannot be resurrected",
    ),
    "phase5_provider_connection_identity_guard": (
        "before update of backend_type, profile, endpoint, credential_source,",
        "bots5_phase5_connection_identity_update_allowed",
        "provider connection identity requires core authority",
    ),
    "generation_attempt_phase5_completion_insert": (
        "before insert on generation_attempts",
        "$.snapshot_version",
        "phase 5 generation finish_reason is inconsistent with state",
    ),
    "generation_attempt_phase5_completion_update": (
        "before update of state, finish_reason, request_snapshot on generation_attempts",
        "$.snapshot_version",
        "phase 5 generation finish_reason is inconsistent with state",
    ),
    "phase5_model_catalogue_metadata_validate_insert": (
        "before insert on model_catalogue_entries",
        "json_tree(new.metadata_json)",
        "phase 5 model catalogue metadata is malformed",
    ),
    "phase5_model_catalogue_metadata_validate_update": (
        "before update of metadata_json on model_catalogue_entries",
        "json_tree(new.metadata_json)",
        "phase 5 model catalogue metadata is malformed",
    ),
    "phase5_capability_fact_provenance_validate_insert": (
        "before insert on capability_facts",
        "json_tree(new.provenance_json)",
        "phase 5 capability provenance is malformed",
    ),
    "phase5_capability_fact_provenance_validate_update": (
        "before update of provenance_json on capability_facts",
        "json_tree(new.provenance_json)",
        "phase 5 capability provenance is malformed",
    ),
    "phase5_capability_fact_truth_validate_insert": (
        "before insert on capability_facts",
        "phase 5 capability fact truth is malformed",
    ),
    "phase5_capability_fact_truth_validate_update": (
        "before update of capability_key, state, source, source_revision, value, observed_at on capability_facts",
        "phase 5 capability fact truth is malformed",
    ),
    "phase5_capability_override_truth_validate_insert": (
        "before insert on capability_overrides",
        "phase 5 capability override truth is malformed",
    ),
    "phase5_capability_override_truth_validate_update": (
        "before update of capability_key, state, value, reason, revision, updated_at on capability_overrides",
        "phase 5 capability override truth is malformed",
    ),
    "phase5_capability_observation_truth_validate_insert": (
        "before insert on capability_observations",
        "phase 5 capability observation truth is malformed",
    ),
    "phase5_capability_observation_truth_validate_update": (
        "before update of capability_key, observed_state, observed_at on capability_observations",
        "phase 5 capability observation truth is malformed",
    ),
    "phase5_capability_fact_identity_insert": (
        "before insert on capability_facts",
        "phase 5 capability fact identity already exists",
    ),
    "phase5_capability_fact_identity_update": (
        "before update of model_entry_id, capability_key, source, source_revision on capability_facts",
        "phase 5 capability fact identity already exists",
    ),
    "phase5_provider_connection_validate_insert": (
        "before insert on provider_connections",
        "bots5_valid_phase5_connection",
    ),
    "phase5_provider_connection_validate_update": (
        "before update of name, name_key, backend_type, profile, endpoint,",
        "bots5_valid_phase5_connection",
    ),
    "phase5_provider_connection_catalogue_revision_guard": (
        "before update of catalogue_revision on provider_connections",
        "new.catalogue_revision > old.catalogue_revision",
        "bots5_phase5_catalogue_refresh_allowed",
        "bots5_phase5_connection_identity_update_allowed",
        "provider connection catalogue revision requires core authority",
    ),
    "phase5_catalogue_refresh_state_insert": (
        "after insert on provider_connections",
        "catalogue_refresh_state",
        "status, refresh_revision, failure_class, failure_message, updated_at",
    ),
    "phase5_catalogue_refresh_state_delete_guard": (
        "before delete on catalogue_refresh_state",
        "phase 5 catalogue refresh state is immutable",
    ),
    "phase5_catalogue_refresh_revision_guard": (
        "before update on catalogue_refresh_state",
        "bots5_phase5_catalogue_refresh_allowed",
        "phase 5 catalogue refresh state requires core authority",
    ),
    "phase5_application_default_model_validate_insert": (
        "before insert on application_generation_config",
        "application default model must be available",
    ),
    "phase5_application_default_model_validate_update": (
        "before update of default_model_entry_id on application_generation_config",
        "application default model must be available",
    ),
    "phase5_application_settings_validate_insert": (
        "before insert on application_generation_config",
        "bots5_valid_phase5_settings",
    ),
    "phase5_application_settings_validate_update": (
        "before update of temperature, max_output_tokens, reasoning_effort,",
        "bots5_valid_phase5_settings",
        "revision, updated_at on",
    ),
    "phase5_model_settings_validate_insert": (
        "before insert on model_generation_config",
        "bots5_valid_phase5_settings",
    ),
    "phase5_model_settings_validate_update": (
        "before update of temperature, max_output_tokens, reasoning_effort,",
        "bots5_valid_phase5_settings",
        "revision, updated_at on",
    ),
    "phase5_chat_model_settings_validate_insert": (
        "before insert on chat_model_generation_config",
        "bots5_valid_phase5_settings",
    ),
    "phase5_chat_model_settings_validate_update": (
        "before update of temperature, max_output_tokens, reasoning_effort,",
        "bots5_valid_phase5_settings",
        "revision, updated_at on",
    ),
}
_PHASE5_CHECK_CONSTRAINTS = {
    "catalogue_refresh_state": (
        "ck_catalogue_refresh_status",
        "ck_catalogue_refresh_revision",
        "ck_catalogue_refresh_failure_class",
        "ck_catalogue_refresh_failure_message",
        "ck_catalogue_refresh_failure_consistency",
        "ck_catalogue_refresh_revision_consistency",
        "ck_catalogue_refresh_failure_diagnostic",
    ),
    "provider_connections": (
        "ck_provider_connection_backend",
        "ck_provider_connection_profile",
        "ck_provider_connection_credential_source",
        "ck_provider_connection_openrouter_auth",
        "ck_provider_connection_revisions",
    ),
    "model_catalogue_entries": (
        "ck_model_catalogue_origin",
        "ck_model_catalogue_availability",
        "ck_model_catalogue_revision",
    ),
    "capability_facts": (
        "ck_capability_fact_state",
        "ck_capability_fact_source",
        "ck_capability_fact_source_revision",
    ),
    "capability_overrides": ("ck_capability_override_state", "ck_capability_override_reason"),
    "capability_observations": ("ck_capability_observation_state",),
    "application_generation_config": (
        "ck_application_generation_config_singleton",
        "ck_application_generation_config_output",
        "ck_application_generation_config_reasoning",
    ),
    "chat_model_selection": ("ck_chat_model_selection_consistency",),
}

_PHASE5_CHECK_EXPRESSIONS = {
    "ck_catalogue_refresh_status": "status IN ('never', 'succeeded', 'failed')",
    "ck_catalogue_refresh_revision": "refresh_revision >= 0",
    "ck_catalogue_refresh_failure_class": "failure_class IS NULL OR failure_class IN ('transport', 'timeout', 'provider_http', 'protocol', 'unknown')",
    "ck_catalogue_refresh_failure_message": "failure_message IS NULL OR failure_message IN ('provider is unreachable', 'provider discovery timed out', 'provider returned an HTTP error', 'provider returned an invalid model catalogue', 'provider discovery failed')",
    "ck_catalogue_refresh_failure_consistency": "(status = 'failed' AND failure_class IS NOT NULL AND failure_message IS NOT NULL) OR (status <> 'failed' AND failure_class IS NULL AND failure_message IS NULL)",
    "ck_catalogue_refresh_revision_consistency": "(status = 'never' AND refresh_revision = 0) OR (status <> 'never' AND refresh_revision > 0)",
    "ck_catalogue_refresh_failure_diagnostic": "(failure_class = 'transport' AND failure_message = 'provider is unreachable') OR (failure_class = 'timeout' AND failure_message = 'provider discovery timed out') OR (failure_class = 'provider_http' AND failure_message = 'provider returned an HTTP error') OR (failure_class = 'protocol' AND failure_message = 'provider returned an invalid model catalogue') OR (failure_class = 'unknown' AND failure_message = 'provider discovery failed') OR (failure_class IS NULL AND failure_message IS NULL)",
    "ck_provider_connection_backend": "backend_type IN ('fake', 'openai_compatible_http')",
    "ck_provider_connection_profile": "profile IN ('generic', 'openrouter')",
    "ck_provider_connection_credential_source": "credential_source IN ('none', 'environment', 'secret_service')",
    "ck_provider_connection_openrouter_auth": "profile <> 'openrouter' OR credential_source <> 'none'",
    "ck_provider_connection_revisions": "revision > 0 AND catalogue_revision >= 0",
    "ck_model_catalogue_origin": "origin IN ('manual', 'discovered', 'manual_confirmed')",
    "ck_model_catalogue_availability": "availability IN ('available', 'unavailable', 'stale', 'disconnected')",
    "ck_model_catalogue_revision": "revision > 0",
    "ck_capability_fact_state": "state IN ('supported', 'unsupported', 'unknown')",
    "ck_capability_fact_source": "source IN ('manual', 'confirmed_endpoint', 'provider_metadata', 'trusted_registry', 'heuristic', 'unknown')",
    "ck_capability_fact_source_revision": "source NOT IN ('confirmed_endpoint', 'provider_metadata') OR source_revision IS NOT NULL",
    "ck_capability_override_state": "state IN ('supported', 'unsupported', 'unknown')",
    "ck_capability_override_reason": "reason IS NULL OR (typeof(reason) = 'text' AND length(reason) <= 256)",
    "ck_capability_observation_state": "observed_state IN ('supported', 'unsupported', 'unknown')",
    "ck_application_generation_config_singleton": "id = 1",
    "ck_application_generation_config_output": "max_output_tokens > 0",
    "ck_application_generation_config_reasoning": "reasoning_effort IS NULL OR reasoning_effort = 'none'",
    "ck_chat_model_selection_consistency": "(selection_required = 1 AND model_entry_id IS NULL) OR (selection_required = 0 AND model_entry_id IS NOT NULL)",
}

_PHASE5_SCHEMA_OBJECTS = tuple(_PHASE5_CHECK_CONSTRAINTS) + tuple(_PHASE5_TRIGGER_MARKERS)


def _normalise_sql_fragment(value: str) -> str:
    # SQL keywords and identifiers are case-insensitive, but values inside
    # quoted literals are part of the closed contract and remain case-sensitive.
    parts = re.split(r"('(?:''|[^'])*')", value)
    return "".join(
        part if index % 2 else re.sub(r"\s+", "", part).casefold()
        for index, part in enumerate(parts)
    )


def _check_expression(table_sql: str, constraint: str) -> str | None:
    """Extract one named CHECK expression without treating a substring as proof."""
    match = re.search(
        rf"\bconstraint\s+{re.escape(constraint)}\s+check\s*\(",
        table_sql,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    opening = table_sql.find("(", match.start(), match.end())
    depth = 0
    index = opening
    in_string = False
    while index < len(table_sql):
        character = table_sql[index]
        if character == "'":
            if in_string and index + 1 < len(table_sql) and table_sql[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string and character == "(":
            depth += 1
        elif not in_string and character == ")":
            depth -= 1
            if depth == 0:
                return table_sql[opening + 1:index]
        index += 1
    return None


@cache
def _canonical_phase5_schema_sql() -> dict[str, str]:
    """Build the migration-owned Phase 5 DDL used for stamped-schema checks."""
    from alembic import command
    from alembic.config import Config

    with tempfile.TemporaryDirectory(prefix="bots5-phase5-schema-") as directory:
        database = Path(directory) / "schema.sqlite3"
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(__file__).with_name("migrations")),
        )
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
        command.upgrade(config, "0008_catalogue_refresh_outcomes")
        with sqlite3.connect(database) as reference:
            rows = reference.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE name IN ({})".format(",".join("?" for _ in _PHASE5_SCHEMA_OBJECTS)),
                _PHASE5_SCHEMA_OBJECTS,
            ).fetchall()
    return {str(name): _normalise_sql_fragment(str(sql or "")) for name, sql in rows}


def _validate_request_snapshot(
    attempt: GenerationAttempt,
    user_message_content: object | None = None,
    *,
    phase3: bool | None = None,
) -> None:
    validate_request_snapshot(
        attempt_id=attempt.id,
        chat_id=attempt.chat_id,
        user_message_id=attempt.user_message_id,
        backend_id=attempt.backend_id,
        model=attempt.model,
        provider_id=attempt.provider_id,
        request_snapshot=attempt.request_snapshot,
        user_message_content=user_message_content,
        phase3=phase3,
        error_type=StateError,
    )


def _validate_attempt_outcome(
    attempt: GenerationAttempt,
    *,
    phase3: bool | None = None,
    phase5: bool | None = None,
) -> None:
    if phase3 is None:
        phase3 = _is_persisted_phase3_attempt(attempt)
    validate_outcome_fields(
        state=attempt.state.value,
        provider_id=attempt.provider_id,
        finish_reason=attempt.finish_reason,
        prompt_tokens=attempt.prompt_tokens,
        completion_tokens=attempt.completion_tokens,
        reasoning_tokens=attempt.reasoning_tokens,
        total_tokens=attempt.total_tokens,
        known_cost_usd=attempt.known_cost_usd,
        remote_outcome_unknown=attempt.remote_outcome_unknown,
        returned_model=attempt.returned_model,
        request_id=attempt.request_id,
        outcome_error_type=attempt.error_type,
        outcome_error_message=attempt.error_message,
        phase3=phase3,
        phase5=(_is_persisted_phase5_attempt(attempt) if phase5 is None else phase5),
        error_type=StateError,
    )


def _is_persisted_phase3_attempt(attempt: GenerationAttempt) -> bool:
    try:
        snapshot = json.loads(attempt.request_snapshot)
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    return is_phase3_record(
        backend_id=attempt.backend_id,
        provider_id=attempt.provider_id,
        snapshot=snapshot,
        include_backend_marker=False,
    )


def _is_persisted_phase5_attempt(attempt: GenerationAttempt) -> bool:
    try:
        snapshot = json.loads(attempt.request_snapshot)
    except (TypeError, ValueError):
        return False
    return isinstance(snapshot, dict) and snapshot.get("snapshot_version") == 2


def _engine(database: Path) -> Engine:
    engine = create_engine(f"sqlite:///{database}", future=True)

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
        install_transition_guard(dbapi_connection, connection_record)

    return engine


def _chat(row) -> Chat:
    return Chat(
        id=row.id,
        title=row.title,
        created_at=parse_utc(row.created_at),
        updated_at=parse_utc(row.updated_at),
        head_message_id=row.head_message_id,
        revision=int(row.revision),
    )


def _message(row) -> Message:
    return Message(
        id=row.id,
        chat_id=row.chat_id,
        role=MessageRole(row.role),
        state=MessageState(row.state),
        content=row.content,
        sequence=row.sequence,
        created_at=parse_utc(row.created_at),
        parent_id=row.parent_id,
        lineage_id=row.lineage_id,
        revision=int(row.revision),
        supersedes_id=row.supersedes_id,
    )


def _attempt(row, user_message_content: object | None = None) -> GenerationAttempt:
    token_values = {}
    for field in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        value = getattr(row, field)
        token_values[field] = value
    cost = None
    if row.known_cost_usd is not None:
        try:
            cost = Decimal(str(row.known_cost_usd))
        except (InvalidOperation, ValueError):
            raise StateError(f"generation attempt has invalid cost: {row.id}") from None
    attempt = GenerationAttempt(
        id=row.id,
        chat_id=row.chat_id,
        user_message_id=row.user_message_id,
        assistant_message_id=row.assistant_message_id,
        backend_id=row.backend_id,
        model=row.model,
        state=AttemptState(row.state),
        request_snapshot=row.request_snapshot,
        started_at=parse_utc(row.started_at),
        ended_at=None if row.ended_at is None else parse_utc(row.ended_at),
        error_type=row.error_type,
        error_message=row.error_message,
        provider_id=row.provider_id,
        returned_model=row.returned_model,
        request_id=row.request_id,
        finish_reason=row.finish_reason,
        prompt_tokens=token_values["prompt_tokens"],
        completion_tokens=token_values["completion_tokens"],
        reasoning_tokens=token_values["reasoning_tokens"],
        total_tokens=token_values["total_tokens"],
        known_cost_usd=cost,
        remote_outcome_unknown=(
            None if row.remote_outcome_unknown is None else bool(row.remote_outcome_unknown)
        ),
        connection_id=getattr(row, "connection_id", None),
        model_entry_id=getattr(row, "model_entry_id", None),
    )
    persisted_phase3 = _is_persisted_phase3_attempt(attempt)
    _validate_request_snapshot(
        attempt,
        user_message_content,
        phase3=persisted_phase3,
    )
    _validate_attempt_outcome(attempt, phase3=persisted_phase3)
    return attempt


def _message_values(message: Message) -> dict[str, object]:
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "parent_id": message.parent_id,
        "sequence": message.sequence,
        "role": message.role.value,
        "state": message.state.value,
        "content": message.content,
        "created_at": utc_iso(message.created_at),
        "lineage_id": message.lineage_id or message.id,
        "revision": message.revision,
        "supersedes_id": message.supersedes_id,
    }


def _workspace_window(row) -> WorkspaceWindowState:
    mapping = row._mapping
    geometry_value = mapping["geometry_json"]
    geometry: tuple[int, int, int, int] | None
    if geometry_value is None:
        geometry = None
    else:
        try:
            decoded = json.loads(geometry_value)
        except (TypeError, ValueError) as exc:
            raise StateError("workspace geometry is not valid JSON") from exc
        if (
            not isinstance(decoded, list)
            or len(decoded) != 4
            or not all(type(value) is int for value in decoded)
        ):
            raise StateError("workspace geometry must contain four integers")
        geometry = tuple(decoded)  # type: ignore[assignment]
    try:
        return WorkspaceWindowState(
            window_id=str(mapping["window_id"]),
            ordinal=int(mapping["ordinal"]),
            geometry=geometry,
            selected_chat_id=mapping["selected_chat_id"],
            rail_collapsed=bool(mapping["rail_collapsed"]),
            restore_open=bool(mapping["restore_open"]),
            updated_at=parse_utc(mapping["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("workspace window state is malformed") from exc


def _validate_phase4_schema(connection) -> None:
    table_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspace_windows'"
    ).first()
    if table_exists is None:
        raise RuntimeError("current Phase 4 schema is missing workspace_windows")

    workspace_columns = {
        row[1]: row
        for row in connection.exec_driver_sql("PRAGMA table_info(workspace_windows)").fetchall()
    }
    missing_columns = sorted(set(_PHASE4_WORKSPACE_COLUMN_TYPES) - set(workspace_columns))
    if missing_columns:
        raise RuntimeError(
            "current Phase 4 workspace_windows schema is missing columns: "
            + ", ".join(missing_columns)
        )
    wrong_columns = sorted(
        name
        for name, expected_type in _PHASE4_WORKSPACE_COLUMN_TYPES.items()
        if str(workspace_columns[name][2]).upper().replace(" ", "")
        != expected_type
    )
    if wrong_columns:
        details = ", ".join(
            f"{name}={workspace_columns[name][2]}" for name in wrong_columns
        )
        raise RuntimeError(
            "current Phase 4 workspace_windows columns have invalid declared types: "
            + details
        )
    wrong_nullability = sorted(
        name
        for name in _PHASE4_WORKSPACE_NOT_NULL
        if workspace_columns[name][3] != 1
    )
    if wrong_nullability:
        raise RuntimeError(
            "current Phase 4 workspace_windows columns must be non-null: "
            + ", ".join(wrong_nullability)
        )
    if workspace_columns["window_id"][5] != 1:
        raise RuntimeError("current Phase 4 workspace_windows must use window_id as its primary key")

    foreign_keys = connection.exec_driver_sql(
        "PRAGMA foreign_key_list(workspace_windows)"
    ).fetchall()
    if not any(
        row[2] == "chats"
        and row[3] == "selected_chat_id"
        and row[4] == "id"
        and str(row[6]).upper() == "SET NULL"
        for row in foreign_keys
    ):
        raise RuntimeError(
            "current Phase 4 workspace_windows is missing the selected_chat_id chat reference"
        )

    index_rows = connection.exec_driver_sql(
        "PRAGMA index_list(generation_attempts)"
    ).fetchall()
    active_index = next(
        (row for row in index_rows if row[1] == "ux_generation_attempts_active_chat"),
        None,
    )
    if active_index is None:
        raise RuntimeError("current Phase 4 schema is missing the active-chat uniqueness index")
    if active_index[2] != 1 or active_index[4] != 1:
        raise RuntimeError("current Phase 4 active-chat index must be unique and partial")
    index_columns = connection.exec_driver_sql(
        "PRAGMA index_info(ux_generation_attempts_active_chat)"
    ).fetchall()
    if [(row[0], row[2]) for row in index_columns] != [(0, "chat_id")]:
        raise RuntimeError(
            "current Phase 4 active-chat index must cover only generation_attempts.chat_id"
        )
    index_sql = connection.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'ux_generation_attempts_active_chat'"
        )
    ).scalar_one_or_none()
    normalized_index_sql = " ".join(str(index_sql or "").strip().rstrip(";").split())
    where_match = re.search(r"\bwhere\b(?P<predicate>.*)\Z", normalized_index_sql, re.IGNORECASE)
    predicate = where_match.group("predicate").strip() if where_match else ""
    if not _PHASE4_ACTIVE_INDEX_PREDICATE.fullmatch(predicate):
        raise RuntimeError(
            "current Phase 4 active-chat index must be filtered to running attempts"
        )


def _validate_phase5_schema(connection) -> None:
    canonical_schema = _canonical_phase5_schema_sql()
    current_schema = {
        str(name): _normalise_sql_fragment(str(sql or ""))
        for name, sql in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master "
            "WHERE name IN ({})".format(",".join("?" for _ in _PHASE5_SCHEMA_OBJECTS)),
            _PHASE5_SCHEMA_OBJECTS,
        ).fetchall()
    }
    for name in _PHASE5_SCHEMA_OBJECTS:
        if current_schema.get(name) != canonical_schema.get(name):
            kind = "trigger" if name in _PHASE5_TRIGGER_MARKERS else "table"
            raise RuntimeError(
                f"current Phase 5 schema {kind} is not migration-authoritative: {name}"
            )
    existing = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = sorted(_PHASE5_TABLES - existing)
    if missing_tables:
        raise RuntimeError(
            "current Phase 5 schema is missing tables: " + ", ".join(missing_tables)
        )
    attempt_columns = {
        row[1]: row
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(generation_attempts)"
        ).fetchall()
    }
    missing_attempt_columns = sorted(
        set(_PHASE5_ATTEMPT_COLUMN_TYPES) - set(attempt_columns)
    )
    if missing_attempt_columns:
        raise RuntimeError(
            "current Phase 5 generation_attempts schema is missing columns: "
            + ", ".join(missing_attempt_columns)
        )
    wrong_attempt_types = sorted(
        name
        for name, expected in _PHASE5_ATTEMPT_COLUMN_TYPES.items()
        if str(attempt_columns[name][2]).upper().replace(" ", "") != expected
    )
    if wrong_attempt_types:
        raise RuntimeError(
            "current Phase 5 attribution columns have invalid declared types: "
            + ", ".join(
                f"{name}={attempt_columns[name][2]}" for name in wrong_attempt_types
            )
        )
    non_nullable_attempt_columns = sorted(
        name for name in _PHASE5_ATTEMPT_COLUMN_TYPES if attempt_columns[name][3] != 0
    )
    if non_nullable_attempt_columns:
        raise RuntimeError(
            "current Phase 5 attribution columns must be nullable: "
            + ", ".join(non_nullable_attempt_columns)
        )
    required_columns = {
        "provider_connections": {
            "id": "VARCHAR(64)",
            "name": "TEXT",
            "name_key": "VARCHAR(256)",
            "backend_type": "VARCHAR(64)",
            "profile": "VARCHAR(64)",
            "endpoint": "TEXT",
            "credential_source": "VARCHAR(64)",
            "credential_reference": "TEXT",
            "enabled": "BOOLEAN",
            "retired": "BOOLEAN",
            "revision": "INTEGER",
            "catalogue_revision": "INTEGER",
            "created_at": "VARCHAR(40)",
            "updated_at": "VARCHAR(40)",
        },
        "catalogue_refresh_state": {
            "connection_id": "VARCHAR(64)",
            "status": "VARCHAR(32)",
            "refresh_revision": "INTEGER",
            "failure_class": "VARCHAR(32)",
            "failure_message": "TEXT",
            "updated_at": "VARCHAR(40)",
        },
        "model_catalogue_entries": {
            "id": "VARCHAR(64)",
            "connection_id": "VARCHAR(64)",
            "provider_model_id": "TEXT",
            "display_name": "TEXT",
            "origin": "VARCHAR(64)",
            "availability": "VARCHAR(64)",
            "discovery_revision": "INTEGER",
            "discovered_at": "VARCHAR(40)",
            "metadata_json": "TEXT",
            "revision": "INTEGER",
            "created_at": "VARCHAR(40)",
            "updated_at": "VARCHAR(40)",
        },
        "capability_facts": {
            "id": "VARCHAR(64)",
            "model_entry_id": "VARCHAR(64)",
            "capability_key": "VARCHAR(128)",
            "state": "VARCHAR(32)",
            "source": "VARCHAR(64)",
            "source_revision": "INTEGER",
            "value": "INTEGER",
            "provenance_json": "TEXT",
            "observed_at": "VARCHAR(40)",
        },
        "application_generation_config": {
            "id": "INTEGER",
            "default_model_entry_id": "VARCHAR(64)",
            "temperature": "TEXT",
            "max_output_tokens": "INTEGER",
            "reasoning_effort": "VARCHAR(32)",
            "timeout_seconds": "TEXT",
            "revision": "INTEGER",
            "updated_at": "VARCHAR(40)",
        },
        "capability_overrides": {
            "model_entry_id": "VARCHAR(64)",
            "capability_key": "VARCHAR(128)",
            "state": "VARCHAR(32)",
            "value": "INTEGER",
            "reason": "TEXT",
            "revision": "INTEGER",
            "updated_at": "VARCHAR(40)",
        },
        "capability_observations": {
            "id": "VARCHAR(64)",
            "model_entry_id": "VARCHAR(64)",
            "capability_key": "VARCHAR(128)",
            "observed_state": "VARCHAR(32)",
            "detail": "TEXT",
            "observed_at": "VARCHAR(40)",
        },
        "model_generation_config": {
            "model_entry_id": "VARCHAR(64)",
            "temperature": "TEXT",
            "max_output_tokens": "INTEGER",
            "reasoning_effort": "VARCHAR(32)",
            "timeout_seconds": "TEXT",
            "revision": "INTEGER",
            "updated_at": "VARCHAR(40)",
        },
        "chat_model_generation_config": {
            "chat_id": "VARCHAR(64)",
            "model_entry_id": "VARCHAR(64)",
            "temperature": "TEXT",
            "max_output_tokens": "INTEGER",
            "reasoning_effort": "VARCHAR(32)",
            "timeout_seconds": "TEXT",
            "revision": "INTEGER",
            "updated_at": "VARCHAR(40)",
        },
        "chat_model_selection": {
            "chat_id": "VARCHAR(64)",
            "model_entry_id": "VARCHAR(64)",
            "selection_required": "BOOLEAN",
            "revision": "INTEGER",
            "updated_at": "VARCHAR(40)",
        },
    }
    for table_name, columns in required_columns.items():
        info = {
            row[1]: row
            for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
        }
        missing = sorted(set(columns) - set(info))
        if missing:
            raise RuntimeError(
                f"current Phase 5 {table_name} schema is missing columns: "
                + ", ".join(missing)
            )
        wrong = sorted(
            name
            for name, expected in columns.items()
            if str(info[name][2]).upper().replace(" ", "") != expected
        )
        if wrong:
            raise RuntimeError(
                f"current Phase 5 {table_name} columns have invalid declared types: "
                + ", ".join(f"{name}={info[name][2]}" for name in wrong)
            )
        wrong_nullability = sorted(
            name
            for name in _PHASE5_NOT_NULL_COLUMNS[table_name]
            if info[name][3] != 1
        )
        if wrong_nullability:
            raise RuntimeError(
                f"current Phase 5 {table_name} columns have invalid nullability: "
                + ", ".join(wrong_nullability)
            )
        primary_key = tuple(
            row[1]
            for row in sorted(info.values(), key=lambda item: int(item[5] or 0))
            if int(row[5] or 0) > 0
        )
        if primary_key != _PHASE5_PRIMARY_KEYS[table_name]:
            raise RuntimeError(
                f"current Phase 5 {table_name} has an invalid primary key"
            )
        actual_foreign_keys = {
            (row[3], row[2], row[4], str(row[6]).upper())
            for row in connection.exec_driver_sql(
                f"PRAGMA foreign_key_list({table_name})"
            ).fetchall()
        }
        if actual_foreign_keys != set(_PHASE5_FOREIGN_KEYS.get(table_name, ())):
            raise RuntimeError(
                f"current Phase 5 {table_name} has invalid foreign keys"
            )
        if table_name in _PHASE5_CHECK_CONSTRAINTS:
            table_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = :table_name"
                ),
                {"table_name": table_name},
            ).scalar_one_or_none()
            normalized_table_sql = str(table_sql or "").casefold()
            missing_constraints = sorted(
                constraint
                for constraint in _PHASE5_CHECK_CONSTRAINTS[table_name]
                if constraint.casefold() not in normalized_table_sql
            )
            if missing_constraints:
                raise RuntimeError(
                    f"current Phase 5 {table_name} is missing CHECK constraints: "
                    + ", ".join(missing_constraints)
                )
            malformed_constraints = sorted(
                constraint
                for constraint in _PHASE5_CHECK_CONSTRAINTS[table_name]
                if (
                    _check_expression(str(table_sql or ""), constraint) is None
                    or _normalise_sql_fragment(
                        _check_expression(str(table_sql or ""), constraint) or ""
                    )
                    != _normalise_sql_fragment(_PHASE5_CHECK_EXPRESSIONS[constraint])
                )
            )
            if malformed_constraints:
                raise RuntimeError(
                    f"current Phase 5 {table_name} has malformed CHECK constraints: "
                    + ", ".join(malformed_constraints)
                )
    if connection.execute(
        text("SELECT COUNT(*) FROM application_generation_config WHERE id = 1")
    ).scalar_one() != 1:
        raise RuntimeError("current Phase 5 schema requires one application generation config")
    trigger_sql = {
        row[0]: str(row[1] or "").casefold()
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    for name, markers in _PHASE5_TRIGGER_MARKERS.items():
        sql = trigger_sql.get(name)
        if sql is None or any(marker not in sql for marker in markers):
            raise RuntimeError(f"current Phase 5 schema is missing or has an invalid trigger: {name}")
        when_clause = sql.split("begin", 1)[0]
        if (
            re.search(r"\bwhen\b.*?(?:\b1\s*=\s*0\b|\b0\s*=\s*1\b)", when_clause)
            or re.search(r"\bwhen\s*(?:\(\s*){0,3}(?:0|false)\b", when_clause)
            or re.search(r"\b(?:and|or)\s*(?:\(\s*)*0\b", when_clause)
            or re.search(r"\b(?:and|or)\s*(?:\(\s*)*false\b", when_clause)
        ):
            raise RuntimeError(f"current Phase 5 schema has a disabled trigger: {name}")
    try:
        _validate_phase5_rows(connection)
    except (AttributeError, KeyError, OverflowError, StateError, TypeError, ValueError) as exc:
        raise RuntimeError("current Phase 5 rows are malformed") from exc


def _validate_phase5_rows(connection) -> None:
    def validate_revision_timestamp(mapping, *, table_name: str) -> None:
        if type(mapping["revision"]) is not int or mapping["revision"] < 1:
            raise RuntimeError(f"current Phase 5 {table_name} revision is invalid")
        if type(mapping["updated_at"]) is not str:
            raise RuntimeError(f"current Phase 5 {table_name} timestamp is invalid")
        parse_utc(mapping["updated_at"])

    invalid_flags = connection.execute(
        text(
            "SELECT COUNT(*) FROM provider_connections "
            "WHERE typeof(enabled) <> 'integer' OR enabled NOT IN (0, 1) "
            "OR typeof(retired) <> 'integer' OR retired NOT IN (0, 1)"
        )
    ).scalar_one()
    invalid_flags += connection.execute(
        text(
            "SELECT COUNT(*) FROM chat_model_selection "
            "WHERE typeof(selection_required) <> 'integer' "
            "OR selection_required NOT IN (0, 1)"
        )
    ).scalar_one()
    if invalid_flags:
        raise RuntimeError("current Phase 5 boolean flags are malformed")

    connection_rows = connection.execute(select(provider_connections)).fetchall()
    connection_ids: set[str] = set()
    for row in connection_rows:
        value = _connection(row)
        if value.revision < 1 or value.catalogue_revision < 0:
            raise RuntimeError("current Phase 5 provider connection revisions are invalid")
        if type(value.enabled) is not bool or type(value.retired) is not bool:
            raise RuntimeError("current Phase 5 provider connection flags are invalid")
        connection_ids.add(value.id)

    refresh_rows = connection.execute(select(catalogue_refresh_state)).fetchall()
    refresh_by_connection: dict[str, object] = {}
    for row in refresh_rows:
        mapping = row._mapping
        connection_id = mapping["connection_id"]
        if type(connection_id) is not str or connection_id not in connection_ids:
            raise RuntimeError("current Phase 5 catalogue refresh state references a missing connection")
        if connection_id in refresh_by_connection:
            raise RuntimeError("current Phase 5 catalogue refresh state is duplicated")
        status = CatalogueRefreshStatus(mapping["status"])
        refresh_revision = mapping["refresh_revision"]
        if type(refresh_revision) is not int or refresh_revision < 0:
            raise RuntimeError("current Phase 5 catalogue refresh revision is invalid")
        if type(mapping["updated_at"]) is not str:
            raise RuntimeError("current Phase 5 catalogue refresh timestamp is invalid")
        parse_utc(mapping["updated_at"])
        failure_class_value = mapping["failure_class"]
        failure_message = mapping["failure_message"]
        failure_class = None if failure_class_value is None else CatalogueRefreshFailureClass(failure_class_value)
        if status is CatalogueRefreshStatus.NEVER:
            if refresh_revision != 0 or failure_class is not None or failure_message is not None:
                raise RuntimeError("current Phase 5 never-refreshed state is malformed")
        elif status is CatalogueRefreshStatus.SUCCEEDED:
            if refresh_revision < 1 or failure_class is not None or failure_message is not None:
                raise RuntimeError("current Phase 5 successful refresh state is malformed")
        elif (
            failure_class is None
            or failure_message != CATALOGUE_REFRESH_FAILURE_MESSAGES[failure_class]
            or refresh_revision < 1
        ):
            raise RuntimeError("current Phase 5 failed refresh state is malformed")
        refresh_by_connection[connection_id] = mapping
    if refresh_by_connection.keys() != connection_ids:
        raise RuntimeError("current Phase 5 catalogue refresh state is incomplete")
    for provider_row in connection_rows:
        provider_id = provider_row._mapping["id"]
        refresh = refresh_by_connection[provider_id]
        status = CatalogueRefreshStatus(refresh["status"])
        if status is not CatalogueRefreshStatus.NEVER and refresh["refresh_revision"] > provider_row._mapping["catalogue_revision"]:
            raise RuntimeError("current Phase 5 refresh revision exceeds catalogue revision")

    model_rows = connection.execute(select(model_catalogue_entries)).fetchall()
    model_ids: set[str] = set()
    model_by_id: dict[str, object] = {}
    for row in model_rows:
        value = _model(row)
        if value.connection_id not in connection_ids:
            raise RuntimeError("current Phase 5 model catalogue references a missing connection")
        model_ids.add(value.id)
        model_by_id[value.id] = value

    app_row = connection.execute(
        select(application_generation_config).where(application_generation_config.c.id == 1)
    ).first()
    if app_row is None:
        raise RuntimeError("current Phase 5 application generation configuration is missing")
    _settings(app_row._mapping)
    validate_revision_timestamp(app_row._mapping, table_name="application_generation_config")
    default_model_id = app_row._mapping["default_model_entry_id"]
    if default_model_id is not None:
        model = model_by_id.get(default_model_id)
        if model is None:
            raise RuntimeError("current Phase 5 application default model is missing")
        provider = next(
            (item for item in connection_rows if item._mapping["id"] == model.connection_id),
            None,
        )
        if provider is None or bool(provider._mapping["retired"]):
            raise RuntimeError("current Phase 5 application default connection is retired")

    for row in connection.execute(select(model_generation_config)).fetchall():
        if row._mapping["model_entry_id"] not in model_ids:
            raise RuntimeError("current Phase 5 model settings reference a missing model")
        _settings(row._mapping)
        validate_revision_timestamp(row._mapping, table_name="model_generation_config")
    for row in connection.execute(select(chat_model_generation_config)).fetchall():
        if row._mapping["model_entry_id"] not in model_ids:
            raise RuntimeError("current Phase 5 chat settings reference a missing model")
        _settings(row._mapping)
        validate_revision_timestamp(row._mapping, table_name="chat_model_generation_config")
    for row in connection.execute(select(chat_model_selection)).fetchall():
        mapping = row._mapping
        if mapping["model_entry_id"] is not None and mapping["model_entry_id"] not in model_ids:
            raise RuntimeError("current Phase 5 chat selection references a missing model")
        if bool(mapping["selection_required"]) != (mapping["model_entry_id"] is None):
            raise RuntimeError("current Phase 5 chat selection state is inconsistent")
        validate_revision_timestamp(mapping, table_name="chat_model_selection")

    duplicate_fact = connection.execute(
        text(
            "SELECT 1 FROM capability_facts "
            "GROUP BY model_entry_id, capability_key, source, source_revision "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_fact is not None:
        raise RuntimeError("current Phase 5 capability facts contain duplicate identities")

    for row in connection.execute(select(capability_facts)).fetchall():
        mapping = row._mapping
        if (
            type(mapping["id"]) is not str
            or not mapping["id"]
            or mapping["model_entry_id"] not in model_ids
            or mapping["capability_key"] not in CAPABILITY_KEYS
            or mapping["source"] == CapabilitySource.MANUAL.value
        ):
            raise RuntimeError("current Phase 5 capability fact is malformed")
        try:
            CapabilityState(mapping["state"])
            CapabilitySource(mapping["source"])
            _capability_provenance(json.loads(mapping["provenance_json"]))
        except (TypeError, ValueError, StateError) as exc:
            raise RuntimeError("current Phase 5 capability fact is malformed") from exc
        if mapping["source_revision"] is not None and (
            type(mapping["source_revision"]) is not int or mapping["source_revision"] < 0
        ):
            raise RuntimeError("current Phase 5 capability fact revision is invalid")
        if mapping["source"] in {
            CapabilitySource.CONFIRMED_ENDPOINT.value,
            CapabilitySource.PROVIDER_METADATA.value,
        } and mapping["source_revision"] is None:
            raise RuntimeError("current Phase 5 capability fact revision is missing")
        try:
            validate_capability_value(
                mapping["capability_key"],
                CapabilityState(mapping["state"]),
                mapping["value"],
            )
        except ValueError as exc:
            raise RuntimeError("current Phase 5 capability fact value is invalid") from exc
    for row in connection.execute(select(capability_overrides)).fetchall():
        mapping = row._mapping
        if mapping["model_entry_id"] not in model_ids or mapping["capability_key"] not in CAPABILITY_KEYS:
            raise RuntimeError("current Phase 5 capability override is malformed")
        try:
            CapabilityState(mapping["state"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("current Phase 5 capability override is malformed") from exc
        if mapping["reason"] is not None and (
            type(mapping["reason"]) is not str or len(mapping["reason"]) > 256
        ):
            raise RuntimeError("current Phase 5 capability override reason is invalid")
        try:
            validate_capability_value(
                mapping["capability_key"],
                CapabilityState(mapping["state"]),
                mapping["value"],
            )
        except ValueError as exc:
            raise RuntimeError("current Phase 5 capability override value is invalid") from exc
        if type(mapping["revision"]) is not int or mapping["revision"] < 1:
            raise RuntimeError("current Phase 5 capability override revision is invalid")
        if type(mapping["updated_at"]) is not str:
            raise RuntimeError("current Phase 5 capability override timestamp is invalid")
        parse_utc(mapping["updated_at"])
    for row in connection.execute(select(capability_observations)).fetchall():
        mapping = row._mapping
        if mapping["model_entry_id"] not in model_ids or mapping["capability_key"] not in CAPABILITY_KEYS:
            raise RuntimeError("current Phase 5 capability observation is malformed")
        try:
            CapabilityState(mapping["observed_state"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("current Phase 5 capability observation is malformed") from exc
        if type(mapping["observed_at"]) is not str:
            raise RuntimeError("current Phase 5 capability observation timestamp is invalid")
        parse_utc(mapping["observed_at"])

    inconsistent_attempts = connection.execute(
        text(
            "SELECT COUNT(*) FROM generation_attempts AS a "
            "WHERE (json_extract(a.request_snapshot, '$.snapshot_version') = 2 "
            "AND (a.connection_id IS NULL OR a.model_entry_id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM model_catalogue_entries AS m "
            "JOIN provider_connections AS p ON p.id = m.connection_id "
            "WHERE m.id = a.model_entry_id AND m.connection_id = a.connection_id "
            "AND m.provider_model_id = json_extract(a.request_snapshot, '$.model') "
            "AND p.id = json_extract(a.request_snapshot, '$.connection_id')"
            "))) OR (COALESCE(json_extract(a.request_snapshot, '$.snapshot_version'), 0) <> 2 "
            "AND (a.connection_id IS NOT NULL OR a.model_entry_id IS NOT NULL))"
        )
    ).scalar_one()
    if inconsistent_attempts:
        raise RuntimeError("current Phase 5 attempt attribution is inconsistent")


def _validate_phase5_trigger_behavior(connection) -> None:
    """Exercise guaranteed Phase 5 DML boundaries inside savepoints.

    Trigger names and SQL markers are useful diagnostics, but are not authority
    by themselves: a marker-preserving inert trigger must not make a database
    acceptable. These probes make startup verify behavior without retaining any
    probe row or changing the caller's transaction.
    """
    savepoint = "bots5_phase5_trigger_probe"

    def require_abort(name: str, statement: str, parameters: tuple[object, ...] = ()) -> None:
        connection.exec_driver_sql(f"SAVEPOINT {savepoint}")
        try:
            try:
                connection.exec_driver_sql(statement, parameters)
            except Exception:
                return
            raise RuntimeError(f"current Phase 5 trigger is not enforcing: {name}")
        finally:
            connection.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")

    now = "2000-01-01T00:00:00Z"
    require_abort(
        "phase5_provider_connection_validate_insert",
        "INSERT INTO provider_connections "
        "(id, name, name_key, backend_type, profile, endpoint, credential_source, "
        "credential_reference, enabled, retired, revision, catalogue_revision, created_at, updated_at) "
        "VALUES (?, 'Trigger Probe', 'wrong-name-key', 'fake', 'generic', NULL, 'none', NULL, 1, 0, 1, 0, ?, ?)",
        (str(uuid7()), now, now),
    )
    connection_id = connection.exec_driver_sql(
        "SELECT id FROM provider_connections ORDER BY id LIMIT 1"
    ).scalar_one_or_none()
    if connection_id is not None:
        require_abort(
            "phase5_provider_connection_catalogue_revision_guard",
            "UPDATE provider_connections SET catalogue_revision = catalogue_revision - 1 WHERE id = ?",
            (connection_id,),
        )
        require_abort(
            "phase5_provider_connection_catalogue_revision_guard",
            "UPDATE provider_connections SET catalogue_revision = catalogue_revision + 1 WHERE id = ?",
            (connection_id,),
        )
    require_abort(
        "phase5_application_settings_validate_update",
        "UPDATE application_generation_config SET revision = 0 WHERE id = 1",
    )
    model_id = connection.exec_driver_sql(
        "SELECT id FROM model_catalogue_entries ORDER BY id LIMIT 1"
    ).scalar_one_or_none()
    if model_id is not None:
        model_connection_id = connection.exec_driver_sql(
            "SELECT connection_id FROM model_catalogue_entries WHERE id = ?",
            (model_id,),
        ).scalar_one()
        require_abort(
            "phase5_model_catalogue_metadata_validate_insert",
            "INSERT INTO model_catalogue_entries "
            "(id, connection_id, provider_model_id, display_name, origin, availability, "
            "discovery_revision, discovered_at, metadata_json, revision, created_at, updated_at) "
            "VALUES (?, ?, '__phase5_trigger_probe__', 'Trigger Probe', 'discovered', "
            "'available', NULL, NULL, '{\"TOKEN\":\"probe\"}', 1, ?, ?)",
            (str(uuid7()), model_connection_id, now, now),
        )
        require_abort(
            "ck_model_catalogue_origin",
            "UPDATE model_catalogue_entries SET origin = 'invalid' WHERE id = ?",
            (model_id,),
        )
        require_abort(
            "ck_model_catalogue_availability",
            "UPDATE model_catalogue_entries SET availability = 'invalid' WHERE id = ?",
            (model_id,),
        )
        require_abort(
            "ck_model_catalogue_revision",
            "UPDATE model_catalogue_entries SET revision = 0 WHERE id = ?",
            (model_id,),
        )
        require_abort(
            "phase5_model_catalogue_metadata_validate_update",
            "UPDATE model_catalogue_entries SET metadata_json = ? WHERE id = ?",
            ('{"TOKEN":"probe"}', model_id),
        )
        require_abort(
            "phase5_capability_fact_truth_validate_insert",
            "INSERT INTO capability_facts "
            "(id, model_entry_id, capability_key, state, source, source_revision, value, provenance_json, observed_at) "
            "VALUES (?, ?, 'outside.closed.vocabulary', 'supported', 'unknown', NULL, NULL, '{}', ?)",
            (str(uuid7()), model_id, now),
        )
        require_abort(
            "phase5_capability_observation_truth_validate_insert",
            "INSERT INTO capability_observations "
            "(id, model_entry_id, capability_key, observed_state, detail, observed_at) "
            "VALUES (?, ?, 'outside.closed.vocabulary', 'unknown', 'probe', 'not-a-timestamp')",
            (str(uuid7()), model_id),
        )
        fact_id = connection.exec_driver_sql(
            "SELECT id FROM capability_facts "
            "WHERE capability_key = 'generation.streaming' ORDER BY id LIMIT 1"
        ).scalar_one_or_none()
        if fact_id is not None:
            require_abort(
                "phase5_capability_fact_provenance_validate_update",
                "UPDATE capability_facts SET provenance_json = ? WHERE id = ?",
                ('{"TOKEN":"probe"}', fact_id),
            )
            require_abort(
                "phase5_capability_fact_truth_validate_update",
                "UPDATE capability_facts SET value = 1 "
                "WHERE id = ? AND capability_key = 'generation.streaming'",
                (fact_id,),
            )
            require_abort(
                "phase5_capability_fact_truth_validate_update",
                "UPDATE capability_facts SET source_revision = -99 "
                "WHERE id = ?",
                (fact_id,),
            )
            require_abort(
                "phase5_capability_fact_truth_validate_update",
                "UPDATE capability_facts SET capability_key = 'outside.closed.vocabulary' "
                "WHERE id = ?",
                (fact_id,),
            )
            require_abort(
                "phase5_capability_fact_truth_validate_update",
                "UPDATE capability_facts SET observed_at = 'not-a-timestamp' "
                "WHERE id = ?",
                (fact_id,),
            )
        observation_id = connection.exec_driver_sql(
            "SELECT id FROM capability_observations ORDER BY id LIMIT 1"
        ).scalar_one_or_none()
        if observation_id is not None:
            require_abort(
                "phase5_capability_observation_truth_validate_update",
                "UPDATE capability_observations SET observed_at = 'not-a-timestamp' "
                "WHERE id = ?",
                (observation_id,),
            )


class SQLiteAppStateStore(Phase5StoreMixin):
    def __init__(self, engine: Engine):
        self._engine = engine
        self._closed = False

    @classmethod
    def open(cls, database: Path) -> SQLiteAppStateStore:
        engine = _engine(database)
        try:
            with engine.begin() as connection:
                integrity = __import__(
                    "bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries",
                    fromlist=["_validate_existing_state"],
                )
                integrity._validate_existing_state(connection)
                column_info = {
                    row[1]: row
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info(generation_attempts)"
                    ).fetchall()
                }
                columns = set(column_info)
                revision = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one_or_none()
                phase3_columns = {
                    "provider_id",
                    "returned_model",
                    "request_id",
                    "finish_reason",
                    "prompt_tokens",
                    "completion_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                    "known_cost_usd",
                    "remote_outcome_unknown",
                }
                if revision in {"0005_generation_outcomes", "0006_phase4_workspace"} and not phase3_columns <= columns:
                    missing = ", ".join(sorted(phase3_columns - columns))
                    raise RuntimeError(
                        "current Phase 3 schema is missing generation outcome columns: "
                        f"{missing}"
                    )
                if revision in {"0005_generation_outcomes", "0006_phase4_workspace"} or "provider_id" in columns:
                    non_nullable = sorted(
                        name for name in phase3_columns if column_info[name][3] != 0
                    )
                    if non_nullable:
                        raise RuntimeError(
                            "current Phase 3 schema outcome columns must be nullable: "
                            + ", ".join(non_nullable)
                        )
                    wrong_types = sorted(
                        name
                        for name, expected_type in _PHASE3_COLUMN_TYPES.items()
                        if str(column_info[name][2]).upper().replace(" ", "")
                        != expected_type
                    )
                    if wrong_types:
                        details = ", ".join(
                            f"{name}={column_info[name][2]}"
                            for name in wrong_types
                        )
                        raise RuntimeError(
                            "current Phase 3 schema outcome columns have invalid declared types: "
                            + details
                        )
                if revision == "0006_phase4_workspace":
                    _validate_phase4_schema(connection)
                if revision == "0008_catalogue_refresh_outcomes":
                    _validate_phase4_schema(connection)
                    _validate_phase5_schema(connection)
                    _validate_phase5_trigger_behavior(connection)
                if "provider_id" in columns:
                    outcomes = __import__(
                        "bots5.infrastructure.persistence.migrations.versions.0005_generation_outcomes",
                        fromlist=["_validate_outcome_rows", "_replace_attempt_triggers"],
                    )
                    outcomes._validate_outcome_rows(connection)
                    outcomes._replace_attempt_triggers(connection)
        except BaseException:
            engine.dispose()
            raise
        return cls(engine)

    @property
    def engine(self) -> Engine:
        self._ensure_open()
        return self._engine

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateError("state store is closed")

    def create_chat(self, chat: Chat) -> None:
        self._ensure_open()
        if chat.revision != 0 or chat.head_message_id is not None:
            raise StateError("new chats must start at revision 0 without a head")
        with self._engine.begin() as connection:
            connection.execute(
                insert(chats).values(
                    id=chat.id,
                    title=chat.title,
                    created_at=utc_iso(chat.created_at),
                    updated_at=utc_iso(chat.updated_at),
                    head_message_id=chat.head_message_id,
                    revision=chat.revision,
                )
            )

    def list_chats(self) -> tuple[Chat, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(chats).order_by(chats.c.updated_at.desc(), chats.c.id.desc())
            ).fetchall()
        return tuple(_chat(row) for row in rows)

    def get_chat(self, chat_id: str) -> Chat | None:
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.execute(select(chats).where(chats.c.id == chat_id)).first()
        return None if row is None else _chat(row)

    def get_message(self, message_id: str) -> Message | None:
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.execute(select(messages).where(messages.c.id == message_id)).first()
        return None if row is None else _message(row)

    def list_messages(self, chat_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(messages)
                .where(messages.c.chat_id == chat_id)
                .order_by(messages.c.sequence.asc())
            ).fetchall()
        return tuple(_message(row) for row in rows)

    def list_branch_messages(
        self,
        chat_id: str,
        leaf_message_id: str | None = None,
    ) -> tuple[Message, ...]:
        self._ensure_open()
        chat = self.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        leaf_id = leaf_message_id or chat.head_message_id
        if leaf_id is None:
            return ()

        by_id = {message.id: message for message in self.list_messages(chat_id)}
        current_id = leaf_id
        branch: list[Message] = []
        seen: set[str] = set()
        while current_id is not None:
            if current_id in seen:
                raise StateError("message lineage contains a cycle")
            seen.add(current_id)
            message = by_id.get(current_id)
            if message is None:
                raise StateError(f"message is not in chat: {current_id}")
            branch.append(message)
            current_id = message.parent_id
        branch.reverse()
        return tuple(branch)

    def list_revisions(self, chat_id: str, lineage_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(messages)
                .where(messages.c.chat_id == chat_id, messages.c.lineage_id == lineage_id)
                .order_by(messages.c.revision.asc(), messages.c.sequence.asc())
            ).fetchall()
        return tuple(_message(row) for row in rows)

    def list_generation_attempts(self, chat_id: str) -> tuple[GenerationAttempt, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    generation_attempts,
                    messages.c.id.label("_attempt_user_message_id"),
                    messages.c.content.label("_attempt_user_message_content"),
                )
                .select_from(
                    generation_attempts.outerjoin(
                        messages,
                        messages.c.id == generation_attempts.c.user_message_id,
                    )
                )
                .where(generation_attempts.c.chat_id == chat_id)
                .order_by(
                    generation_attempts.c.started_at.asc(),
                    generation_attempts.c.id.asc(),
                )
            ).fetchall()
        attempts = []
        for row in rows:
            mapping = row._mapping
            if mapping["_attempt_user_message_id"] is None:
                raise StateError(
                    "generation attempt user message not found: "
                    f"{mapping['id']}"
                )
            attempts.append(
                _attempt(row, mapping["_attempt_user_message_content"])
            )
        return tuple(attempts)

    def list_active_generation_attempts(
        self,
        chat_id: str | None = None,
    ) -> tuple[GenerationAttempt, ...]:
        self._ensure_open()
        statement = (
            select(
                generation_attempts,
                messages.c.id.label("_attempt_user_message_id"),
                messages.c.content.label("_attempt_user_message_content"),
            )
            .select_from(
                generation_attempts.join(
                    messages,
                    messages.c.id == generation_attempts.c.user_message_id,
                )
            )
            .where(generation_attempts.c.state == AttemptState.RUNNING.value)
            .order_by(generation_attempts.c.started_at.asc(), generation_attempts.c.id.asc())
        )
        if chat_id is not None:
            statement = statement.where(generation_attempts.c.chat_id == chat_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).fetchall()
        return tuple(
            _attempt(row, row._mapping["_attempt_user_message_content"])
            for row in rows
        )

    def get_generation_attempt(self, attempt_id: str) -> GenerationAttempt | None:
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    generation_attempts,
                    messages.c.id.label("_attempt_user_message_id"),
                    messages.c.content.label("_attempt_user_message_content"),
                )
                .select_from(
                    generation_attempts.outerjoin(
                        messages,
                        messages.c.id == generation_attempts.c.user_message_id,
                    )
                )
                .where(generation_attempts.c.id == attempt_id)
            ).first()
        if row is None:
            return None
        mapping = row._mapping
        if mapping["_attempt_user_message_id"] is None:
            raise StateError(f"generation attempt user message not found: {attempt_id}")
        return _attempt(row, mapping["_attempt_user_message_content"])

    def next_message_sequence(self, chat_id: str) -> int:
        self._ensure_open()
        with self._engine.connect() as connection:
            value = connection.execute(
                select(func.max(messages.c.sequence)).where(messages.c.chat_id == chat_id)
            ).scalar_one()
        return 1 if value is None else int(value) + 1

    def _insert_messages_and_attempt(
        self,
        connection,
        messages_to_insert: tuple[Message, ...],
        attempt: GenerationAttempt,
    ) -> None:
        if attempt.state is not AttemptState.RUNNING or attempt.ended_at is not None:
            raise StateError("generation start must use a running attempt without an end time")
        pending = {message.id: message for message in messages_to_insert}
        if len(pending) != len(messages_to_insert):
            raise StateError("generation start contains duplicate message identities")
        for message in messages_to_insert:
            if message.chat_id != attempt.chat_id:
                raise StateError("generation messages must belong to the attempt chat")
            if message.sequence < 1:
                raise StateError("message sequence must be positive")
            if not isinstance(message.role, MessageRole):
                raise StateError("message role is invalid")
            if message.state.value not in _MESSAGE_STATES:
                raise StateError("message state is invalid")
            if message.supersedes_id is None:
                if message.revision != 1:
                    raise StateError(
                        "a message without a superseded revision must have revision 1"
                    )
                continue
            target = pending.get(message.supersedes_id)
            if target is None:
                target = connection.execute(
                    select(messages).where(messages.c.id == message.supersedes_id)
                ).first()
            if target is None:
                raise StateError(f"superseded message not found: {message.supersedes_id}")
            target_lineage = (
                target.lineage_id if isinstance(target, Message) else target.lineage_id
            )
            target_revision = int(
                target.revision if isinstance(target, Message) else target.revision
            )
            target_chat_id = target.chat_id if isinstance(target, Message) else target.chat_id
            target_role = target.role if isinstance(target, Message) else MessageRole(target.role)
            if target_chat_id != message.chat_id:
                raise StateError("superseded message must be in the same chat")
            if target_lineage != message.lineage_id:
                raise StateError("superseded message must share the message lineage")
            if message.revision != target_revision + 1:
                raise StateError("message revision must immediately follow its superseded revision")
            if target_role != message.role:
                raise StateError("superseded message must have the same role")

        def pending_or_stored(message_id: str) -> Message | None:
            message = pending.get(message_id)
            if message is not None:
                return message
            row = connection.execute(
                select(messages).where(messages.c.id == message_id)
            ).first()
            return None if row is None else _message(row)

        user_message = pending_or_stored(attempt.user_message_id)
        assistant_message = pending_or_stored(attempt.assistant_message_id)
        if user_message is None or user_message.role != MessageRole.USER:
            raise StateError("generation attempt must reference a user message")
        if assistant_message is None or assistant_message.role != MessageRole.ASSISTANT:
            raise StateError("generation attempt must reference an assistant message")
        if user_message.state != MessageState.SENT:
            raise StateError("generation attempt user message must be sent")
        if assistant_message.state != MessageState.STREAMING:
            raise StateError("generation attempt assistant message must be streaming")
        if user_message.chat_id != attempt.chat_id or assistant_message.chat_id != attempt.chat_id:
            raise StateError("generation attempt messages must belong to the attempt chat")
        if attempt.user_message_id == attempt.assistant_message_id:
            raise StateError("generation attempt user and assistant messages must differ")
        if assistant_message.parent_id != user_message.id:
            raise StateError("generation attempt assistant must belong to its user turn")
        _validate_request_snapshot(attempt, user_message.content)
        _validate_attempt_outcome(attempt)

        active_id = connection.execute(
            select(generation_attempts.c.id)
            .where(
                generation_attempts.c.chat_id == attempt.chat_id,
                generation_attempts.c.state == AttemptState.RUNNING.value,
            )
            .limit(1)
        ).scalar_one_or_none()
        if active_id is not None:
            raise StateError(
                "chat already has an active generation: "
                f"{attempt.chat_id} ({active_id})"
            )

        arm_transition(
            connection,
            attempt.assistant_message_id,
            attempt.id,
            "start",
            user_message_id=attempt.user_message_id,
        )
        try:
            connection.execute(
                insert(messages),
                [_message_values(message) for message in messages_to_insert],
            )
            connection.execute(
                insert(generation_attempts).values(
                    id=attempt.id,
                    chat_id=attempt.chat_id,
                    user_message_id=attempt.user_message_id,
                    assistant_message_id=attempt.assistant_message_id,
                    backend_id=attempt.backend_id,
                    model=attempt.model,
                    state=attempt.state.value,
                    request_snapshot=attempt.request_snapshot,
                    started_at=utc_iso(attempt.started_at),
                    provider_id=attempt.provider_id,
                    returned_model=attempt.returned_model,
                    request_id=attempt.request_id,
                    finish_reason=attempt.finish_reason,
                    prompt_tokens=attempt.prompt_tokens,
                    completion_tokens=attempt.completion_tokens,
                    reasoning_tokens=attempt.reasoning_tokens,
                    total_tokens=attempt.total_tokens,
                    known_cost_usd=(
                        None
                        if attempt.known_cost_usd is None
                        else str(attempt.known_cost_usd)
                    ),
                    remote_outcome_unknown=attempt.remote_outcome_unknown,
                    connection_id=attempt.connection_id,
                    model_entry_id=attempt.model_entry_id,
                )
            )
        finally:
            clear_transition(connection)

    def _advance_chat(
        self,
        connection,
        chat: Chat,
        head_message_id: str,
        expected_chat_revision: int | None,
    ) -> None:
        self._ensure_open()
        if chat.revision < 0:
            raise StateError("chat revision must be nonnegative")
        statement = update(chats).where(chats.c.id == chat.id)
        current_revision = connection.execute(
            select(chats.c.revision).where(chats.c.id == chat.id)
        ).scalar_one_or_none()
        if current_revision is None:
            raise StateError(f"chat not found: {chat.id}")
        expected = current_revision if expected_chat_revision is None else expected_chat_revision
        if chat.revision != int(expected) + 1:
            raise RevisionConflict(f"chat revision must advance by one: {chat.id}")
        statement = statement.where(chats.c.revision == expected)
        arm_transition(connection, head_message_id, chat.id, "advance")
        try:
            result = connection.execute(
                statement.values(
                    updated_at=utc_iso(chat.updated_at),
                    head_message_id=head_message_id,
                    revision=chat.revision,
                )
            )
        finally:
            clear_transition(connection)
        if result.rowcount != 1:
            raise RevisionConflict(f"chat revision changed: {chat.id}")

    def persist_generation_start(
        self,
        chat: Chat,
        user_message: Message,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
    ) -> None:
        self._ensure_open()
        with self._engine.begin() as connection:
            self._insert_messages_and_attempt(
                connection,
                (user_message, assistant_message),
                attempt,
            )
            self._advance_chat(
                connection,
                chat,
                assistant_message.id,
                expected_chat_revision,
            )

    def persist_regeneration_start(
        self,
        chat: Chat,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
    ) -> None:
        self._ensure_open()
        with self._engine.begin() as connection:
            self._insert_messages_and_attempt(connection, (assistant_message,), attempt)
            self._advance_chat(
                connection,
                chat,
                assistant_message.id,
                expected_chat_revision,
            )

    def update_streaming_message(self, message: Message) -> None:
        self._ensure_open()
        if message.state != MessageState.STREAMING:
            raise StateError("streaming updates must retain the streaming state")
        with self._engine.begin() as connection:
            result = connection.execute(
                update(messages)
                .where(messages.c.id == message.id)
                .values(state=message.state.value, content=message.content)
            )
            if result.rowcount != 1:
                raise StateError(f"message not found or no longer mutable: {message.id}")

    def finalize_generation(self, message: Message, attempt: GenerationAttempt) -> None:
        self._ensure_open()
        persisted_phase3 = _is_persisted_phase3_attempt(attempt)
        _validate_attempt_outcome(attempt, phase3=persisted_phase3)
        if message.state not in _MESSAGE_TERMINAL_STATES:
            raise StateError("finalized message must be terminal")
        if attempt.state not in _ATTEMPT_TERMINAL_STATES:
            raise StateError("finalized attempt must be terminal")
        expected_message_states = {
            _ATTEMPT_TO_MESSAGE_STATE[attempt.state]
        }
        if attempt.state is AttemptState.INCOMPLETE:
            expected_message_states.add(MessageState.TRUNCATED)
        if message.state not in expected_message_states:
            raise StateError("message and attempt terminal states do not match")
        _validate_request_snapshot(attempt, phase3=persisted_phase3)
        with self._engine.begin() as connection:
            stored_message_row = connection.execute(
                select(messages).where(messages.c.id == message.id)
            ).first()
            stored_attempt_row = connection.execute(
                select(generation_attempts).where(generation_attempts.c.id == attempt.id)
            ).first()
            if stored_message_row is None:
                raise StateError(f"message not found or no longer mutable: {message.id}")
            if stored_attempt_row is None:
                raise StateError(f"generation attempt not found: {attempt.id}")
            stored_message = _message(stored_message_row)
            stored_attempt = _attempt(stored_attempt_row)
            validate_remote_outcome_transition(
                old_state=stored_attempt.state.value,
                old_remote_outcome_unknown=stored_attempt.remote_outcome_unknown,
                new_state=attempt.state.value,
                new_remote_outcome_unknown=attempt.remote_outcome_unknown,
                new_finish_reason=attempt.finish_reason,
                phase3=_is_persisted_phase3_attempt(stored_attempt),
                error_type=StateError,
            )
            if stored_message.state != MessageState.STREAMING:
                raise StateError(f"message is not streaming: {message.id}")
            if stored_attempt.state != AttemptState.RUNNING:
                raise StateError(f"generation attempt is not running: {attempt.id}")
            if stored_attempt.assistant_message_id != message.id:
                raise StateError("finalized message does not belong to the attempt")
            if stored_attempt.user_message_id != message.parent_id:
                raise StateError("finalized message has the wrong user turn")
            stored_user_row = connection.execute(
                select(messages).where(messages.c.id == stored_attempt.user_message_id)
            ).first()
            if stored_user_row is None:
                raise StateError(f"generation attempt user message not found: {attempt.id}")
            _validate_request_snapshot(
                attempt,
                stored_user_row.content,
                phase3=persisted_phase3,
            )
            if stored_attempt.chat_id != message.chat_id or attempt.chat_id != message.chat_id:
                raise StateError("finalized message and attempt must share a chat")
            if (
                attempt.user_message_id != stored_attempt.user_message_id
                or attempt.assistant_message_id != stored_attempt.assistant_message_id
                or attempt.backend_id != stored_attempt.backend_id
                or attempt.model != stored_attempt.model
                or attempt.provider_id != stored_attempt.provider_id
                or attempt.connection_id != stored_attempt.connection_id
                or attempt.model_entry_id != stored_attempt.model_entry_id
                or attempt.request_snapshot != stored_attempt.request_snapshot
                or utc_iso(attempt.started_at) != utc_iso(stored_attempt.started_at)
            ):
                raise StateError("generation request snapshot identity is immutable")
            if message.chat_id != stored_message.chat_id or message.parent_id != stored_message.parent_id:
                raise StateError("message identity is immutable")
            arm_transition(connection, message.id, attempt.id, "finalize")
            try:
                attempt_result = connection.execute(
                    update(generation_attempts)
                    .where(generation_attempts.c.id == attempt.id)
                    .values(
                        state=attempt.state.value,
                        ended_at=None if attempt.ended_at is None else utc_iso(attempt.ended_at),
                        error_type=attempt.error_type,
                        error_message=attempt.error_message,
                        provider_id=attempt.provider_id,
                        returned_model=attempt.returned_model,
                        request_id=attempt.request_id,
                        finish_reason=attempt.finish_reason,
                        prompt_tokens=attempt.prompt_tokens,
                        completion_tokens=attempt.completion_tokens,
                        reasoning_tokens=attempt.reasoning_tokens,
                        total_tokens=attempt.total_tokens,
                        known_cost_usd=(
                            None
                            if attempt.known_cost_usd is None
                            else str(attempt.known_cost_usd)
                        ),
                        remote_outcome_unknown=attempt.remote_outcome_unknown,
                    )
                )
                if attempt_result.rowcount != 1:
                    raise StateError(f"generation attempt not found: {attempt.id}")
                message_result = connection.execute(
                    update(messages)
                    .where(messages.c.id == message.id)
                    .values(state=message.state.value, content=message.content)
                )
                if message_result.rowcount != 1:
                    raise StateError(f"message not found or no longer mutable: {message.id}")
            finally:
                clear_transition(connection)

    def reconcile_interrupted_generations(self, now) -> None:
        """Persist an honest terminal state for work left running by a prior process."""
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(generation_attempts).where(
                    generation_attempts.c.state == AttemptState.RUNNING.value
                )
            ).fetchall()
        for row in rows:
            attempt = _attempt(row)
            message = self.get_message(attempt.assistant_message_id)
            if message is None:
                raise StateError(
                    f"running generation attempt has no assistant message: {attempt.id}"
                )
            if message.state != MessageState.STREAMING:
                raise StateError(
                    f"running generation attempt has non-streaming assistant: {attempt.id}"
                )
            self.finalize_generation(
                replace(message, state=MessageState.ABORTED),
                replace(
                    attempt,
                    state=AttemptState.ABORTED,
                    ended_at=now,
                    error_type="aborted",
                    error_message="generation was interrupted before application restart",
                    remote_outcome_unknown=(
                        True
                        if attempt.remote_outcome_unknown is None
                        else attempt.remote_outcome_unknown
                    ),
                ),
            )

    def update_attempt(self, attempt: GenerationAttempt) -> None:
        self._ensure_open()
        persisted_phase3 = _is_persisted_phase3_attempt(attempt)
        _validate_attempt_outcome(attempt, phase3=persisted_phase3)
        _validate_request_snapshot(attempt, phase3=persisted_phase3)
        with self._engine.begin() as connection:
            stored_row = connection.execute(
                select(generation_attempts).where(generation_attempts.c.id == attempt.id)
            ).first()
            if stored_row is None:
                raise StateError(f"generation attempt not found: {attempt.id}")
            stored = _attempt(stored_row)
            user_row = connection.execute(
                select(messages).where(messages.c.id == stored.user_message_id)
            ).first()
            if user_row is None:
                raise StateError(f"generation attempt user message not found: {attempt.id}")
            _validate_request_snapshot(
                attempt,
                user_row.content,
                phase3=persisted_phase3,
            )
            validate_remote_outcome_transition(
                old_state=stored.state.value,
                old_remote_outcome_unknown=stored.remote_outcome_unknown,
                new_state=attempt.state.value,
                new_remote_outcome_unknown=attempt.remote_outcome_unknown,
                new_finish_reason=attempt.finish_reason,
                phase3=_is_persisted_phase3_attempt(stored),
                error_type=StateError,
            )
            if (
                attempt.chat_id != stored.chat_id
                or attempt.user_message_id != stored.user_message_id
                or attempt.assistant_message_id != stored.assistant_message_id
                or attempt.backend_id != stored.backend_id
                or attempt.model != stored.model
                or attempt.provider_id != stored.provider_id
                or attempt.connection_id != stored.connection_id
                or attempt.model_entry_id != stored.model_entry_id
                or attempt.request_snapshot != stored.request_snapshot
                or utc_iso(attempt.started_at) != utc_iso(stored.started_at)
            ):
                raise StateError("generation request snapshot identity is immutable")
            result = connection.execute(
                update(generation_attempts)
                .where(generation_attempts.c.id == attempt.id)
                .values(
                    state=attempt.state.value,
                    ended_at=None if attempt.ended_at is None else utc_iso(attempt.ended_at),
                    error_type=attempt.error_type,
                    error_message=attempt.error_message,
                    returned_model=attempt.returned_model,
                    request_id=attempt.request_id,
                    finish_reason=attempt.finish_reason,
                    prompt_tokens=attempt.prompt_tokens,
                    completion_tokens=attempt.completion_tokens,
                    reasoning_tokens=attempt.reasoning_tokens,
                    total_tokens=attempt.total_tokens,
                    known_cost_usd=(
                        None
                        if attempt.known_cost_usd is None
                        else str(attempt.known_cost_usd)
                    ),
                    remote_outcome_unknown=attempt.remote_outcome_unknown,
                )
            )
            if result.rowcount != 1:
                raise StateError(f"generation attempt not found: {attempt.id}")

    def list_workspace_windows(self) -> tuple[WorkspaceWindowState, ...]:
        self._ensure_open()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(workspace_windows).order_by(
                    workspace_windows.c.ordinal.asc(),
                    workspace_windows.c.window_id.asc(),
                )
            ).fetchall()
        states: list[WorkspaceWindowState] = []
        for row in rows:
            try:
                states.append(_workspace_window(row))
            except StateError:
                # Presentation corruption must fall back to a fresh window;
                # it must not prevent access to durable chat state.
                continue
        return tuple(states)

    def save_workspace_window(self, state: WorkspaceWindowState) -> None:
        self._ensure_open()
        geometry_json = None if state.geometry is None else json.dumps(list(state.geometry))
        with self._engine.begin() as connection:
            connection.execute(
                delete(workspace_windows).where(
                    workspace_windows.c.window_id == state.window_id
                )
            )
            connection.execute(
                insert(workspace_windows).values(
                    window_id=state.window_id,
                    ordinal=state.ordinal,
                    geometry_json=geometry_json,
                    selected_chat_id=state.selected_chat_id,
                    rail_collapsed=state.rail_collapsed,
                    restore_open=state.restore_open,
                    updated_at=utc_iso(state.updated_at),
                )
            )

    def delete_workspace_window(self, window_id: str) -> None:
        self._ensure_open()
        with self._engine.begin() as connection:
            connection.execute(
                delete(workspace_windows).where(workspace_windows.c.window_id == window_id)
            )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._engine.dispose()
