from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3

from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from functools import wraps
from threading import RLock

from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, text, update
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import DBAPIError, IntegrityError
from uuid6 import uuid7

from bots5.core.errors import (
    AuthorityError,
    RevisionConflict,
    SearchIndexInvalid,
    SearchRebuilding,
    SearchResultGone,
    SearchStaleIndex,
    SearchUnavailable,
    StateError,
)
from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.models import (
    AttemptState,
    Attachment,
    AttachmentBlob,
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
from bots5.domain.search import (
    SearchBranchState,
    SearchDocumentKind,
    SearchFilters,
    SearchIndexCondition,
    SearchLocation,
    SearchNavigation,
    SearchPage,
    SearchResult,
    SearchStatus,
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
    attachment_blobs,
    attachments,
    message_attachments,
    attempt_attachments,
    context_plans,
    search_document_keys,
    search_index_state,
    search_source_state,
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
    arm_phase6_attachment_delete,
    arm_phase6_attachment_insert,
    arm_phase6_blob_delete,
    arm_phase6_blob_transition,
    clear_transition,
    clear_phase6,
    arm_phase7_source_mutation,
    clear_phase7_source_mutation,
    require_phase7_consumed,
    require_phase6_consumed,
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
from .phase6_schema import validate_phase6_schema
from .phase7_schema import (
    PHASE7_REVISION,
    SEARCH_SCHEMA_VERSION,
    SEARCH_TOKENIZER_VERSION,
    validate_phase7_schema,
)
from .phase7_validation import validate_phase7_rebuild
from .search import (
    ReceiptCoordinator,
    SearchProjection,
    SearchReceipt,
    bind_cursor_fingerprint,
    build_search_statement,
    compile_literal_query,
    decode_cursor,
    deterministic_rowid,
    encode_cursor,
    replace_projection,
    validate_limit,
)
from bots5.infrastructure.attachments import (
    AttachmentCleanupUncertain,
    AttachmentIntegrityError,
    _open_attachment_fs,
    classify_attachment_text,
    digest_to_text,
)


_TEST_FAULT_HOOK = None
_SQLITE_MAX_INTEGER = (1 << 63) - 1
_PHASE7_PENDING_SOURCE_COMMIT = "bots5_phase7_pending_source_commit"


def _fault(point: str) -> None:
    hook = _TEST_FAULT_HOOK
    if hook is not None:
        hook(point)
from bots5.infrastructure.data_root_authority import DataRootAuthority


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


_PHASE5_SCHEMA_SHA256 = {
    "application_generation_config": "24838857aacc21317eac1aa863996d839ed04553cefe0ad17f44523218f60cc7",
    "capability_facts": "affb4a3d9ebd17196e6f44692c82b7d6acdfb98398b5139e0bf2b1df23728fe6",
    "capability_observations": "85f8037280af9a0e4a113aecd85c6c29e6bd4bce0ea50d3afa2fdc4b90dd4b0b",
    "capability_overrides": "e4e0c27cf04cd8a39ff1393447ac06657fec7392c30e5e69deaca896b0017ca9",
    "catalogue_refresh_state": "01030b9386e41d2d7d5e4cc40b942e0ed974eb5696f6c2947376df00d1559f7a",
    "chat_model_selection": "88e16544fdea1ca4b7986ee9566c94891969fbaf0ab37fe005add2ff4a3f978e",
    "generation_attempt_phase5_completion_insert": "62784f9198898e5e7015711ab00ae25bef35879e66ec3d941c13f7f112b37ff3",
    "generation_attempt_phase5_completion_update": "d849c6547dd03cd993c83e66ac5cf2bc8799f988b88110032af63172c751828a",
    "model_catalogue_entries": "96b801e2b96fb5c6905364b81533039943a4a3ae51e4be68a6b32f77b639d7e8",
    "phase5_application_default_model_validate_insert": "207c3c2daaae4579e7844a95e4d193703ff52477d05782a62e0c9a92f98ca9bb",
    "phase5_application_default_model_validate_update": "03acd7e8fe3ff5ccd4cfac5595c46650bc1418a224bfa051b58aee9fbf641270",
    "phase5_application_settings_validate_insert": "d45b517f05c6183044b9049c1ae2867c9d1d62a20706672d6270b6a57a60a333",
    "phase5_application_settings_validate_update": "2f04af0ee5d4b5babbfe51c4d1ac8f6f678d7947541fa5d6d84735b68710e199",
    "phase5_attempt_attribution_insert": "7c8c694d796a7e239c2afeae5cd4fcf8bbf01c314ffd8d4eea84afcc576476b6",
    "phase5_attempt_attribution_update": "5ea29ad477a2081800047acbb1e1ea322696884cd0780113d5c06f2f4d9a6f4b",
    "phase5_capability_fact_identity_insert": "21315d4401a57ed45a28d8ddc8a45630ea892c7f9a9c890ebd039d1b63361d12",
    "phase5_capability_fact_identity_update": "772df4718eb7099e0581a3a647cd8151e9050bbb8f4ae071a9dbc699600e84b4",
    "phase5_capability_fact_provenance_validate_insert": "86502b0c61f5294c73127de6b33adc546c3353f0d56a87ba3a3b4fd97f9cff65",
    "phase5_capability_fact_provenance_validate_update": "983ea07f11c1711bc86c827554ff873ae298e386790ac43070c149f4f7d1a532",
    "phase5_capability_fact_truth_validate_insert": "330a734e65207119ceddc23e7824134a7ce66ed4aeb4aec9dda787cfd44e0fed",
    "phase5_capability_fact_truth_validate_update": "21e3223add32399c4b72706f227bf422857079263d55100006c1516beedc114d",
    "phase5_capability_observation_truth_validate_insert": "146b616421603ec063f1918bec89805635bd1a9170da4248b05faa82ad696f35",
    "phase5_capability_observation_truth_validate_update": "0a297ac6878d55a493d97a44e7e297ee0503cb1b526110bfb4ae4aea71eef58d",
    "phase5_capability_override_truth_validate_insert": "6f3ca3581a56fdb04bb4efe40646dec8c43108fa33ef2b196c58f1ce8dd42ad4",
    "phase5_capability_override_truth_validate_update": "757911b86803cab2bc8e4070c00cbbeda0a6451ef4f183942da1ad584e26e1a3",
    "phase5_catalogue_refresh_revision_guard": "2b390f76fbf6742039417c7ed3594ad68703007f0a7a320d68ae68ec3d0f7246",
    "phase5_catalogue_refresh_state_delete_guard": "5408083076fcd9f5af3c9692213faf4961c9f69e3a3876105a9e25cf82b91295",
    "phase5_catalogue_refresh_state_insert": "c651dfb53770ff9296eb72f9d0cc3f2b355861644612371c85a331db6a2b2236",
    "phase5_chat_model_settings_validate_insert": "3519374a89ff5a2972878082c1100ebc28700c73a62b93687ec0a0237d3a9148",
    "phase5_chat_model_settings_validate_update": "e21dcb1d4f6874f82f7b6ca2dbae7760c855b0c31dfd6013282db795814d558a",
    "phase5_model_catalogue_delete_referenced": "7ac64a45e6a37191416ef98f719420bc2008c394841229f8b5db1abcc96a4f3d",
    "phase5_model_catalogue_identity_referenced": "eee48120fe3e5999788eb72e09a1a5b7686bdc4b9efbb59a4fac0a1327e38cad",
    "phase5_model_catalogue_metadata_validate_insert": "d2e4be011c79316ccc454513ba3ec33dfb9963f3f589d688595570d22622ab7d",
    "phase5_model_catalogue_metadata_validate_update": "ce11f19de46b477924cda9d2235c1673df56c9620dae28ecf6c7223bf39ffcc7",
    "phase5_model_settings_validate_insert": "dbddc795705bba4ea94d4eb12389af11f0f358f9169c6d8057b40fd0cd9af710",
    "phase5_model_settings_validate_update": "22592e9b57cc26092e7039c8aa9592c11ffbb12eecbf9891748be90dc0a35003",
    "phase5_provider_connection_catalogue_revision_guard": "dbf71eddfb7504e3a06b3573b962a4eed40e16b25685e2d9492405176e2cfe3d",
    "phase5_provider_connection_delete_referenced": "bcbf4a8a3c22a7da607f64a5b45b5f562ee6f223cd3ac8f02cedf59a35eb2d3b",
    "phase5_provider_connection_identity_guard": "a65cf2c82a6499bae9fd2a6f227584f533ccba6ac98bdb7db2fae6afe12c74de",
    "phase5_provider_connection_resurrection_guard": "b52fc7001a849427ad9b68da255fda1554d5116e81e2df8e96ca94ce584fcab7",
    "phase5_provider_connection_retirement_guard": "ac43d8a74b3d5bcfe744e13b16b9e22ab75f0aef52599969d3ad0b2e57da6529",
    "phase5_provider_connection_validate_insert": "bba8974c244355c3e1143de4c577f30602a1f94c2a801db8f0c3d86c30fa2ca7",
    "phase5_provider_connection_validate_update": "7a070de7f673c756ced9e94ba509c727f9bfade1651ca5a719d3fb19081aad56",
    "provider_connections": "9cf64f0bda172e5def4815b660ee326a207482f1da46b7bb347fc7832f8edd82",
}


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
        phase5=(
            (_is_persisted_phase5_attempt(attempt) or _is_persisted_phase6_attempt(attempt))
            if phase5 is None
            else phase5
        ),
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


def _is_persisted_phase6_attempt(attempt: GenerationAttempt) -> bool:
    try:
        snapshot = json.loads(attempt.request_snapshot)
    except (TypeError, ValueError):
        return False
    return isinstance(snapshot, dict) and snapshot.get("snapshot_version") == 3


def _validate_phase6_attempt_authority(connection, attempt: GenerationAttempt) -> None:
    """CAS-check every durable fact used by a frozen v3 plan.

    The request snapshot is built outside the persistence transaction.  A
    connection/model check alone is insufficient: capability facts, manual
    overrides, or any settings layer can change between planning and start.
    Compare their resolved values and revisions while the start CAS is still
    open so a stale plan rolls back before events or backend dispatch.
    """
    try:
        snapshot = json.loads(attempt.request_snapshot)
    except (TypeError, ValueError):
        return
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_version") != 3:
        return
    connection_id = snapshot.get("connection_id")
    model_entry_id = snapshot.get("model_entry_id")
    if connection_id != attempt.connection_id or model_entry_id != attempt.model_entry_id:
        raise StateError("Phase 6 request attribution is inconsistent")
    row = connection.execute(
        select(
            provider_connections.c.id,
            provider_connections.c.revision,
            provider_connections.c.catalogue_revision,
            provider_connections.c.profile,
            provider_connections.c.endpoint,
            provider_connections.c.credential_source,
            provider_connections.c.credential_reference,
            model_catalogue_entries.c.id.label("model_id"),
            model_catalogue_entries.c.connection_id.label("model_connection_id"),
            model_catalogue_entries.c.provider_model_id,
        )
        .select_from(
            provider_connections.join(
                model_catalogue_entries,
                model_catalogue_entries.c.connection_id == provider_connections.c.id,
            )
        )
        .where(provider_connections.c.id == connection_id, model_catalogue_entries.c.id == model_entry_id)
    ).first()
    if row is None:
        raise StateError("Phase 6 selected provider/model disappeared before start")
    mapping = row._mapping
    expected = {
        "connection_revision": mapping["revision"],
        "catalogue_revision": mapping["catalogue_revision"],
        "provider_profile": mapping["profile"],
        "endpoint": mapping["endpoint"],
        "credential_source": mapping["credential_source"],
        "credential_reference": mapping["credential_reference"],
        "model": mapping["provider_model_id"],
    }
    if any(snapshot.get(key) != value for key, value in expected.items()):
        raise StateError("Phase 6 provider/model configuration changed before start")
    selection = connection.execute(
        select(chat_model_selection.c.model_entry_id, chat_model_selection.c.selection_required)
        .where(chat_model_selection.c.chat_id == attempt.chat_id)
    ).first()
    if selection is None or selection.model_entry_id != model_entry_id or selection.selection_required:
        raise StateError("Phase 6 chat model selection changed before start")

    # Compare the effective settings plus each contributing layer revision.
    app_row = connection.execute(
        select(application_generation_config).where(application_generation_config.c.id == 1)
    ).first()
    model_row = connection.execute(
        select(model_generation_config).where(model_generation_config.c.model_entry_id == model_entry_id)
    ).first()
    chat_row = connection.execute(
        select(chat_model_generation_config).where(
            chat_model_generation_config.c.chat_id == attempt.chat_id,
            chat_model_generation_config.c.model_entry_id == model_entry_id,
        )
    ).first()
    if app_row is None:
        raise StateError("Phase 6 application settings disappeared before start")
    app = app_row._mapping
    model_settings = None if model_row is None else model_row._mapping
    chat_settings = None if chat_row is None else chat_row._mapping
    defaults = {
        "temperature": 0.0,
        "max_output_tokens": 1024,
        "reasoning_effort": None,
        "timeout_seconds": None,
    }
    effective: dict[str, object] = {}
    provenance: dict[str, str] = {}
    for key, default in defaults.items():
        value = None
        source = "application"
        if chat_settings is not None and chat_settings[key] is not None:
            value = chat_settings[key]
            source = "chat_model"
        elif model_settings is not None and model_settings[key] is not None:
            value = model_settings[key]
            source = "model"
        elif app[key] is not None:
            value = app[key]
        else:
            value = default
        if key in {"temperature", "timeout_seconds"} and value is not None:
            value = float(value)
        effective[key] = value
        provenance[key] = source
    expected_settings = snapshot.get("effective_settings")
    expected_provenance = snapshot.get("settings_provenance")
    if expected_settings != effective or expected_provenance != provenance:
        raise StateError("Phase 6 generation settings changed before start")
    expected_revisions = snapshot.get("settings_revisions")
    actual_revisions = {
        "application": int(app["revision"]),
        "model": None if model_settings is None else int(model_settings["revision"]),
        "chat": None if chat_settings is None else int(chat_settings["revision"]),
    }
    if expected_revisions != actual_revisions:
        raise StateError("Phase 6 generation settings revision changed before start")

    # Resolve the closed capability vocabulary in the same precedence order as
    # ProviderConfiguration, then compare both truth and provenance.  This
    # intentionally reads the rows inside the start transaction rather than
    # trusting an object retained by the caller.
    precedence = {
        "manual": 0,
        "confirmed_endpoint": 1,
        "provider_metadata": 2,
        "trusted_registry": 3,
        "heuristic": 4,
        "unknown": 5,
    }
    catalogue_revision = int(mapping["catalogue_revision"])
    facts = []
    for fact_row in connection.execute(
        select(capability_facts).where(capability_facts.c.model_entry_id == model_entry_id)
    ).fetchall():
        fact = fact_row._mapping
        source = str(fact["source"])
        if source in {"confirmed_endpoint", "provider_metadata"} and fact["source_revision"] is not None and int(fact["source_revision"]) != catalogue_revision:
            continue
        provenance_value = json.loads(fact["provenance_json"] or "{}")
        facts.append((
            fact,
            provenance_value,
            (
                precedence[source],
                0 if fact["source_revision"] is not None else 1,
                -(int(fact["source_revision"]) if fact["source_revision"] is not None else 0),
                json.dumps(provenance_value, sort_keys=True, separators=(",", ":")),
                str(fact["state"]),
                fact["value"] is None,
                -1 if fact["value"] is None else int(fact["value"]),
            ),
        ))
    overrides = {
        str(row._mapping["capability_key"]): row._mapping
        for row in connection.execute(
            select(capability_overrides).where(capability_overrides.c.model_entry_id == model_entry_id)
        ).fetchall()
    }
    resolved = []
    for key in sorted(CAPABILITY_KEYS):
        override = overrides.get(key)
        if override is not None:
            state = str(override["state"])
            source = "manual"
            source_revision = int(override["revision"])
            value = override["value"]
            provenance_value = {"reason": override["reason"] or "manual override"}
        else:
            candidates = [item for item in facts if str(item[0]["capability_key"]) == key]
            if not candidates:
                state, source, source_revision, value, provenance_value = "unknown", "unknown", None, None, {}
            else:
                fact, provenance_value, _sort_key = min(candidates, key=lambda item: item[2])
                state = str(fact["state"])
                source = str(fact["source"])
                source_revision = None if fact["source_revision"] is None else int(fact["source_revision"])
                value = fact["value"]
        resolved.append({
            "key": key,
            "state": state,
            "source": source,
            "source_revision": source_revision,
            "value": value,
        })
    if snapshot.get("capabilities") != resolved:
        raise StateError("Phase 6 capability facts changed before start")
    expected_provenance = snapshot.get("capability_provenance")
    actual_provenance = {
        item["key"]: {"source": item["source"], **(
            ({"reason": overrides[item["key"]]["reason"] or "manual override"}
             if item["key"] in overrides else next(
                (candidate[1] for candidate in facts if str(candidate[0]["capability_key"]) == item["key"] and candidate[0]["source"] == item["source"] and candidate[0]["source_revision"] == item["source_revision"]),
                {},
            ))
        )}
        for item in resolved
    }
    if expected_provenance != actual_provenance:
        raise StateError("Phase 6 capability provenance changed before start")
    expected_overrides = snapshot.get("manual_overrides")
    actual_overrides = {
        key: {
            "state": str(value["state"]),
            "value": value["value"],
            "revision": int(value["revision"]),
        }
        for key, value in overrides.items()
    }
    if expected_overrides != actual_overrides:
        raise StateError("Phase 6 capability overrides changed before start")


def _engine(authority: DataRootAuthority) -> Engine:
    authority.assert_live()
    vfs = authority._open_rooted_vfs()
    engine = create_engine(
        "sqlite://",
        creator=vfs.connect,
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record):
        dbapi_connection.enable_load_extension(False)
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
        install_transition_guard(dbapi_connection, connection_record)
        dbapi_connection.set_authorizer(_authorise_application_sql)

    @event.listens_for(engine, "checkout")
    def _admit_connection(dbapi_connection, connection_record, connection_proxy):
        del dbapi_connection, connection_record, connection_proxy
        authority._connection_checkout()

    @event.listens_for(engine, "checkin")
    def _release_connection(dbapi_connection, connection_record):
        del dbapi_connection, connection_record
        authority._connection_checkin()

    return engine


_SCHEMA_AUTHOR_ACTIONS = frozenset(
    value
    for value in (
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_REINDEX", None),
        getattr(sqlite3, "SQLITE_ANALYZE", None),
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
    )
    if value is not None
)
_MUTATING_PRAGMAS = frozenset(
    {
        "foreign_keys",
        "journal_mode",
        "legacy_alter_table",
        "locking_mode",
        "query_only",
        "synchronous",
        "temp_store",
        "temp_store_directory",
        "trusted_schema",
        "wal_autocheckpoint",
        "writable_schema",
    }
)


def _authorise_application_sql(
    action: int,
    arg1: str | None,
    arg2: str | None,
    database_name: str | None,
    trigger_name: str | None,
) -> int:
    del database_name, trigger_name
    if action in _SCHEMA_AUTHOR_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        pragma = "" if arg1 is None else arg1.casefold()
        if pragma in _MUTATING_PRAGMAS and arg2 is not None:
            return sqlite3.SQLITE_DENY
    if (
        action == sqlite3.SQLITE_FUNCTION
        and (arg2 or arg1 or "").casefold() == "load_extension"
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _validate_open_connection(
    connection,
    *,
    expected_revision: str,
    destructive_phase6: bool = True,
    require_fts: bool = True,
    allow_missing_search_index_state: bool = False,
    allow_repairable_search_index_version: bool = False,
) -> None:
    """Validate the exact authoritative schema and destructive guard behavior."""
    phase7 = expected_revision == PHASE7_REVISION
    if phase7:
        arm_phase7_source_mutation(connection, "phase7 schema validation")
    try:
        integrity = __import__(
            "bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries",
            fromlist=["_validate_existing_state"],
        )
        integrity._validate_existing_state(connection)
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one_or_none()
        if revision != expected_revision:
            raise RuntimeError("current database revision is not authoritative")
        _validate_phase4_schema(connection)
        _validate_phase5_schema(connection)
        _validate_phase5_trigger_behavior(connection)
        validate_phase6_schema(connection, destructive=destructive_phase6)
    finally:
        if phase7:
            clear_phase7_source_mutation(connection)
    if phase7:
        validate_phase7_schema(
            connection,
            require_fts=require_fts,
            expensive=destructive_phase6,
            allow_missing_index_state=allow_missing_search_index_state,
            allow_repairable_index_version=allow_repairable_search_index_version,
        )


def _chat(row) -> Chat:
    archived_at = getattr(row, "archived_at", None)
    return Chat(
        id=row.id,
        title=row.title,
        created_at=parse_utc(row.created_at),
        updated_at=parse_utc(row.updated_at),
        head_message_id=row.head_message_id,
        revision=int(row.revision),
        archived_at=None if archived_at is None else parse_utc(archived_at),
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


def _attachment(row) -> Attachment:
    mapping = row._mapping
    try:
        value = Attachment(
            id=mapping["id"],
            blob_digest=digest_to_text(mapping["blob_digest"]),
            filename=mapping["filename"],
            source_kind=mapping["source_kind"],
            source_name=mapping["source_name"],
            text_representation_id=(
                None
                if mapping["text_representation_id"] is None
                else digest_to_text(mapping["text_representation_id"])
            ),
            text_digest=(
                None
                if mapping["text_digest"] is None
                else digest_to_text(mapping["text_digest"])
            ),
            ineligibility_reason=mapping["ineligibility_reason"],
            created_at=parse_utc(mapping["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("attachment record is malformed") from exc
    if len(value.blob_digest) != 64 or len(value.filename) > 255:
        raise StateError("attachment record is malformed")
    return value


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
    current_schema = {
        str(name): hashlib.sha256(
            _normalise_sql_fragment(str(sql or "")).encode("utf-8")
        ).hexdigest()
        for name, sql in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master "
            "WHERE name IN ({})".format(",".join("?" for _ in _PHASE5_SCHEMA_OBJECTS)),
            _PHASE5_SCHEMA_OBJECTS,
        ).fetchall()
    }
    for name in _PHASE5_SCHEMA_OBJECTS:
        if current_schema.get(name) != _PHASE5_SCHEMA_SHA256.get(name):
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
                "WHERE (json_extract(a.request_snapshot, '$.snapshot_version') IN (2, 3) "
            "AND (a.connection_id IS NULL OR a.model_entry_id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM model_catalogue_entries AS m "
            "JOIN provider_connections AS p ON p.id = m.connection_id "
            "WHERE m.id = a.model_entry_id AND m.connection_id = a.connection_id "
            "AND m.provider_model_id = json_extract(a.request_snapshot, '$.model') "
            "AND p.id = json_extract(a.request_snapshot, '$.connection_id')"
                "))) OR (COALESCE(json_extract(a.request_snapshot, '$.snapshot_version'), 0) NOT IN (2, 3) "
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
    _CONSTRUCTION_KEY = object()

    def __init__(
        self,
        engine: Engine,
        *,
        authority: DataRootAuthority,
        _construction_key: object,
        search_available: bool = True,
        search_generation: int = 0,
        search_source_revision: int = 0,
    ):
        if _construction_key is not self._CONSTRUCTION_KEY:
            raise TypeError("SQLiteAppStateStore is constructed only by DataRootAuthority")
        self._engine = engine
        self._closed = False
        self._poisoned = False
        self._authority = authority
        self._attachment_manager = _open_attachment_fs(authority)
        self._search_available = search_available
        self._search_receipts = ReceiptCoordinator()
        self._search_generation_high_water = search_generation
        self._search_source_revision_lock = RLock()
        self._search_source_revision_high_water = search_source_revision
        self._search_cursor_epoch = str(uuid7())
        authority.register_store(self)

    @classmethod
    def open(cls, *args, **kwargs) -> SQLiteAppStateStore:
        del args, kwargs
        raise TypeError("open stores through DataRootAuthority.open_store()")

    @classmethod
    def _open_from_authority(
        cls, authority: DataRootAuthority
    ) -> SQLiteAppStateStore:
        authority.assert_live()
        authority._claim_database()
        lease = authority
        engine = _engine(authority)
        search_available = True
        search_generation = 0
        search_source_revision = 0
        try:
            lease.assert_live()
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
                if revision == "0009_phase6_context_attachments":
                    _validate_phase4_schema(connection)
                    _validate_phase5_schema(connection)
                    _validate_phase5_trigger_behavior(connection)
                    validate_phase6_schema(connection)
                if revision == PHASE7_REVISION:
                    _validate_open_connection(
                        connection,
                        expected_revision=PHASE7_REVISION,
                        destructive_phase6=False,
                        require_fts=False,
                        allow_missing_search_index_state=True,
                        allow_repairable_search_index_version=True,
                    )
                    try:
                        connection.exec_driver_sql(
                            "SELECT rowid FROM search_fts "
                            "WHERE search_fts MATCH ? LIMIT 0",
                            ('"probe"',),
                        ).fetchall()
                    except DBAPIError as exc:
                        if "no such module: fts5" not in str(exc).casefold():
                            raise
                        search_available = False
                    stored_generation = connection.exec_driver_sql(
                        "SELECT generation FROM search_index_state WHERE singleton_id=1"
                    ).scalar_one_or_none()
                    search_generation = (
                        int(stored_generation)
                        if type(stored_generation) is int and stored_generation >= 0
                        else 0
                    )
                    stored_source_revision = connection.exec_driver_sql(
                        "SELECT source_revision FROM search_source_state "
                        "WHERE singleton_id=1"
                    ).scalar_one()
                    if (
                        type(stored_source_revision) is not int
                        or stored_source_revision < 0
                        or stored_source_revision > _SQLITE_MAX_INTEGER
                    ):
                        raise RuntimeError(
                            "current Phase 7 source singleton is malformed"
                        )
                    search_source_revision = stored_source_revision
                if "provider_id" in columns:
                    outcomes = __import__(
                        "bots5.infrastructure.persistence.migrations.versions.0005_generation_outcomes",
                        fromlist=["_validate_outcome_rows"],
                    )
                    outcomes._validate_outcome_rows(connection)
        except BaseException:
            engine.dispose()
            raise
        # SQLite has now drained any hot-journal recovery.  Attachment intent
        # is interpreted only after the recovered main inode and its namespace
        # have crossed the explicit core-owned durability handoff.
        authority.database_durability_fence()
        try:
            store = cls(
                engine,
                authority=lease,
                _construction_key=cls._CONSTRUCTION_KEY,
                search_available=search_available,
                search_generation=search_generation,
                search_source_revision=search_source_revision,
            )
        except BaseException:
            engine.dispose()
            raise
        try:
            with engine.connect() as connection:
                phase6_tables = connection.exec_driver_sql(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
                    "('attachment_blobs','attachments','message_attachments','attempt_attachments','context_plans')"
                ).scalar_one()
            if phase6_tables:
                store._reconcile_attachments_startup()
        except BaseException as exc:
            # Startup verification has failed.  Release existing bearers only;
            # do not open another database connection from the caught path.
            store._release_after_invalidation()
            raise RuntimeError("durable Phase 6 attachment storage failed verification") from exc
        lease.publish_ready(store)
        return store

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateError("state store is closed")

    def assert_admitting(self) -> None:
        """Reject every application command after an uncertain lifecycle commit."""
        with self.command_admission():
            pass

    @contextmanager
    def command_admission(self, *, independent: bool = False):
        """Hold authoritative admission for one complete application command."""
        self._ensure_open()
        try:
            with self._authority.application_operation(independent=independent):
                yield
        except AuthorityError as exc:
            raise StateError(
                "state store is not admitting work; fresh-authority recovery is required"
            ) from exc

    @contextmanager
    def event_admission(self, *, independent: bool = False):
        with self.command_admission(independent=independent):
            yield

    @contextmanager
    def issued_event_effect(self):
        try:
            with self._authority.issued_effect():
                yield
        except AuthorityError as exc:
            raise StateError("event publication authority is unavailable") from exc

    def _commit_attachment_transaction(self, connection, operation: str) -> None:
        """Classify an attachment-consequential commit at its only call site."""
        connection_info = getattr(connection, "info", None)
        pending_revision = (
            None
            if connection_info is None
            else connection_info.get(_PHASE7_PENDING_SOURCE_COMMIT)
        )
        lock = (
            self._search_source_revision_lock
            if pending_revision is not None
            else nullcontext()
        )
        with lock:
            try:
                connection.commit()
            except BaseException as exc:
                if connection_info is not None:
                    connection_info.pop(_PHASE7_PENDING_SOURCE_COMMIT, None)
                self._poisoned = True
                self._authority.poison(
                    f"uncertain attachment transaction commit: {operation}"
                )
                try:
                    connection.rollback()
                except BaseException:
                    pass
                raise StateError(
                    "attachment transaction outcome is uncertain; restart recovery is required"
                ) from exc
            if pending_revision is not None:
                assert connection_info is not None
                connection_info.pop(_PHASE7_PENDING_SOURCE_COMMIT, None)
                self._record_committed_search_source_revision(pending_revision)

    def _rollback_attachment_transaction(self, connection, operation: str) -> None:
        """Establish a known abort or poison when rollback itself is uncertain."""
        try:
            connection.rollback()
        except BaseException as exc:
            self._poisoned = True
            self._authority.poison(
                f"attachment transaction rollback failed: {operation}"
            )
            raise StateError(
                "attachment transaction rollback is uncertain; restart recovery is required"
            ) from exc
        finally:
            connection_info = getattr(connection, "info", None)
            if connection_info is not None:
                connection_info.pop(_PHASE7_PENDING_SOURCE_COMMIT, None)

    def _poison_attachment_lifecycle(self, operation: str) -> None:
        self._poisoned = True
        self._authority.poison(f"attachment lifecycle failure: {operation}")

    def _close_attachment_connection(self, connection, operation: str) -> None:
        """Classify an uncertain close at a live attachment boundary."""
        try:
            connection.close()
        except BaseException as exc:
            self._poison_attachment_lifecycle(
                f"{operation} database connection close outcome uncertain"
            )
            raise StateError(
                "attachment database connection close is uncertain; "
                "restart recovery is required"
            ) from exc

    def _discard_capture_or_poison(self, captured, operation: str) -> None:
        try:
            self._attachment_manager.discard_capture(captured.operation_id)
        except BaseException as exc:
            self._poison_attachment_lifecycle(f"{operation} capture cleanup failed")
            raise StateError(
                "attachment capture cleanup failed; restart recovery is required"
            ) from exc

    def _rollback_and_discard_capture(self, connection, captured, operation: str) -> None:
        self._rollback_attachment_transaction(connection, operation)
        self._discard_capture_or_poison(captured, operation)

    def _ensure_phase6(self) -> None:
        """Require the normal Phase 6 current schema; never repair lazily."""
        with self._engine.connect() as connection:
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one_or_none()
            if revision != PHASE7_REVISION:
                raise StateError("Phase 7 persistence schema is not current")
            validate_phase6_schema(connection)
            validate_phase7_schema(
                connection,
                require_fts=self._search_available,
                expensive=False,
            )

    @contextmanager
    def _search_source_transaction(
        self,
        operation: str,
        document_keys: tuple[str, ...],
    ):
        """Commit business data and its one source revision atomically."""
        revision: int | None = None
        with self._authority.transition():
            with self._search_transaction(f"{operation} source transaction") as connection:
                self._arm_phase7_source_mutation(connection, operation)
                try:
                    yield connection
                    revision = self._require_phase7_source_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
        assert revision is not None
        self._accept_search_receipt(
            SearchReceipt(revision, frozenset(document_keys))
        )

    def create_chat(self, chat: Chat) -> None:
        self._ensure_open()
        if chat.revision != 0 or chat.head_message_id is not None:
            raise StateError("new chats must start at revision 0 without a head")
        with self._search_source_transaction(
            "create chat", (f"chat:{chat.id}",)
        ) as connection:
            connection.execute(
                insert(chats).values(
                    id=chat.id,
                    title=chat.title,
                    created_at=utc_iso(chat.created_at),
                    updated_at=utc_iso(chat.updated_at),
                    head_message_id=chat.head_message_id,
                    revision=chat.revision,
                    archived_at=(
                        None if chat.archived_at is None else utc_iso(chat.archived_at)
                    ),
                )
            )

    def archive_chat(
        self,
        chat_id: str,
        archived_at,
        *,
        expected_revision: int | None = None,
    ) -> Chat:
        self._ensure_open()
        if not chat_id:
            raise StateError("chat id must not be empty")
        archived_value = None if archived_at is None else utc_iso(archived_at)
        result_chat: Chat | None = None
        source_revision: int | None = None
        with self._authority.transition():
            with self._search_transaction("archive chat source transaction") as connection:
                row = connection.execute(select(chats).where(chats.c.id == chat_id)).first()
                if row is None:
                    raise StateError(f"chat not found: {chat_id}")
                current = _chat(row)
                if expected_revision is not None and current.revision != expected_revision:
                    raise RevisionConflict(f"chat revision changed: {chat_id}")
                if current.archived_at == archived_at:
                    result_chat = current
                else:
                    self._arm_phase7_source_mutation(
                        connection,
                        "archive chat" if archived_at is not None else "unarchive chat",
                    )
                    try:
                        updated_at = utc_iso(datetime.now(UTC))
                        update_result = connection.execute(
                            update(chats)
                            .where(
                                chats.c.id == chat_id,
                                chats.c.revision == current.revision,
                            )
                            .values(
                                archived_at=archived_value,
                                updated_at=updated_at,
                            )
                        )
                        if update_result.rowcount != 1:
                            raise RevisionConflict(f"chat revision changed: {chat_id}")
                        source_revision = self._require_phase7_source_consumed(connection)
                    finally:
                        clear_phase7_source_mutation(connection)
                    result_chat = replace(
                        current,
                        archived_at=archived_at,
                        updated_at=parse_utc(updated_at),
                    )
        if source_revision is not None:
            self._accept_search_receipt(
                SearchReceipt(source_revision, frozenset({f"chat:{chat_id}"}))
            )
        assert result_chat is not None
        return result_chat

    # ------------------------------------------------------------------
    # Phase 6 attachment authority
    def ingest_attachment(self, source, *, filename: str | None = None) -> Attachment:
        """Capture once, durably stage, publish, and create one reusable identity."""
        self._ensure_open()
        with self._authority.transition():
            try:
                captured = self._attachment_manager.capture(source, filename=filename)
            except AttachmentCleanupUncertain as exc:
                self._poison_attachment_lifecycle(
                    "capture cleanup durability is uncertain"
                )
                raise StateError(
                    "attachment capture cleanup durability is uncertain; "
                    "restart recovery is required"
                ) from exc
            except AttachmentIntegrityError as exc:
                raise StateError(str(exc)) from exc
            created_at = utc_iso(datetime.now(UTC))
            attachment_id = str(uuid7())
            # The non-reentrant transition gate is owned continuously from
            # source capture through the final durable lifecycle commit.
            with __import__("contextlib").nullcontext():
                connection = None
                transaction_committed = False
                try:
                    connection = self._engine.connect()
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        select(attachment_blobs).where(
                            attachment_blobs.c.digest == captured.digest
                        )
                    ).first()
                    if existing is not None:
                        if existing.state != "ready" or int(existing.byte_size) != captured.byte_size:
                            self._rollback_attachment_transaction(
                                connection, "T1 abnormal existing lifecycle row"
                            )
                            self._poisoned = True
                            self._authority.poison("live attachment lifecycle row is not ready")
                            raise StateError("attachment lifecycle requires restart recovery")
                        self._attachment_manager.read_verified(
                            captured.digest, expected_size=captured.byte_size
                        )
                        search_revision = None
                        self._arm_phase7_source_mutation(
                            connection, "insert deduplicated attachment"
                        )
                        try:
                            arm_phase6_attachment_insert(connection, attachment_id)
                            try:
                                result = connection.execute(
                                    insert(attachments).values(
                                        id=attachment_id,
                                        blob_digest=captured.digest,
                                        filename=captured.filename,
                                        source_kind="filesystem",
                                        source_name=captured.filename,
                                        text_representation_id=captured.representation_id,
                                        text_digest=captured.representation_id,
                                        ineligibility_reason=captured.ineligibility_reason,
                                        created_at=created_at,
                                    )
                                )
                                require_phase6_consumed(connection)
                                if result.rowcount != 1:
                                    raise StateError("attachment insertion did not affect one row")
                            finally:
                                clear_phase6(connection)
                            search_revision = self._require_phase7_source_consumed(connection)
                        finally:
                            clear_phase7_source_mutation(connection)
                        row = connection.execute(
                            select(attachments, attachment_blobs.c.byte_size)
                            .select_from(
                                attachments.join(
                                    attachment_blobs,
                                    attachments.c.blob_digest == attachment_blobs.c.digest,
                                )
                            )
                            .where(attachments.c.id == attachment_id)
                        ).first()
                        self._commit_attachment_transaction(
                            connection, "T1 deduplicated attachment identity"
                        )
                        transaction_committed = True
                        try:
                            self._attachment_manager.discard_capture(captured.operation_id)
                        except BaseException as exc:
                            self._poisoned = True
                            self._authority.poison(
                                "deduplicated attachment cleanup failed after commit"
                            )
                            raise StateError(
                                "attachment cleanup failed; restart recovery is required"
                            ) from exc
                        assert search_revision is not None
                        self._accept_search_receipt(
                            SearchReceipt(
                                search_revision,
                                frozenset({f"attachment:{attachment_id}"}),
                            )
                        )
                        return self._attachment_from_authoritative_row(row)
                    arm_phase6_blob_transition(
                        connection,
                        captured.digest,
                        "",
                        "staging",
                        operation_id=captured.operation_id,
                        stage_name=captured.operation_id,
                        byte_size=captured.byte_size,
                    )
                    try:
                        result = connection.execute(
                            insert(attachment_blobs).values(
                                digest=captured.digest,
                                byte_size=captured.byte_size,
                                state="staging",
                                operation_id=captured.operation_id,
                                stage_name=captured.operation_id,
                                gc_id=None,
                                created_at=created_at,
                            )
                        )
                        require_phase6_consumed(connection)
                        if result.rowcount != 1:
                            raise StateError("blob staging did not affect one row")
                    finally:
                        clear_phase6(connection)
                    self._commit_attachment_transaction(
                        connection, "T2 initial staging intent"
                    )
                    transaction_committed = True
                except BaseException:
                    if not transaction_committed and not self._poisoned and connection is not None:
                        self._rollback_and_discard_capture(connection, captured, "T1/T2 known pre-commit abort")
                    elif not transaction_committed and not self._poisoned and connection is None:
                        self._discard_capture_or_poison(
                            captured, "T1/T2 checkout before transaction"
                        )
                    raise
                finally:
                    if connection is not None:
                        self._close_attachment_connection(
                            connection, "T1/T2 attachment transaction"
                        )
            _fault("after-staging-row-commit")
            try:
                self._attachment_manager.capture_to_stage(
                    captured.operation_id, captured.digest, captured.byte_size
                )
                self._attachment_manager.stage_to_object(
                    captured.operation_id, captured.digest, captured.byte_size
                )
                self._attachment_manager.prove_staging_publication(
                    captured.operation_id, captured.digest, captured.byte_size
                )
                connection = None
                commit_attempted = False
                try:
                    connection = self._engine.connect()
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    arm_phase6_blob_transition(
                        connection,
                        captured.digest,
                        "staging",
                        "ready",
                        byte_size=captured.byte_size,
                    )
                    try:
                        result = connection.execute(
                            update(attachment_blobs)
                            .where(
                                attachment_blobs.c.digest == captured.digest,
                                attachment_blobs.c.state == "staging",
                                attachment_blobs.c.operation_id == captured.operation_id,
                            )
                            .values(
                                state="ready",
                                operation_id=None,
                                stage_name=None,
                                gc_id=None,
                            )
                        )
                        require_phase6_consumed(connection)
                        if result.rowcount != 1:
                            raise StateError("blob publication did not affect one row")
                    finally:
                        clear_phase6(connection)
                    search_revision = None
                    self._arm_phase7_source_mutation(connection, "publish attachment")
                    try:
                        arm_phase6_attachment_insert(connection, attachment_id)
                        try:
                            result = connection.execute(
                                insert(attachments).values(
                                    id=attachment_id,
                                    blob_digest=captured.digest,
                                    filename=captured.filename,
                                    source_kind="filesystem",
                                    source_name=captured.filename,
                                    text_representation_id=captured.representation_id,
                                    text_digest=captured.representation_id,
                                    ineligibility_reason=captured.ineligibility_reason,
                                    created_at=created_at,
                                )
                            )
                            require_phase6_consumed(connection)
                            if result.rowcount != 1:
                                raise StateError("attachment insertion did not affect one row")
                        finally:
                            clear_phase6(connection)
                        search_revision = self._require_phase7_source_consumed(connection)
                    finally:
                        clear_phase7_source_mutation(connection)
                    row = connection.execute(
                        select(attachments, attachment_blobs.c.byte_size)
                        .select_from(
                            attachments.join(
                                attachment_blobs,
                                attachments.c.blob_digest == attachment_blobs.c.digest,
                            )
                        )
                        .where(attachments.c.id == attachment_id)
                    ).first()
                    _fault("before-ready-commit")
                    commit_attempted = True
                    self._commit_attachment_transaction(
                        connection, "T3 final publication"
                    )
                except BaseException:
                    if not commit_attempted and not self._poisoned and connection is not None:
                        self._rollback_attachment_transaction(
                            connection, "T3 final publication K0"
                        )
                        self._poison_attachment_lifecycle(
                            "T3 final publication known abort"
                        )
                    raise
                finally:
                    if connection is not None:
                        connection.close()
                _fault("after-ready-commit")
                assert search_revision is not None
                self._accept_search_receipt(
                    SearchReceipt(
                        search_revision,
                        frozenset({f"attachment:{attachment_id}"}),
                    )
                )
            except BaseException as exc:
                self._poisoned = True
                self._authority.poison("attachment publication failed after durable staging")
                raise StateError(
                    "attachment publication failed; restart recovery is required"
                ) from exc
        return self._attachment_from_authoritative_row(row)

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            row = connection.execute(
                select(attachments, attachment_blobs.c.byte_size)
                .select_from(
                    attachments.join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
                .where(attachments.c.id == attachment_id)
            ).first()
        return None if row is None else self._attachment_from_authoritative_row(row)

    def list_attachments(self) -> tuple[Attachment, ...]:
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            rows = connection.execute(
                select(attachments, attachment_blobs.c.byte_size)
                .select_from(
                    attachments.join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
                .order_by(attachments.c.created_at, attachments.c.id)
            ).fetchall()
        return tuple(self._attachment_from_authoritative_row(row) for row in rows)

    def delete_attachment(self, attachment_id: str) -> None:
        self._ensure_open()
        with self._authority.transition():
            connection = None
            commit_attempted = False
            try:
                connection = self._engine.connect()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                refs = connection.execute(
                    select(message_attachments.c.message_id).where(message_attachments.c.attachment_id == attachment_id)
                ).first()
                attempt_ref = connection.execute(
                    select(attempt_attachments.c.attempt_id).where(attempt_attachments.c.attachment_id == attachment_id)
                ).first()
                if refs is not None or attempt_ref is not None:
                    raise StateError("attachment has durable historical references")
                search_revision = None
                self._arm_phase7_source_mutation(connection, "delete attachment")
                try:
                    arm_phase6_attachment_delete(connection, attachment_id)
                    try:
                        result = connection.execute(
                            delete(attachments).where(attachments.c.id == attachment_id)
                        )
                        require_phase6_consumed(connection)
                    finally:
                        clear_phase6(connection)
                    if result.rowcount != 1:
                        raise StateError(f"attachment not found: {attachment_id}")
                    search_revision = self._require_phase7_source_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
                commit_attempted = True
                self._commit_attachment_transaction(
                    connection, "T4 reusable attachment deletion"
                )
                assert search_revision is not None
                self._accept_search_receipt(
                    SearchReceipt(
                        search_revision,
                        frozenset({f"attachment:{attachment_id}"}),
                    )
                )
            except BaseException:
                if not commit_attempted and not self._poisoned and connection is not None:
                    self._rollback_attachment_transaction(
                        connection, "T4 reusable attachment deletion K0"
                    )
                raise
            finally:
                if connection is not None:
                    self._close_attachment_connection(
                        connection, "T4 reusable attachment deletion"
                    )

    def read_attachment_bytes(self, attachment_id: str) -> bytes:
        self._ensure_open()
        with self._authority.transition():
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(
                        attachments.c.blob_digest,
                        attachments.c.text_representation_id,
                        attachments.c.text_digest,
                        attachments.c.ineligibility_reason,
                        attachment_blobs.c.byte_size,
                    )
                    .select_from(attachments.join(attachment_blobs, attachments.c.blob_digest == attachment_blobs.c.digest))
                    .where(attachments.c.id == attachment_id)
                ).first()
            if row is None:
                raise StateError(f"attachment not found: {attachment_id}")
            try:
                raw = self._attachment_manager.read_verified(
                    row.blob_digest, expected_size=row.byte_size
                )
            except AttachmentIntegrityError as exc:
                raise StateError(str(exc)) from exc
            self._validated_authoritative_attachment_representation(
                row,
                attachment_id,
                raw=raw,
            )
            return raw

    def list_message_attachments(self, message_id: str) -> tuple[Attachment, ...]:
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            rows = connection.execute(
                select(attachments)
                .add_columns(attachment_blobs.c.byte_size)
                .select_from(
                    message_attachments
                    .join(
                        attachments,
                        message_attachments.c.attachment_id == attachments.c.id,
                    )
                    .join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
                .where(message_attachments.c.message_id == message_id)
                .order_by(message_attachments.c.ordinal)
            ).fetchall()
        return tuple(self._attachment_from_authoritative_row(row) for row in rows)

    def list_attempt_attachments(self, attempt_id: str) -> tuple[Attachment, ...]:
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            rows = connection.execute(
                select(attachments)
                .add_columns(attachment_blobs.c.byte_size)
                .select_from(
                    attempt_attachments
                    .join(
                        attachments,
                        attempt_attachments.c.attachment_id == attachments.c.id,
                    )
                    .join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
                .where(attempt_attachments.c.attempt_id == attempt_id)
                .order_by(attempt_attachments.c.ordinal)
            ).fetchall()
        return tuple(self._attachment_from_authoritative_row(row) for row in rows)

    def gc_attachments(self) -> tuple[str, ...]:
        """Linearize deletion, durably mark deleting, then remove bytes."""
        self._ensure_open()
        removed: list[str] = []
        with self._authority.transition():
            rows = self._enumerate_unreferenced_blobs()
            for row in rows:
                gc_id = str(uuid7())
                connection = None
                commit_attempted = False
                try:
                    connection = self._engine.connect()
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    current = connection.execute(
                        select(attachment_blobs.c.state, attachment_blobs.c.byte_size)
                        .where(attachment_blobs.c.digest == row.digest)
                    ).first()
                    if current is None or current.state != "ready" or connection.execute(
                        select(attachments.c.id).where(attachments.c.blob_digest == row.digest)
                    ).first() is not None:
                        self._rollback_attachment_transaction(
                            connection, "T7 GC authorization no-op K0"
                        )
                        continue
                    self._attachment_manager.read_verified(
                        row.digest, expected_size=int(row.byte_size)
                    )
                    arm_phase6_blob_transition(
                        connection,
                        row.digest,
                        "ready",
                        "deleting",
                        gc_id=gc_id,
                        byte_size=int(row.byte_size),
                    )
                    try:
                        result = connection.execute(
                            update(attachment_blobs)
                            .where(attachment_blobs.c.digest == row.digest)
                            .values(
                                state="deleting",
                                operation_id=None,
                                stage_name=None,
                                gc_id=gc_id,
                            )
                        )
                        require_phase6_consumed(connection)
                        if result.rowcount != 1:
                            raise StateError("GC intent did not affect one row")
                    finally:
                        clear_phase6(connection)
                    _fault("before-gc-deleting-commit")
                    commit_attempted = True
                    self._commit_attachment_transaction(
                        connection, "T7 GC authorization"
                    )
                    _fault("after-gc-deleting-commit")
                except BaseException:
                    if not commit_attempted and not self._poisoned and connection is not None:
                        self._rollback_attachment_transaction(
                            connection, "T7 GC authorization K0"
                        )
                    raise
                finally:
                    if connection is not None:
                        self._close_attachment_connection(
                            connection, "T7 GC authorization"
                        )
                try:
                    self._authority.database_durability_fence()
                    self._recover_deleting_blob(
                        bytes(row.digest), int(row.byte_size), gc_id
                    )
                except BaseException as exc:
                    self._poisoned = True
                    self._authority.poison(
                        "attachment garbage collection failed after deleting intent"
                    )
                    raise StateError(
                        "attachment garbage collection failed; deleting intent retained"
                    ) from exc
                removed.append(digest_to_text(row.digest))
        return tuple(removed)

    def _enumerate_unreferenced_blobs(self):
        """Return a disposable candidate set without retaining a read snapshot."""
        with self._engine.connect() as connection:
            return connection.execute(
                select(attachment_blobs.c.digest, attachment_blobs.c.byte_size)
                .where(attachment_blobs.c.state == "ready")
                .where(~attachment_blobs.c.digest.in_(select(attachments.c.blob_digest)))
            ).fetchall()

    def _reconcile_attachments_startup(self) -> tuple[str, ...]:
        """Private pre-READY recovery; never exposed as a second manager."""
        with self._authority._transition_gate:
            return self._reconcile_attachments_locked()

    def _reconcile_attachments_locked(self) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(select(attachment_blobs)).fetchall()
        try:
            self._recover_attachment_rows(rows)
            self._validate_attachment_payload_rows()
            return ()
        except AttachmentIntegrityError as exc:
            raise StateError(str(exc)) from exc

    def _recover_attachment_rows(self, rows) -> None:
        with self._engine.connect() as connection:
            dangling = connection.exec_driver_sql(
                "SELECT 1 FROM message_attachments m "
                "LEFT JOIN attachments a ON a.id=m.attachment_id "
                "WHERE a.id IS NULL LIMIT 1"
            ).first()
            dangling_attempt = connection.exec_driver_sql(
                "SELECT 1 FROM attempt_attachments r "
                "LEFT JOIN attachments a ON a.id=r.attachment_id "
                "WHERE a.id IS NULL LIMIT 1"
            ).first()
        if dangling is not None or dangling_attempt is not None:
            raise AttachmentIntegrityError(
                "historical attachment reference has no reusable identity"
            )
        expected_operations = {
            str(row.operation_id) for row in rows if row.state == "staging"
        }
        for name in self._attachment_manager.inventory("captures"):
            if name not in expected_operations:
                self._attachment_manager.discard_capture(name)
        for row in rows:
            digest = bytes(row.digest)
            size = int(row.byte_size)
            if row.state == "ready":
                self._attachment_manager.read_verified(digest, expected_size=size)
                continue
            if row.state == "staging":
                operation_id = str(row.operation_id)
                capture = operation_id in self._attachment_manager.inventory("captures")
                stage = operation_id in self._attachment_manager.inventory("staging")
                canonical = digest_to_text(digest) in self._attachment_manager.inventory("objects")
                if not (capture or stage or canonical):
                    raise AttachmentIntegrityError(
                        "durable staging row has no attributable payload"
                    )
                if capture and canonical:
                    raise AttachmentIntegrityError("staging attachment state is ambiguous")
                if capture:
                    self._attachment_manager.verify_capture(operation_id, digest, size)
                    self._attachment_manager.capture_to_stage(
                        operation_id, digest, size
                    )
                    stage = True
                if stage:
                    self._attachment_manager.stage_to_object(
                        operation_id, digest, size
                    )
                self._attachment_manager.prove_staging_publication(
                    operation_id, digest, size
                )
                connection = None
                commit_attempted = False
                try:
                    connection = self._engine.connect()
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    arm_phase6_blob_transition(
                        connection, digest, "staging", "ready", byte_size=size
                    )
                    try:
                        result = connection.execute(
                            update(attachment_blobs)
                            .where(
                                attachment_blobs.c.digest == digest,
                                attachment_blobs.c.operation_id == operation_id,
                            )
                            .values(
                                state="ready", operation_id=None, stage_name=None, gc_id=None
                            )
                        )
                        require_phase6_consumed(connection)
                        if result.rowcount != 1:
                            raise AttachmentIntegrityError(
                                "staging recovery did not affect one row"
                            )
                    finally:
                        clear_phase6(connection)
                    commit_attempted = True
                    self._commit_attachment_transaction(
                        connection, "T8 startup staging recovery"
                    )
                except BaseException:
                    if not commit_attempted and not self._poisoned and connection is not None:
                        self._rollback_attachment_transaction(
                            connection, "T8 startup staging recovery K0"
                        )
                    raise
                finally:
                    if connection is not None:
                        connection.close()
                continue
            if row.state == "deleting":
                self._recover_deleting_blob(digest, size, str(row.gc_id))
        with self._engine.connect() as connection:
            ready = {
                digest_to_text(bytes(row.digest))
                for row in connection.execute(
                    select(attachment_blobs.c.digest).where(
                        attachment_blobs.c.state == "ready"
                    )
                )
            }
        # Seal visible absence left by any rowless cleanup and every prior
        # cross-directory edge before the authority can publish READY.
        self._attachment_manager.sync_all_namespaces()
        if set(self._attachment_manager.inventory("objects")) != ready:
            raise AttachmentIntegrityError("attachment object inventory is not authoritative")
        if self._attachment_manager.inventory("staging"):
            raise AttachmentIntegrityError("attachment staging inventory is not empty")
        if self._attachment_manager.inventory("captures"):
            raise AttachmentIntegrityError("attachment capture inventory is not empty")
        if self._attachment_manager.inventory("gc"):
            raise AttachmentIntegrityError("attachment GC inventory is not empty")

    def _recover_deleting_blob(self, digest: bytes, size: int, gc_id: str) -> None:
        temporary = self._attachment_manager.tombstone_temp(gc_id)
        if temporary in self._attachment_manager.inventory("gc"):
            self._attachment_manager._remove_gc_temp(gc_id, digest)
        object_state, gc_state = self._attachment_manager.gc_content_state(
            digest, size, gc_id
        )
        if (object_state, gc_state) == ("payload", "absent"):
            self._attachment_manager.create_tombstone(gc_id, digest)
            object_state, gc_state = self._attachment_manager.gc_content_state(
                digest, size, gc_id
            )
        if (object_state, gc_state) == ("payload", "tombstone"):
            self._attachment_manager.exchange_object_to_gc(digest, gc_id)
            object_state, gc_state = self._attachment_manager.gc_content_state(
                digest, size, gc_id
            )
        if (object_state, gc_state) == ("tombstone", "payload"):
            self._attachment_manager.sync_namespace("gc")
            self._attachment_manager.sync_namespace("objects")
            self._attachment_manager.verify_gc_payload(gc_id, digest, size)
            _fault("after-gc-payload-verification")
            self._attachment_manager.delete_gc_payload(gc_id)
            self._attachment_manager.delete_canonical_tombstone(digest)
        elif (object_state, gc_state) == ("payload", "payload"):
            self._attachment_manager.delete_canonical_payload(digest)
            self._attachment_manager.delete_gc_payload(gc_id)
        elif (object_state, gc_state) == ("tombstone", "tombstone"):
            self._attachment_manager.sync_namespace("gc")
            self._attachment_manager.sync_namespace("objects")
            self._attachment_manager.delete_gc_tombstone(gc_id)
            self._attachment_manager.delete_canonical_tombstone(digest)
        elif (object_state, gc_state) == ("tombstone", "absent"):
            self._attachment_manager.delete_canonical_tombstone(digest)
        elif (object_state, gc_state) == ("absent", "payload"):
            self._attachment_manager.delete_gc_payload(gc_id)
        elif (object_state, gc_state) != ("absent", "absent"):
            raise AttachmentIntegrityError(
                "GC names do not match an authorized recovery state"
            )
        self._attachment_manager.sync_namespace("gc")
        self._attachment_manager.sync_namespace("objects")
        object_state, gc_state = self._attachment_manager.gc_content_state(
            digest, size, gc_id
        )
        if (object_state, gc_state) != ("absent", "absent"):
            raise AttachmentIntegrityError("GC D4 absence is not authoritative")
        connection = None
        commit_attempted = False
        try:
            connection = self._engine.connect()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            current = connection.execute(
                select(attachment_blobs.c.state, attachment_blobs.c.gc_id).where(
                    attachment_blobs.c.digest == digest
                )
            ).first()
            if current is None or current.state != "deleting" or current.gc_id != gc_id:
                connection.rollback()
                raise AttachmentIntegrityError("GC durable identity changed")
            if connection.execute(
                select(attachments.c.id).where(attachments.c.blob_digest == digest)
            ).first() is not None:
                connection.rollback()
                raise AttachmentIntegrityError("deleting blob gained an attachment")
            arm_phase6_blob_delete(connection, digest, gc_id)
            try:
                _fault("before-gc-row-delete")
                result = connection.execute(
                    delete(attachment_blobs).where(attachment_blobs.c.digest == digest)
                )
                require_phase6_consumed(connection)
                if result.rowcount != 1:
                    raise AttachmentIntegrityError(
                        "GC recovery did not affect one row"
                    )
            finally:
                clear_phase6(connection)
            _fault("before-gc-row-delete-commit")
            commit_attempted = True
            self._commit_attachment_transaction(
                connection, "T9 GC final intent deletion"
            )
            _fault("after-gc-row-delete-commit")
        except BaseException:
            if not commit_attempted and not self._poisoned and connection is not None:
                self._rollback_attachment_transaction(
                    connection, "T9 GC final intent deletion K0"
                )
                self._poison_attachment_lifecycle(
                    "T9 GC final intent deletion known abort"
                )
            raise
        finally:
            if connection is not None:
                connection.close()

    def _validate_attachment_payload_rows(self) -> None:
        """Verify representation identity/eligibility against the owned bytes."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    attachments.c.blob_digest,
                    attachments.c.text_representation_id,
                    attachments.c.text_digest,
                    attachments.c.ineligibility_reason,
                    attachment_blobs.c.byte_size,
                ).select_from(
                    attachments.join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
            ).fetchall()
        for row in rows:
            try:
                raw = self._attachment_manager.read_verified(
                    row.blob_digest,
                    expected_size=int(row.byte_size),
                )
            except AttachmentIntegrityError as exc:
                raise StateError("durable attachment payload integrity failed") from exc
            classification = classify_attachment_text(raw)
            expected_representation = classification.representation_id
            persisted = (
                row.text_representation_id,
                row.text_digest,
                row.ineligibility_reason,
            )
            expected = (
                expected_representation,
                expected_representation,
                classification.ineligibility_reason,
            )
            if persisted != expected:
                if classification.ineligibility_reason == "invalid_utf8":
                    raise StateError(
                        "durable attachment UTF-8 representation is malformed"
                    )
                if classification.ineligibility_reason == "contains_nul":
                    raise StateError(
                        "durable attachment NUL representation is malformed"
                    )
                raise StateError("durable attachment representation is malformed")

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
        _validate_phase6_attempt_authority(connection, attempt)
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

    def _attachment_from_authoritative_row(self, row) -> Attachment:
        attachment_id = str(row._mapping["id"])
        try:
            raw = self._attachment_manager.read_verified(
                row.blob_digest,
                expected_size=int(row.byte_size),
            )
        except AttachmentIntegrityError as exc:
            raise StateError("attachment payload integrity failed") from exc
        self._validated_authoritative_attachment_representation(
            row,
            attachment_id,
            raw=raw,
        )
        return _attachment(row)

    def _validated_authoritative_attachment_representation(
        self,
        attachment_row,
        attachment_id: str,
        *,
        raw: bytes,
    ) -> str | None:
        """Classify persisted text evidence before any application consumes it."""
        representation_id = attachment_row.text_representation_id
        representation_digest = attachment_row.text_digest
        ineligibility_reason = attachment_row.ineligibility_reason
        classification = classify_attachment_text(raw)

        def corruption(message: str, cause: BaseException | None = None) -> None:
            # Keep ``_poisoned`` false so the existing transaction can perform
            # its known rollback.  The authority request is monotonic and
            # revokes this logical grant before that cleanup begins.
            self._authority.poison(
                "required attachment text representation integrity failure"
            )
            error = StateError(message)
            if cause is not None:
                raise error from cause
            raise error

        if representation_id is None and representation_digest is not None:
            corruption("selected attachment text representation metadata is inconsistent")
        if representation_id is not None and representation_digest is None:
            corruption("attachment text representation is malformed")
        if (
            representation_id is not None
            and representation_digest is not None
            and representation_id != representation_digest
        ):
            corruption("selected attachment text representation identity disagrees with its digest")

        if representation_id is None:
            if (
                classification.representation_id is not None
                or ineligibility_reason != classification.ineligibility_reason
            ):
                corruption(
                    "selected attachment ineligibility metadata disagrees with its payload"
                )
            return None

        if ineligibility_reason is not None:
            corruption(
                "selected attachment text representation conflicts with ineligibility metadata"
            )
        if classification.ineligibility_reason == "invalid_utf8":
            corruption(
                f"selected attachment is not valid UTF-8: {attachment_id}",
                classification.decode_error,
            )
        if classification.ineligibility_reason == "contains_nul":
            corruption(
                f"selected attachment text representation contains NUL: {attachment_id}"
            )
        if classification.representation_id != representation_digest:
            corruption("selected attachment text representation digest mismatches its payload")
        return classification.text

    def _persist_phase6_evidence(
        self,
        connection,
        *,
        attempt_id: str,
        message_id: str,
        context_plan,
        attachment_ids: tuple[str, ...],
        reuse_message_attachments: bool = False,
    ) -> None:
        if context_plan is None and not attachment_ids:
            return
        if context_plan is None:
            raise StateError("Phase 6 attachment selection requires a frozen context plan")
        try:
            wire_digest = hashlib.sha256(context_plan.wire_representation).hexdigest()
            connection.execute(
                insert(context_plans).values(
                    attempt_id=attempt_id,
                    plan_version=context_plan.version,
                    canonical_representation=context_plan.canonical_representation,
                    canonical_digest=context_plan.canonical_digest,
                    wire_representation_digest=wire_digest,
                    budget_limit=context_plan.budget.limit,
                    budget_provenance=context_plan.budget.provenance,
                    budget_semantics=context_plan.budget.semantics,
                    adapter_id=context_plan.budget.adapter_id,
                    adapter_version=context_plan.budget.adapter_version,
                    input_counts=json.dumps(context_plan.input_counts, sort_keys=True, separators=(",", ":")),
                    envelope_overhead=context_plan.budget.envelope_overhead,
                    output_reserve=context_plan.budget.output_reserve,
                    input_units=context_plan.budget.input_units,
                    total_units=context_plan.budget.total_units,
                    headroom=context_plan.budget.headroom,
                    created_at=utc_iso(datetime.now(UTC)),
                )
            )
            source_by_id = {source.source_id: source for source in context_plan.sources}
            for ordinal, attachment_id in enumerate(attachment_ids):
                source = source_by_id.get(attachment_id)
                if source is None or source.kind != "attachment" or not source.selected:
                    raise StateError("selected attachment is not part of the frozen context plan")
                attachment_row = connection.execute(
                    select(
                        attachments.c.blob_digest,
                        attachments.c.text_representation_id,
                        attachments.c.text_digest,
                        attachments.c.ineligibility_reason,
                        attachment_blobs.c.byte_size,
                    )
                    .select_from(
                        attachments.join(
                            attachment_blobs,
                            attachments.c.blob_digest == attachment_blobs.c.digest,
                        )
                    )
                    .where(attachments.c.id == attachment_id)
                ).first()
                if attachment_row is None:
                    raise StateError(f"selected attachment is missing: {attachment_id}")
                try:
                    raw = self._attachment_manager.read_verified(
                        attachment_row.blob_digest,
                        expected_size=attachment_row.byte_size,
                    )
                except AttachmentIntegrityError as exc:
                    raise StateError("selected attachment payload integrity failed") from exc
                text_content = self._validated_authoritative_attachment_representation(
                    attachment_row,
                    attachment_id,
                    raw=raw,
                )
                if text_content is None:
                    raise StateError(
                        f"selected attachment is not context-eligible: {attachment_id}"
                    )
                if (
                    source.representation_id
                    != digest_to_text(attachment_row.text_representation_id)
                    or source.content != text_content
                ):
                    raise StateError("selected attachment changed while context was being prepared")
                if not reuse_message_attachments:
                    connection.execute(
                        insert(message_attachments).values(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            ordinal=ordinal,
                        )
                    )
                else:
                    existing_message_refs = connection.execute(
                        select(message_attachments.c.attachment_id, message_attachments.c.ordinal)
                        .where(message_attachments.c.message_id == message_id)
                        .order_by(message_attachments.c.ordinal)
                    ).fetchall()
                    expected_message_refs = tuple(
                        (item.attachment_id, int(item.ordinal))
                        for item in existing_message_refs
                    )
                    if expected_message_refs != tuple(
                        (value, index) for index, value in enumerate(attachment_ids)
                    ):
                        raise StateError("regenerated attachment references do not match the frozen context plan")
                connection.execute(
                    insert(attempt_attachments).values(
                        attempt_id=attempt_id,
                        attachment_id=attachment_id,
                        ordinal=ordinal,
                    )
                )
        except (AttributeError, TypeError, ValueError) as exc:
            raise StateError("frozen Phase 6 context evidence is malformed") from exc

    def persist_generation_start(
        self,
        chat: Chat,
        user_message: Message,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
        context_plan=None,
        attachment_ids: tuple[str, ...] = (),
    ) -> None:
        self._ensure_open()
        # Acquire the attachment transition gate before opening the SQLite
        # write transaction.  This ordering prevents GC (gate -> BEGIN
        # IMMEDIATE) from deadlocking generation (BEGIN -> gate).
        source_revision: int | None = None
        with self._authority.transition():
            with ExitStack() as stack:
                if attachment_ids:
                    connection = self._engine.connect()
                    stack.callback(
                        self._close_attachment_connection,
                        connection,
                        "T5 generation start with attachments",
                    )
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    explicit = True
                else:
                    connection = stack.enter_context(
                        self._search_transaction("T5 generation start")
                    )
                    explicit = False
                commit_succeeded = False
                if explicit:
                    def rollback_k0() -> None:
                        if not commit_succeeded and not self._poisoned:
                            self._rollback_attachment_transaction(
                                connection, "T5 generation start with attachments K0"
                            )
                    stack.callback(rollback_k0)
                self._arm_phase7_source_mutation(connection, "generation start")
                try:
                    self._insert_messages_and_attempt(
                        connection,
                        (user_message, assistant_message),
                        attempt,
                    )
                    self._persist_phase6_evidence(
                        connection,
                        attempt_id=attempt.id,
                        message_id=user_message.id,
                        context_plan=context_plan,
                        attachment_ids=attachment_ids,
                    )
                    self._advance_chat(
                        connection,
                        chat,
                        assistant_message.id,
                        expected_chat_revision,
                    )
                    source_revision = self._require_phase7_source_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
                if explicit:
                    commit_attempted = True
                    self._commit_attachment_transaction(
                        connection, "T5 generation start with attachments"
                    )
                    commit_succeeded = True
            assert source_revision is not None
            self._accept_search_receipt(
                SearchReceipt(
                    source_revision,
                    frozenset(
                        {
                            f"chat:{chat.id}",
                            f"message:{user_message.id}",
                            f"message:{assistant_message.id}",
                            *(f"attachment:{value}" for value in attachment_ids),
                        }
                    ),
                )
            )

    def persist_regeneration_start(
        self,
        chat: Chat,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
        context_plan=None,
        attachment_ids: tuple[str, ...] = (),
    ) -> None:
        self._ensure_open()
        source_revision: int | None = None
        with self._authority.transition():
            with ExitStack() as stack:
                if attachment_ids:
                    connection = self._engine.connect()
                    stack.callback(
                        self._close_attachment_connection,
                        connection,
                        "T6 regeneration with attachments",
                    )
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    explicit = True
                else:
                    connection = stack.enter_context(
                        self._search_transaction("T6 regeneration start")
                    )
                    explicit = False
                commit_succeeded = False
                if explicit:
                    def rollback_k0() -> None:
                        if not commit_succeeded and not self._poisoned:
                            self._rollback_attachment_transaction(
                                connection, "T6 regeneration with attachments K0"
                            )
                    stack.callback(rollback_k0)
                self._arm_phase7_source_mutation(connection, "regeneration start")
                try:
                    self._insert_messages_and_attempt(
                        connection, (assistant_message,), attempt
                    )
                    self._persist_phase6_evidence(
                        connection,
                        attempt_id=attempt.id,
                        message_id=attempt.user_message_id,
                        context_plan=context_plan,
                        attachment_ids=attachment_ids,
                        reuse_message_attachments=True,
                    )
                    self._advance_chat(
                        connection,
                        chat,
                        assistant_message.id,
                        expected_chat_revision,
                    )
                    source_revision = self._require_phase7_source_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
                if explicit:
                    commit_attempted = True
                    self._commit_attachment_transaction(
                        connection, "T6 regeneration with attachments"
                    )
                    commit_succeeded = True
            assert source_revision is not None
            self._accept_search_receipt(
                SearchReceipt(
                    source_revision,
                    frozenset(
                        {
                            f"chat:{chat.id}",
                            f"message:{assistant_message.id}",
                            *(f"attachment:{value}" for value in attachment_ids),
                        }
                    ),
                )
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
        with self._search_source_transaction(
            "finalize generation",
            (f"chat:{message.chat_id}", f"message:{message.id}"),
        ) as connection:
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
                except IntegrityError as exc:
                    if "finish_reason" in str(exc):
                        raise StateError("generation finish_reason is inconsistent with state") from None
                    raise
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

    # ------------------------------------------------------------------
    # Phase 7 derived search and exact navigation

    @staticmethod
    def _is_search_counter(value) -> bool:
        return (
            type(value) is int
            and 0 <= value <= _SQLITE_MAX_INTEGER
        )

    def _authoritative_search_state_corrupt(self, detail: str) -> None:
        """Use the existing Phase 6 invalidation path for known source corruption."""
        self._authority.poison(detail)
        raise StateError(f"{detail}; restart recovery is required")

    def _require_search_source_revision(self, connection) -> int:
        # A writer holds this lock from commit through high-water publication.
        # A deferred read transaction therefore observes either the complete
        # old state or the complete new state, never a mismatched pair.
        with self._search_source_revision_lock:
            rows = connection.exec_driver_sql(
                "SELECT singleton_id, source_revision FROM search_source_state LIMIT 2"
            ).fetchall()
            if (
                len(rows) != 1
                or type(rows[0][0]) is not int
                or rows[0][0] != 1
                or not self._is_search_counter(rows[0][1])
            ):
                self._authoritative_search_state_corrupt(
                    "authoritative search source state is malformed"
                )
            revision = rows[0][1]
            if revision != self._search_source_revision_high_water:
                self._authoritative_search_state_corrupt(
                    "authoritative search source revision regressed or advanced outside its transition"
                )
            return revision

    def _record_committed_search_source_revision(self, revision: int) -> None:
        """Publish one known committed authoritative revision while serialized."""
        with self._search_source_revision_lock:
            if (
                not self._is_search_counter(revision)
                or revision != self._search_source_revision_high_water + 1
            ):
                self._authoritative_search_state_corrupt(
                    "authoritative search source revision transition is not monotonic"
                )
            self._search_source_revision_high_water = revision

    def _arm_phase7_source_mutation(self, connection, operation: str) -> None:
        """Fail closed before SQLite arithmetic can launder corrupt source state."""
        revision = self._require_search_source_revision(connection)
        if revision >= _SQLITE_MAX_INTEGER:
            self._authoritative_search_state_corrupt(
                "authoritative search source revision is exhausted"
            )
        arm_phase7_source_mutation(
            connection,
            operation,
            expected_revision=revision,
        )

    def _require_phase7_source_consumed(self, connection) -> int:
        """Classify the authoritative post-trigger counter before commit."""
        try:
            revision = require_phase7_consumed(connection)
        except (RuntimeError, TypeError, ValueError, OverflowError) as exc:
            self._authority.poison(
                "authoritative search source mutation result is malformed"
            )
            raise StateError(
                "authoritative search source mutation result is malformed; "
                "restart recovery is required"
            ) from exc
        if not self._is_search_counter(revision):
            self._authoritative_search_state_corrupt(
                "authoritative search source mutation result is malformed"
            )
        if connection.info.get(_PHASE7_PENDING_SOURCE_COMMIT) is not None:
            self._authoritative_search_state_corrupt(
                "authoritative search source mutation result was registered twice"
            )
        connection.info[_PHASE7_PENDING_SOURCE_COMMIT] = revision
        return revision

    def _search_status_from_connection(self, connection) -> SearchStatus:
        source_revision = self._require_search_source_revision(connection)
        rows = connection.exec_driver_sql(
            "SELECT singleton_id, condition, checkpoint_revision, generation, "
            "schema_version, tokenizer_version, detail "
            "FROM search_index_state LIMIT 2"
        ).fetchall()
        if not rows:
            if not self._search_available:
                return SearchStatus(
                    SearchIndexCondition.UNAVAILABLE,
                    source_revision,
                    None,
                    None,
                    SEARCH_SCHEMA_VERSION,
                    SEARCH_TOKENIZER_VERSION,
                    "SQLite FTS5 is unavailable and derived search index state is missing",
                )
            return SearchStatus(
                SearchIndexCondition.INVALID,
                source_revision,
                None,
                None,
                SEARCH_SCHEMA_VERSION,
                SEARCH_TOKENIZER_VERSION,
                "derived search index state is missing",
            )
        row = rows[0]
        singleton_valid = (
            len(rows) == 1
            and type(row[0]) is int
            and row[0] == 1
        )
        checkpoint_valid = singleton_valid and self._is_search_counter(row[2])
        generation_valid = singleton_valid and self._is_search_counter(row[3])
        schema_valid = singleton_valid and type(row[4]) is int
        tokenizer_valid = singleton_valid and type(row[5]) is str
        condition_valid = (
            singleton_valid
            and type(row[1]) is str
            and row[1] in {"VALID", "REBUILDING", "INVALID"}
        )
        detail_valid = singleton_valid and (row[6] is None or type(row[6]) is str)
        checkpoint_revision = row[2] if checkpoint_valid else None
        generation = row[3] if generation_valid else None
        stored_schema_version = row[4] if schema_valid else None
        stored_tokenizer_version = row[5] if tokenizer_valid else None
        if generation_valid:
            generation_regressed = generation < self._search_generation_high_water
            self._search_generation_high_water = max(
                self._search_generation_high_water, generation
            )
        else:
            generation_regressed = False
        malformed = (
            not condition_valid
            or not checkpoint_valid
            or not generation_valid
            or not schema_valid
            or not tokenizer_valid
            or not detail_valid
            or checkpoint_revision > source_revision
        )
        if malformed:
            return SearchStatus(
                SearchIndexCondition.INVALID,
                source_revision,
                checkpoint_revision,
                generation,
                stored_schema_version,
                stored_tokenizer_version,
                "derived search index coordination state is malformed",
            )
        assert checkpoint_revision is not None
        assert generation is not None
        assert stored_schema_version is not None
        assert stored_tokenizer_version is not None
        version_mismatch = (
            stored_schema_version != SEARCH_SCHEMA_VERSION
            or stored_tokenizer_version != SEARCH_TOKENIZER_VERSION
        )
        detail = row[6]
        if not self._search_available:
            condition = SearchIndexCondition.UNAVAILABLE
            detail = detail or "SQLite FTS5 is unavailable in this runtime"
        elif version_mismatch:
            condition = SearchIndexCondition.INVALID
            detail = detail or "derived search schema or tokenizer version is invalid"
        elif row[1] == "REBUILDING":
            condition = SearchIndexCondition.REBUILDING
        elif (
            row[1] == "INVALID"
            or checkpoint_revision > source_revision
            or generation_regressed
        ):
            condition = SearchIndexCondition.INVALID
            if generation_regressed:
                detail = detail or "derived search generation regressed"
        elif checkpoint_revision != source_revision:
            condition = SearchIndexCondition.STALE
            detail = detail or "authoritative source revision is newer than the search checkpoint"
        else:
            condition = SearchIndexCondition.VALID
        return SearchStatus(
            condition=condition,
            source_revision=source_revision,
            checkpoint_revision=checkpoint_revision,
            generation=generation,
            schema_version=stored_schema_version,
            tokenizer_version=stored_tokenizer_version,
            detail=None if detail is None else str(detail),
        )

    def search_status(self) -> SearchStatus:
        self._ensure_open()
        with self._search_transaction("search status") as connection:
            return self._search_status_from_connection(connection)

    @staticmethod
    def _require_searchable(status: SearchStatus) -> None:
        if status.condition is SearchIndexCondition.VALID:
            return
        if status.condition is SearchIndexCondition.UNAVAILABLE:
            raise SearchUnavailable(status.detail or "search is unavailable")
        if status.condition is SearchIndexCondition.REBUILDING:
            raise SearchRebuilding(status.detail or "search index is rebuilding")
        if status.condition is SearchIndexCondition.STALE:
            raise SearchStaleIndex(status.detail or "search index is stale")
        raise SearchIndexInvalid(status.detail or "search index is invalid")

    def _attachment_projection(self, connection, attachment_id: str) -> SearchProjection | None:
        row = connection.execute(
            select(attachments, attachment_blobs.c.byte_size)
            .select_from(
                attachments.join(
                    attachment_blobs,
                    attachments.c.blob_digest == attachment_blobs.c.digest,
                )
            )
            .where(attachments.c.id == attachment_id)
        ).first()
        if row is None:
            return None
        try:
            raw = self._attachment_manager.read_verified(
                row.blob_digest,
                expected_size=int(row.byte_size),
            )
        except AttachmentIntegrityError as exc:
            self._poison_attachment_lifecycle(
                "search attachment payload integrity mismatch"
            )
            raise StateError("attachment payload integrity failed") from exc
        body = self._validated_authoritative_attachment_representation(
            row,
            attachment_id,
            raw=raw,
        )
        names = str(row.filename)
        if row.source_name != row.filename:
            names += "\n" + str(row.source_name)
        return SearchProjection(
            "attachment",
            attachment_id,
            str(row.filename),
            body or "",
            names,
        )

    def _projection_for_key(self, connection, document_key: str) -> SearchProjection | None:
        try:
            kind, document_id = document_key.split(":", 1)
        except ValueError:
            raise SearchIndexInvalid("derived receipt contains a malformed document key") from None
        if not document_id:
            raise SearchIndexInvalid("derived receipt contains an empty document identity")
        if kind == "chat":
            row = connection.execute(
                select(chats.c.id, chats.c.title).where(chats.c.id == document_id)
            ).first()
            return (
                None
                if row is None
                else SearchProjection("chat", document_id, str(row.title), "", "")
            )
        if kind == "message":
            row = connection.execute(
                select(
                    messages.c.id,
                    messages.c.role,
                    messages.c.state,
                    messages.c.content,
                ).where(messages.c.id == document_id)
            ).first()
            if row is None or not (
                row.role == MessageRole.USER.value
                or (
                    row.role == MessageRole.ASSISTANT.value
                    and row.state
                    in {state.value for state in _MESSAGE_TERMINAL_STATES}
                )
            ):
                return None
            return SearchProjection("message", document_id, "", str(row.content), "")
        if kind == "attachment":
            return self._attachment_projection(connection, document_id)
        raise SearchIndexInvalid("derived receipt contains an unknown document kind")

    def _all_search_projections(self, connection) -> tuple[SearchProjection, ...]:
        result: list[SearchProjection] = []
        for row in connection.execute(
            select(chats.c.id, chats.c.title).order_by(chats.c.id)
        ).fetchall():
            result.append(SearchProjection("chat", str(row.id), str(row.title), "", ""))
        for row in connection.execute(
            select(messages.c.id, messages.c.content)
            .where(
                (messages.c.role == MessageRole.USER.value)
                | (
                    (messages.c.role == MessageRole.ASSISTANT.value)
                    & (
                        messages.c.state.in_(
                            tuple(state.value for state in _MESSAGE_TERMINAL_STATES)
                        )
                    )
                )
            )
            .order_by(messages.c.id)
        ).fetchall():
            result.append(
                SearchProjection("message", str(row.id), "", str(row.content), "")
            )
        attachment_ids = connection.execute(
            select(attachments.c.id).order_by(attachments.c.id)
        ).scalars().all()
        for attachment_id in attachment_ids:
            projection = self._attachment_projection(connection, str(attachment_id))
            if projection is None:
                raise SearchIndexInvalid("attachment disappeared during serialized rebuild")
            result.append(projection)
        return tuple(result)

    @staticmethod
    def _validate_search_projection_contents(
        connection,
        projections: tuple[SearchProjection, ...],
    ) -> None:
        """Compare every derived field at explicit expensive boundaries only."""
        expected = {
            projection.document_key: (
                projection.document_kind,
                projection.document_id,
                projection.title,
                projection.body,
                projection.filename,
            )
            for projection in projections
        }
        rows = connection.exec_driver_sql(
            "SELECT f.document_key, k.document_kind, k.document_id, "
            "f.title, f.body, f.filename FROM search_document_keys k "
            "JOIN search_fts f ON f.rowid=k.fts_rowid"
        ).fetchall()
        actual: dict[str, tuple[str, ...]] = {}
        for row in rows:
            values = tuple(row)
            if len(values) != 6 or any(not isinstance(value, str) for value in values):
                raise SearchIndexInvalid("derived search document content is malformed")
            key = values[0]
            if key in actual:
                raise SearchIndexInvalid("derived search document key is duplicated")
            actual[key] = values[1:]
        if actual != expected:
            raise SearchIndexInvalid(
                "derived search document content does not match authoritative truth"
            )

    def _rollback_search_transaction(self, connection, operation: str) -> None:
        try:
            connection.rollback()
        except BaseException as exc:
            self._poisoned = True
            self._authority.poison(f"search transaction rollback failed: {operation}")
            raise StateError(
                "search transaction rollback is uncertain; restart recovery is required"
            ) from exc
        finally:
            connection_info = getattr(connection, "info", None)
            if connection_info is not None:
                connection_info.pop(_PHASE7_PENDING_SOURCE_COMMIT, None)

    def _commit_search_transaction(self, connection, operation: str) -> None:
        connection_info = getattr(connection, "info", None)
        pending_revision = (
            None
            if connection_info is None
            else connection_info.get(_PHASE7_PENDING_SOURCE_COMMIT)
        )
        lock = (
            self._search_source_revision_lock
            if pending_revision is not None
            else nullcontext()
        )
        with lock:
            try:
                connection.commit()
            except BaseException as exc:
                if connection_info is not None:
                    connection_info.pop(_PHASE7_PENDING_SOURCE_COMMIT, None)
                self._poisoned = True
                self._authority.poison(f"uncertain search transaction commit: {operation}")
                try:
                    connection.rollback()
                except BaseException:
                    pass
                raise StateError(
                    "search transaction outcome is uncertain; restart recovery is required"
                ) from exc
            if pending_revision is not None:
                assert connection_info is not None
                connection_info.pop(_PHASE7_PENDING_SOURCE_COMMIT, None)
                self._record_committed_search_source_revision(pending_revision)

    def _close_search_connection(self, connection, operation: str) -> None:
        try:
            connection.close()
        except BaseException as exc:
            self._poisoned = True
            self._authority.poison(f"search connection close failed: {operation}")
            raise StateError(
                "search connection close is uncertain; restart recovery is required"
            ) from exc

    @contextmanager
    def _search_transaction(self, operation: str):
        """Classify settlement and lifetime outcomes for one Phase 7 transaction."""
        connection = None
        try:
            connection = self._engine.connect()
            connection.exec_driver_sql("BEGIN")
            try:
                yield connection
            except BaseException:
                self._rollback_search_transaction(connection, operation)
                raise
            else:
                self._commit_search_transaction(connection, operation)
        finally:
            if connection is not None:
                self._close_search_connection(connection, operation)

    def _accept_search_receipt(self, receipt: SearchReceipt) -> None:
        try:
            self._search_receipts.accept(receipt)
            if not self._search_available:
                return
            self._drain_search_receipts()
        except Exception:
            # The authoritative transaction is already known committed.  A
            # derived rollback or uncertain derived settlement leaves source >
            # checkpoint visible. Unknown outcomes have already poisoned the
            # authority, but must not falsely report business mutation failure.
            return

    def _write_search_invalid_state(
        self,
        detail: str,
        *,
        expected_generation: int,
        expected_checkpoint: int,
    ) -> bool:
        """Persist INVALID for the exact derived snapshot already observed bad.

        The caller owns writer serialization. A concurrent completed rebuild is
        not overwritten because generation and checkpoint are part of the CAS.
        """
        connection = None
        commit_attempted = False
        try:
            connection = self._engine.connect()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            updated = connection.exec_driver_sql(
                "UPDATE search_index_state SET condition='INVALID', detail=?, "
                "updated_at=? WHERE singleton_id=1 AND generation=? "
                "AND checkpoint_revision=?",
                (
                    detail[:1024],
                    utc_iso(datetime.now(UTC)),
                    expected_generation,
                    expected_checkpoint,
                ),
            )
            if updated.rowcount not in {0, 1}:
                raise SearchIndexInvalid("search index singleton is not unique")
            commit_attempted = True
            self._commit_search_transaction(connection, "mark invalid")
            return updated.rowcount == 1
        except SearchIndexInvalid:
            if connection is not None and not commit_attempted:
                self._rollback_search_transaction(connection, "mark invalid")
            raise
        except (DBAPIError, sqlite3.DatabaseError, IntegrityError) as exc:
            if connection is not None and not commit_attempted:
                self._rollback_search_transaction(connection, "mark invalid")
            raise SearchIndexInvalid("failed to persist invalid search state") from exc
        finally:
            if connection is not None:
                self._close_search_connection(connection, "mark invalid")

    def _mark_search_invalid(
        self,
        detail: str,
        *,
        expected_generation: int,
        expected_checkpoint: int,
    ) -> bool:
        with self._authority.transition():
            return self._write_search_invalid_state(
                detail,
                expected_generation=expected_generation,
                expected_checkpoint=expected_checkpoint,
            )

    def _drain_search_receipts(self) -> None:
        with self._authority.transition():
            connection = None
            commit_attempted = False
            try:
                connection = self._engine.connect()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                status = self._search_status_from_connection(connection)
                if status.condition in {
                    SearchIndexCondition.REBUILDING,
                    SearchIndexCondition.INVALID,
                    SearchIndexCondition.UNAVAILABLE,
                }:
                    self._rollback_search_transaction(connection, "receipt status gate")
                    return
                assert status.checkpoint_revision is not None
                contiguous = self._search_receipts.contiguous(status.checkpoint_revision)
                if contiguous is None:
                    self._rollback_search_transaction(connection, "receipt gap")
                    return
                target_revision, document_keys = contiguous
                assert status.source_revision is not None
                if target_revision > status.source_revision:
                    self._rollback_search_transaction(connection, "future receipt")
                    raise SearchIndexInvalid("derived receipt is newer than authoritative state")
                for document_key in sorted(document_keys):
                    replace_projection(
                        connection,
                        self._projection_for_key(connection, document_key),
                        document_key,
                    )
                updated = connection.exec_driver_sql(
                    "UPDATE search_index_state SET checkpoint_revision=?, condition='VALID', "
                    "detail=NULL, updated_at=? WHERE singleton_id=1 AND checkpoint_revision=?",
                    (
                        target_revision,
                        utc_iso(datetime.now(UTC)),
                        status.checkpoint_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise SearchIndexInvalid("derived checkpoint changed during receipt drain")
                commit_attempted = True
                self._commit_search_transaction(connection, "receipt drain")
                self._search_receipts.acknowledge(target_revision)
            except SearchIndexInvalid:
                if connection is not None and not commit_attempted:
                    self._rollback_search_transaction(connection, "receipt logical failure")
                raise
            except (DBAPIError, sqlite3.DatabaseError, IntegrityError) as exc:
                if connection is not None and not commit_attempted:
                    self._rollback_search_transaction(connection, "receipt derived failure")
                raise SearchIndexInvalid("incremental derived search update failed") from exc
            finally:
                if connection is not None:
                    self._close_search_connection(connection, "receipt drain")

    def search(
        self,
        query: str,
        *,
        filters: SearchFilters = SearchFilters(),
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        self._ensure_open()
        limit = validate_limit(limit)
        expression, fingerprint = compile_literal_query(query, filters)
        fingerprint = bind_cursor_fingerprint(
            fingerprint, self._search_cursor_epoch
        )
        status: SearchStatus | None = None
        try:
            with self._search_transaction("search query snapshot") as connection:
                status = self._search_status_from_connection(connection)
                self._require_searchable(status)
                assert status.checkpoint_revision is not None
                assert status.generation is not None
                offset = decode_cursor(
                    cursor,
                    fingerprint=fingerprint,
                    checkpoint_revision=status.checkpoint_revision,
                    generation=status.generation,
                )
                statement, parameters = build_search_statement(
                    expression,
                    filters,
                    limit=limit,
                    offset=offset,
                )
                rows = connection.exec_driver_sql(statement, parameters).fetchall()
                for row in rows:
                    self._validate_returned_search_projection(connection, row)
                page_rows = rows[:limit]
                results = tuple(
                    self._search_result_from_row(
                        connection,
                        row,
                        status=status,
                        filters=filters,
                    )
                    for row in page_rows
                )
                next_cursor = (
                    encode_cursor(
                        fingerprint,
                        status.checkpoint_revision,
                        status.generation,
                        offset + limit,
                    )
                    if len(rows) > limit
                    else None
                )
                return SearchPage(results, next_cursor, status)
        except SearchIndexInvalid as exc:
            if (
                status is not None
                and status.condition is SearchIndexCondition.VALID
                and status.generation is not None
                and status.checkpoint_revision is not None
            ):
                self._mark_search_invalid(
                    str(exc),
                    expected_generation=status.generation,
                    expected_checkpoint=status.checkpoint_revision,
                )
            raise
        except (DBAPIError, sqlite3.DatabaseError) as exc:
            if (
                status is not None
                and status.generation is not None
                and status.checkpoint_revision is not None
            ):
                try:
                    self._mark_search_invalid(
                        str(exc),
                        expected_generation=status.generation,
                        expected_checkpoint=status.checkpoint_revision,
                    )
                except SearchIndexInvalid:
                    # The original structural failure remains the typed cause;
                    # an exact newer snapshot may also have won the CAS.
                    pass
            raise SearchIndexInvalid("search index query failed") from exc

    def _message_is_active(self, connection, chat_id: str, message_id: str) -> bool:
        return bool(
            connection.exec_driver_sql(
                "WITH RECURSIVE active(id) AS ("
                " SELECT head_message_id FROM chats WHERE id=? AND head_message_id IS NOT NULL"
                " UNION SELECT m.parent_id FROM messages m JOIN active a ON m.id=a.id"
                " WHERE m.parent_id IS NOT NULL) SELECT 1 FROM active WHERE id=? LIMIT 1",
                (chat_id, message_id),
            ).first()
        )

    def _attachment_locations(
        self,
        connection,
        attachment_id: str,
        *,
        filters: SearchFilters,
    ) -> tuple[SearchLocation, ...]:
        predicates = ["ma.attachment_id=?"]
        parameters: list[object] = [attachment_id]
        if filters.chat_id is not None:
            predicates.append("m.chat_id=?")
            parameters.append(filters.chat_id)
        if not filters.include_archived:
            predicates.append("c.archived_at IS NULL")
        sql = (
            "SELECT m.chat_id, m.id, c.archived_at FROM message_attachments ma "
            "JOIN messages m ON m.id=ma.message_id JOIN chats c ON c.id=m.chat_id "
            "WHERE "
            + " AND ".join(predicates)
            + " "
            + "ORDER BY m.chat_id, m.sequence, m.id"
        )
        locations = []
        for chat_id, message_id, archived_at in connection.exec_driver_sql(
            sql, tuple(parameters)
        ).fetchall():
            active = self._message_is_active(connection, str(chat_id), str(message_id))
            if filters.active_branch_only and not active:
                continue
            locations.append(
                SearchLocation(
                    str(chat_id),
                    str(message_id),
                    SearchBranchState.ACTIVE if active else SearchBranchState.HISTORICAL,
                    None if archived_at is None else parse_utc(str(archived_at)),
                )
            )
        return tuple(locations)

    def _search_result_from_row(
        self,
        connection,
        row,
        *,
        status: SearchStatus,
        filters: SearchFilters,
    ) -> SearchResult:
        try:
            kind = SearchDocumentKind(str(row[0]))
            document_id = str(row[1])
            document_key = str(row[2])
            rank = float(row[3])
        except (TypeError, ValueError) as exc:
            raise SearchIndexInvalid("derived search result identity is malformed") from exc
        if document_key != f"{kind.value}:{document_id}":
            raise SearchIndexInvalid("derived search result key is inconsistent")
        if not math.isfinite(rank):
            raise SearchIndexInvalid("derived search result rank is malformed")
        if row[5] is None:
            raise SearchIndexInvalid(
                "derived search result has no authoritative timestamp"
            )
        snippet = "" if row[4] is None else str(row[4])
        authoritative_at = parse_utc(str(row[5]))
        assert status.checkpoint_revision is not None and status.generation is not None
        if kind is SearchDocumentKind.CHAT:
            chat_row = connection.execute(
                select(chats).where(chats.c.id == document_id)
            ).first()
            if chat_row is None:
                raise SearchIndexInvalid("indexed chat document has no authoritative row")
            archived = None if chat_row.archived_at is None else parse_utc(chat_row.archived_at)
            return SearchResult(
                document_key,
                kind,
                document_id,
                str(chat_row.title),
                snippet,
                rank,
                authoritative_at,
                chat_id=document_id,
                locations=(
                    SearchLocation(document_id, None, SearchBranchState.ACTIVE, archived),
                ),
                checkpoint_revision=status.checkpoint_revision,
                generation=status.generation,
                include_archived=filters.include_archived,
                active_branch_only=filters.active_branch_only,
            )
        if kind is SearchDocumentKind.MESSAGE:
            message_row = connection.execute(
                select(messages, chats.c.title, chats.c.archived_at)
                .select_from(messages.join(chats, messages.c.chat_id == chats.c.id))
                .where(messages.c.id == document_id)
            ).first()
            if message_row is None:
                raise SearchIndexInvalid("indexed message document has no authoritative row")
            active = self._message_is_active(
                connection, str(message_row.chat_id), document_id
            )
            branch = SearchBranchState.ACTIVE if active else SearchBranchState.HISTORICAL
            archived = (
                None
                if message_row.archived_at is None
                else parse_utc(str(message_row.archived_at))
            )
            return SearchResult(
                document_key,
                kind,
                document_id,
                str(message_row.title),
                snippet,
                rank,
                authoritative_at,
                chat_id=str(message_row.chat_id),
                message_id=document_id,
                lineage_id=str(message_row.lineage_id),
                revision=int(message_row.revision),
                supersedes_message_id=(
                    None if message_row.supersedes_id is None else str(message_row.supersedes_id)
                ),
                role=MessageRole(str(message_row.role)),
                state=MessageState(str(message_row.state)),
                locations=(
                    SearchLocation(str(message_row.chat_id), document_id, branch, archived),
                ),
                checkpoint_revision=status.checkpoint_revision,
                generation=status.generation,
                include_archived=filters.include_archived,
                active_branch_only=filters.active_branch_only,
            )
        attachment_row = connection.execute(
            select(attachments.c.filename).where(attachments.c.id == document_id)
        ).first()
        if attachment_row is None:
            raise SearchIndexInvalid("indexed attachment document has no authoritative row")
        locations = self._attachment_locations(
            connection,
            document_id,
            filters=filters,
        )
        if not locations:
            raise SearchIndexInvalid("visible attachment result has no authoritative location")
        return SearchResult(
            document_key,
            kind,
            document_id,
            str(attachment_row.filename),
            snippet,
            rank,
            authoritative_at,
            locations=locations,
            checkpoint_revision=status.checkpoint_revision,
            generation=status.generation,
            include_archived=filters.include_archived,
            active_branch_only=filters.active_branch_only,
        )

    def _validate_returned_search_projection(self, connection, row) -> None:
        """Validate only bounded FTS rows returned by the current query snapshot."""
        try:
            values = tuple(row)
            if len(values) != 9 or any(
                type(values[index]) is not str for index in (0, 1, 2, 6, 7, 8)
            ):
                raise TypeError
            document_key = f"{values[0]}:{values[1]}"
            if values[2] != document_key:
                raise SearchIndexInvalid("derived search result key is inconsistent")
            derived = SearchProjection(
                values[0], values[1], values[6], values[7], values[8]
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise SearchIndexInvalid(
                "returned search document projection is malformed"
            ) from exc
        authoritative = self._projection_for_key(connection, document_key)
        if authoritative is None or derived != authoritative:
            raise SearchIndexInvalid(
                "returned search document does not match authoritative truth"
            )

    def _write_rebuild_state(self) -> int:
        connection = None
        commit_attempted = False
        try:
            connection = self._engine.connect()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            rows = connection.exec_driver_sql(
                "SELECT singleton_id, generation FROM search_index_state LIMIT 2"
            ).fetchall()
            row_is_canonical = (
                len(rows) == 1
                and type(rows[0][0]) is int
                and rows[0][0] == 1
            )
            stored_generation = (
                rows[0][1]
                if row_is_canonical and self._is_search_counter(rows[0][1])
                else None
            )
            generation_base = max(
                self._search_generation_high_water,
                -1 if stored_generation is None else stored_generation,
            )
            if generation_base >= _SQLITE_MAX_INTEGER:
                self._search_cursor_epoch = str(uuid7())
                self._search_generation_high_water = 0
                generation = 1
            else:
                generation = generation_base + 1
            if not row_is_canonical:
                connection.exec_driver_sql("DELETE FROM search_index_state")
                connection.exec_driver_sql(
                    "INSERT INTO search_index_state"
                    "(singleton_id, condition, checkpoint_revision, generation, "
                    "schema_version, tokenizer_version, detail, updated_at) "
                    "VALUES (1, 'REBUILDING', 0, ?, ?, ?, NULL, ?)",
                    (
                        generation,
                        SEARCH_SCHEMA_VERSION,
                        SEARCH_TOKENIZER_VERSION,
                        utc_iso(datetime.now(UTC)),
                    ),
                )
            else:
                updated = connection.exec_driver_sql(
                    "UPDATE search_index_state SET condition='REBUILDING', "
                    "checkpoint_revision=0, generation=?, "
                    "schema_version=?, tokenizer_version=?, detail=NULL, updated_at=? "
                    "WHERE singleton_id=1",
                    (
                        generation,
                        SEARCH_SCHEMA_VERSION,
                        SEARCH_TOKENIZER_VERSION,
                        utc_iso(datetime.now(UTC)),
                    ),
                )
                if updated.rowcount != 1:
                    raise SearchIndexInvalid("search index singleton is unavailable")
            commit_attempted = True
            self._commit_search_transaction(connection, "mark rebuilding")
            self._search_generation_high_water = generation
            return generation
        except SearchIndexInvalid:
            if connection is not None and not commit_attempted:
                self._rollback_search_transaction(connection, "mark rebuilding")
            raise
        except (DBAPIError, sqlite3.DatabaseError, IntegrityError, RuntimeError) as exc:
            if connection is not None and not commit_attempted:
                self._rollback_search_transaction(connection, "mark rebuilding")
            raise SearchIndexInvalid("failed to mark search index rebuilding") from exc
        finally:
            if connection is not None:
                self._close_search_connection(connection, "mark rebuilding")

    def rebuild_search_index(self) -> SearchStatus:
        self._ensure_open()
        if not self._search_available:
            raise SearchUnavailable("SQLite FTS5 is unavailable in this runtime")
        with self._authority.transition():
            generation = self._write_rebuild_state()
            connection = None
            commit_attempted = False
            try:
                connection = self._engine.connect()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                source_revision = self._require_search_source_revision(connection)
                projections = self._all_search_projections(connection)
                connection.exec_driver_sql("DELETE FROM search_fts")
                connection.exec_driver_sql("DELETE FROM search_document_keys")
                occupied: set[int] = set()
                for projection in projections:
                    rowid = deterministic_rowid(projection.document_key, occupied)
                    occupied.add(rowid)
                    connection.exec_driver_sql(
                        "INSERT INTO search_document_keys"
                        "(fts_rowid, document_kind, document_id) VALUES (?, ?, ?)",
                        (rowid, projection.document_kind, projection.document_id),
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO search_fts"
                        "(rowid, document_key, title, body, filename) VALUES (?, ?, ?, ?, ?)",
                        (
                            rowid,
                            projection.document_key,
                            projection.title,
                            projection.body,
                            projection.filename,
                        ),
                    )
                validate_phase7_rebuild(connection)
                self._validate_search_projection_contents(connection, projections)
                updated = connection.exec_driver_sql(
                    "UPDATE search_index_state SET condition='VALID', checkpoint_revision=?, "
                    "generation=?, detail=NULL, updated_at=? WHERE singleton_id=1 "
                    "AND condition='REBUILDING' AND generation=?",
                    (
                        source_revision,
                        generation,
                        utc_iso(datetime.now(UTC)),
                        generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise SearchIndexInvalid("search rebuild generation changed")
                commit_attempted = True
                self._commit_search_transaction(connection, "search rebuild")
                self._search_receipts.acknowledge(source_revision)
            except SearchIndexInvalid:
                if connection is not None and not commit_attempted:
                    self._rollback_search_transaction(connection, "search rebuild")
                raise
            except (DBAPIError, sqlite3.DatabaseError, IntegrityError) as exc:
                if connection is not None and not commit_attempted:
                    self._rollback_search_transaction(connection, "search rebuild")
                raise SearchIndexInvalid("search rebuild failed") from exc
            finally:
                if connection is not None:
                    self._close_search_connection(connection, "search rebuild")
        return self.search_status()

    def diagnose_search_index(self) -> SearchStatus:
        self._ensure_open()
        if not self._search_available:
            return self.search_status()
        with self._authority.transition():
            status: SearchStatus | None = None
            try:
                with self._search_transaction("search diagnostics") as connection:
                    status = self._search_status_from_connection(connection)
                    if status.generation is None or status.checkpoint_revision is None:
                        return status
                    validate_phase7_rebuild(connection)
                    projections = self._all_search_projections(connection)
                    self._validate_search_projection_contents(connection, projections)
            except (SearchIndexInvalid, DBAPIError, sqlite3.DatabaseError, RuntimeError) as exc:
                if status is None:
                    status = self.search_status()
                assert status.generation is not None
                assert status.checkpoint_revision is not None
                try:
                    self._write_search_invalid_state(
                        str(exc),
                        expected_generation=status.generation,
                        expected_checkpoint=status.checkpoint_revision,
                    )
                except SearchIndexInvalid:
                    return SearchStatus(
                        SearchIndexCondition.INVALID,
                        status.source_revision,
                        status.checkpoint_revision,
                        status.generation,
                        SEARCH_SCHEMA_VERSION,
                        SEARCH_TOKENIZER_VERSION,
                        str(exc),
                    )
            return self.search_status()

    def _branch_messages_in_snapshot(
        self, connection, chat: Chat, leaf_message_id: str | None
    ) -> tuple[Message, ...]:
        if leaf_message_id is None:
            return ()
        rows = connection.execute(
            select(messages)
            .where(messages.c.chat_id == chat.id)
            .order_by(messages.c.sequence)
        ).fetchall()
        by_id = {str(row.id): _message(row) for row in rows}
        branch: list[Message] = []
        seen: set[str] = set()
        current: str | None = leaf_message_id
        while current is not None:
            if current in seen:
                raise SearchResultGone("message lineage contains a cycle")
            seen.add(current)
            message = by_id.get(current)
            if message is None:
                raise SearchResultGone("search result message is gone")
            branch.append(message)
            current = message.parent_id
        branch.reverse()
        return tuple(branch)

    def resolve_search_result(
        self,
        result: SearchResult,
        *,
        location_index: int = 0,
    ) -> SearchNavigation:
        self._ensure_open()
        if not isinstance(result, SearchResult):
            raise SearchResultGone("search result identity is invalid")
        if type(location_index) is not int or location_index < 0:
            raise SearchResultGone("search result location is invalid")
        with self._search_transaction("search result navigation") as connection:
            try:
                focus_message_id: str | None = None
                if result.document_kind is SearchDocumentKind.CHAT:
                    chat_id = result.document_id
                    leaf_message_id = None
                    branch_state = SearchBranchState.ACTIVE
                elif result.document_kind is SearchDocumentKind.MESSAGE:
                    message_row = connection.execute(
                        select(messages).where(messages.c.id == result.document_id)
                    ).first()
                    if message_row is None or str(message_row.chat_id) != result.chat_id:
                        raise SearchResultGone("search result message is gone")
                    chat_id = str(message_row.chat_id)
                    focus_message_id = str(message_row.id)
                    active = self._message_is_active(connection, chat_id, focus_message_id)
                    branch_state = (
                        SearchBranchState.ACTIVE if active else SearchBranchState.HISTORICAL
                    )
                    leaf_message_id = None if active else focus_message_id
                else:
                    if location_index >= len(result.locations):
                        raise SearchResultGone("search result location is gone")
                    chosen = result.locations[location_index]
                    if chosen.message_id is None:
                        raise SearchResultGone("attachment search location is malformed")
                    exists = connection.exec_driver_sql(
                        "SELECT 1 FROM attachments a JOIN message_attachments ma "
                        "ON ma.attachment_id=a.id JOIN messages m ON m.id=ma.message_id "
                        "WHERE a.id=? AND ma.message_id=? AND m.chat_id=?",
                        (result.document_id, chosen.message_id, chosen.chat_id),
                    ).first()
                    if exists is None:
                        raise SearchResultGone("attachment search location is gone")
                    chat_id = chosen.chat_id
                    focus_message_id = chosen.message_id
                    active = self._message_is_active(connection, chat_id, focus_message_id)
                    branch_state = (
                        SearchBranchState.ACTIVE if active else SearchBranchState.HISTORICAL
                    )
                    leaf_message_id = None if active else focus_message_id
                chat_row = connection.execute(
                    select(chats).where(chats.c.id == chat_id)
                ).first()
                if chat_row is None:
                    raise SearchResultGone("search result chat is gone")
                chat = _chat(chat_row)
                if not result.include_archived and chat.archived_at is not None:
                    raise SearchResultGone("search result is no longer visible")
                if (
                    result.active_branch_only
                    and branch_state is SearchBranchState.HISTORICAL
                ):
                    raise SearchResultGone("search result is no longer visible")
                presentation_leaf = leaf_message_id or chat.head_message_id
                branch = self._branch_messages_in_snapshot(
                    connection, chat, presentation_leaf
                )
                navigation = SearchNavigation(
                    chat,
                    branch,
                    focus_message_id,
                    leaf_message_id,
                    branch_state,
                    chat.archived_at,
                )
                return navigation
            except BaseException:
                raise

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

    @property
    def closed(self) -> bool:
        return self._closed

    def _close_under_authority(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                with self._engine.connect() as connection:
                    mode = str(
                        connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
                    ).casefold()
                    synchronous = int(
                        connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
                    )
                    if mode != "delete":
                        raise StateError("steady SQLite journal mode changed before close")
                    if synchronous != 2:
                        raise StateError("steady SQLite synchronous mode changed before close")
            finally:
                self._attachment_manager.close()
                self._engine.dispose()

    def _release_after_invalidation(self) -> None:
        """Release bearers only; never open a recovery/inspection connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self._attachment_manager.close()
        finally:
            self._engine.dispose()

    def close(self) -> None:
        self._authority.close()


def _authority_store_operation(method):
    """Give every public store operation a callee-owned logical grant."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            with self._authority.operation():
                return method(self, *args, **kwargs)
        except AuthorityError as exc:
            raise StateError(
                "state store is not admitting work; fresh-authority recovery is required"
            ) from exc

    return guarded


_SQLITE_OPERATION_METHODS = (
    "create_chat",
    "archive_chat",
    "ingest_attachment",
    "get_attachment",
    "list_attachments",
    "delete_attachment",
    "read_attachment_bytes",
    "list_message_attachments",
    "list_attempt_attachments",
    "gc_attachments",
    "list_chats",
    "get_chat",
    "get_message",
    "list_messages",
    "list_branch_messages",
    "list_revisions",
    "list_generation_attempts",
    "list_active_generation_attempts",
    "get_generation_attempt",
    "next_message_sequence",
    "persist_generation_start",
    "persist_regeneration_start",
    "update_streaming_message",
    "finalize_generation",
    "reconcile_interrupted_generations",
    "update_attempt",
    "search_status",
    "search",
    "rebuild_search_index",
    "diagnose_search_index",
    "resolve_search_result",
    "list_workspace_windows",
    "save_workspace_window",
    "delete_workspace_window",
)

_PHASE5_OPERATION_METHODS = (
    "list_provider_connections",
    "get_provider_connection",
    "create_provider_connection",
    "update_provider_connection",
    "set_provider_connection_enabled",
    "retire_provider_connection",
    "list_model_catalogue_entries",
    "get_model_catalogue_entry",
    "get_model_catalogue_entry_by_provider_id",
    "add_manual_model",
    "refresh_model_catalogue",
    "list_capability_facts",
    "set_capability_fact",
    "list_capability_overrides",
    "set_capability_override",
    "add_capability_observation",
    "get_application_generation_config",
    "get_application_generation_settings",
    "set_application_generation_settings",
    "set_application_default_model",
    "get_model_generation_settings",
    "get_model_generation_config",
    "set_model_generation_settings",
    "get_chat_model_generation_settings",
    "get_chat_model_generation_config",
    "set_chat_model_generation_settings",
    "get_chat_model_selection",
    "set_chat_model_selection",
)

for _method_name in _SQLITE_OPERATION_METHODS:
    setattr(
        SQLiteAppStateStore,
        _method_name,
        _authority_store_operation(getattr(SQLiteAppStateStore, _method_name)),
    )

for _method_name in _PHASE5_OPERATION_METHODS:
    setattr(
        Phase5StoreMixin,
        _method_name,
        _authority_store_operation(getattr(Phase5StoreMixin, _method_name)),
    )
