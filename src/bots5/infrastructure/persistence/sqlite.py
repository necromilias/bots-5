from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
from pathlib import Path

from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, replace
from collections.abc import Callable
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
from bots5.core.interchange import canonical_json_bytes
from bots5.core.import_history import ContinuationReadiness, ContinuationRequirement
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
    CapabilityKey,
    CapabilitySource,
    CapabilityState,
    CatalogueAvailability,
    CatalogueRefreshFailureClass,
    CatalogueRefreshStatus,
    validate_capability_value,
    GenerationSettings,
)
from bots5.core.provider_configuration import _validate_settings, resolve_capability
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


# Import publication creates one capture inode per distinct new payload plus
# exact journal/row/index records.  These fixed units are deliberately larger
# than their serialized rows and are rounded to the observed filesystem unit.
_PHASE9_PAYLOAD_ROW_OVERHEAD = 16 * 1024
_PHASE9_PAYLOAD_JOURNAL_OVERHEAD = 32 * 1024
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
    arm_phase9_import_graph,
    arm_phase9_import_continuation_rows,
    arm_phase9_object_derivations,
    clear_phase9_object_derivations,
    clear_phase9_import_graph,
    arm_phase9_attachment_healing,
    clear_phase9_attachment_healing,
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
from .phase9_schema import validate_phase9_schema
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
    CapturedAttachment,
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
from bots5.core.archive_import import (
    ArchiveSourcePlan,
    ImportErrorCode,
    capture_validated_archive,
    close_payload_snapshots,
    resolve_external_payloads,
    resolve_source,
    source_fingerprint,
    with_initial_receiver_continuation,
)
from bots5.core.import_queue import ImportQueueState, QueueItem, QueuePage, reorder as reorder_queue
from bots5.infrastructure.persistence.archive_import_store import (
    ArchiveImportStoreError,
    JournalIntent,
    assert_enqueueable,
    begin_graph_commit,
    complete_known_graph,
    begin_staging,
    cancel_waiting,
    claim,
    advance_queue_control,
    enqueue as enqueue_archive_import,
    fail_known_pregraph,
    queue_control,
    queue_item as archive_queue_item,
    recover_known_operation,
    settle_preflight_without_journal,
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
_PHASE8_REVISION = "0011_phase8_inspector_state"
_PHASE9_REVISION = "0012_phase9_archive_import"
_PHASE8_WORKSPACE_COLUMN_TYPES = {
    "inspector_open": "BOOLEAN",
    "inspector_message_id": "VARCHAR(64)",
    "inspector_leaf_message_id": "VARCHAR(64)",
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


def _validate_phase6_attempt_authority(connection, attempt: GenerationAttempt, branch_choice_context=None) -> None:
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
    branch_choice = connection.exec_driver_sql(
        "SELECT c.explicit_settings FROM archive_continuation_branches b JOIN archive_continuation_choices c ON c.chat_id=b.chat_id AND c.base_key=b.base_key AND c.choice_revision=b.choice_revision WHERE b.attempt_id=? AND c.local_model_entry_id=? AND c.local_connection_id=?",
        (attempt.id, model_entry_id, connection_id),
    ).first()
    if branch_choice is None and branch_choice_context is not None:
        base_key, choice_revision = branch_choice_context
        branch_choice = connection.exec_driver_sql(
            "SELECT explicit_settings FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=? AND local_model_entry_id=? AND local_connection_id=?",
            (attempt.chat_id, base_key, choice_revision, model_entry_id, connection_id),
        ).first()
    if (selection is None or selection.model_entry_id != model_entry_id or selection.selection_required) and branch_choice is None:
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
    if branch_choice is not None:
        try:
            branch_settings = json.loads(str(branch_choice[0]))
        except (TypeError, ValueError) as exc:
            raise StateError("imported continuation choice settings are malformed") from exc
        if not isinstance(branch_settings, dict) or set(branch_settings) != set(defaults):
            raise StateError("imported continuation choice settings are malformed")
        for key, value in branch_settings.items():
            if value is not None:
                effective[key] = value
                provenance[key] = "branch"
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
    phase7 = expected_revision in {PHASE7_REVISION, _PHASE8_REVISION, _PHASE9_REVISION}
    if phase7:
        arm_phase7_source_mutation(connection, "phase7 schema validation")
    try:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one_or_none()
        if revision != expected_revision:
            raise RuntimeError("current database revision is not authoritative")
        if expected_revision != _PHASE9_REVISION:
            integrity = __import__("bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries", fromlist=["_validate_existing_state"])
            integrity._validate_existing_state(connection)
        _validate_phase4_schema(connection)
        if expected_revision in {_PHASE8_REVISION, _PHASE9_REVISION}:
            _validate_phase8_schema(connection)
        _validate_phase5_schema(connection)
        _validate_phase5_trigger_behavior(connection)
        validate_phase6_schema(connection, destructive=destructive_phase6)
        if expected_revision == _PHASE9_REVISION:
            validate_phase9_schema(connection)
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
            inspector_open=bool(mapping.get("inspector_open", False)),
            inspector_message_id=mapping.get("inspector_message_id"),
            inspector_leaf_message_id=mapping.get("inspector_leaf_message_id"),
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


def _validate_phase8_schema(connection) -> None:
    workspace_columns = {
        row[1]: row
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(workspace_windows)"
        ).fetchall()
    }
    missing = sorted(set(_PHASE8_WORKSPACE_COLUMN_TYPES) - set(workspace_columns))
    if missing:
        raise RuntimeError(
            "current Phase 8 workspace_windows schema is missing columns: "
            + ", ".join(missing)
        )
    wrong_types = sorted(
        name
        for name, expected_type in _PHASE8_WORKSPACE_COLUMN_TYPES.items()
        if str(workspace_columns[name][2]).upper().replace(" ", "") != expected_type
    )
    if wrong_types:
        raise RuntimeError(
            "current Phase 8 workspace_windows columns have invalid declared types: "
            + ", ".join(
                f"{name}={workspace_columns[name][2]}" for name in wrong_types
            )
        )
    if workspace_columns["inspector_open"][3] != 1:
        raise RuntimeError(
            "current Phase 8 workspace_windows inspector_open must be non-null"
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


@dataclass(frozen=True, slots=True)
class ArchiveImportPreflight:
    """One owned, sealed intake plan awaiting its durable settlement."""

    queue_id: str
    queue_revision: int
    owner_epoch: str
    import_as_archived: bool
    captured: ArchiveSourcePlan

    def close(self) -> None:
        """Release the private snapshots if no durable settlement will run."""
        close_payload_snapshots(self.captured)


@dataclass(frozen=True, slots=True)
class ArchiveImportSettlement:
    """A plan that crossed the durable cutoff and must now settle or recover."""

    preflight: ArchiveImportPreflight
    operation_id: str
    timestamp: str
    staging_revision: int


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
                revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one_or_none()
                if revision != _PHASE9_REVISION:
                    integrity = __import__("bots5.infrastructure.persistence.migrations.versions.0003_integrity_boundaries", fromlist=["_validate_existing_state"])
                    integrity._validate_existing_state(connection)
                column_info = {
                    row[1]: row
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info(generation_attempts)"
                    ).fetchall()
                }
                columns = set(column_info)
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
                if revision in {_PHASE8_REVISION, _PHASE9_REVISION}:
                    _validate_open_connection(
                        connection,
                        expected_revision=revision,
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
            # A new authority holds the database/root claim only after the
            # old owner is gone.  Reconcile durable journal facts before any
            # ordinary command can claim work; this never reopens intake
            # source bytes and only releases an unjournalled PREFLIGHTING
            # claim whose prior owner cannot still run.
            store._recover_archive_imports_startup(now=datetime.now(UTC))
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

    # Phase 9 Archive import -------------------------------------------------
    # The queue has two public effects deliberately.  Admission records one
    # immutable source identity, while staging captures and validates that
    # identity before its durable, non-cancellable cutoff transaction.
    def enqueue_archive_import(
        self,
        source: Path | str,
        *,
        resolver_roots: tuple[Path | str, ...],
        now,
        queue_id: str | None = None,
        import_as_archived: bool = False,
    ) -> QueueItem:
        self._ensure_open()
        # Queue admission deliberately does not resolve roots or read archive
        # content.  Those potentially expensive and replacement-sensitive
        # operations belong to the worker's preflight immediately before the
        # first durable cutoff.
        path = Path(source)
        if not path.is_absolute() or type(import_as_archived) is not bool:
            raise StateError("archive import intake path or resolver roots are invalid")
        if any(not Path(root).is_absolute() for root in resolver_roots):
            raise StateError("archive import resolver root is invalid")
        fingerprint = source_fingerprint(path)
        identifier = queue_id or str(uuid7())
        if not identifier:
            raise StateError("archive import queue ID is invalid")
        item = QueueItem(identifier, 1, 0, ImportQueueState.QUEUED, fingerprint)
        timestamp = utc_iso(now)
        with self.command_admission():
            with self._authority.transition():
                connection = self._engine.connect()
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    assert_enqueueable(connection)
                    enqueue_archive_import(
                        connection, item, source_path=str(path),
                        resolver_roots=tuple(str(Path(root)) for root in resolver_roots),
                        options={"import_as_archived": import_as_archived}, enqueued_at=timestamp,
                    )
                    self._commit_attachment_transaction(connection, "enqueue archive import")
                except ArchiveImportStoreError as exc:
                    self._rollback_attachment_transaction(connection, "enqueue archive import")
                    raise StateError(str(exc)) from exc
                except BaseException:
                    self._rollback_attachment_transaction(connection, "enqueue archive import")
                    raise
                finally:
                    connection.close()
        return item

    def cancel_archive_import(self, queue_id: str, *, expected_revision: int, now) -> QueueItem:
        """CAS-cancel an unjournalled import at the shared queue mutex."""
        self._ensure_open()
        if not queue_id or type(expected_revision) is not int or expected_revision < 1:
            raise StateError("archive import cancellation request is invalid")
        timestamp = utc_iso(now)
        with self.command_admission(independent=True), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                cancelled = cancel_waiting(connection, queue_id, expected_revision, now=timestamp)
                self._commit_attachment_transaction(connection, "archive import cancellation")
                return cancelled
            except ArchiveImportStoreError as exc:
                self._rollback_attachment_transaction(connection, "archive import cancellation")
                raise StateError(str(exc)) from exc
            except BaseException:
                self._rollback_attachment_transaction(connection, "archive import cancellation")
                raise
            finally:
                self._close_attachment_connection(connection, "archive import cancellation")

    def list_archive_imports(self, *, limit: int = 50, cursor: tuple[int, str] | None = None) -> QueuePage:
        self._ensure_open()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StateError("archive import page request is invalid")
        args: tuple[object, ...] = ()
        where = ""
        if cursor is not None:
            if type(cursor) is not tuple or len(cursor) != 2 or type(cursor[0]) is not int or type(cursor[1]) is not str:
                raise StateError("archive import page request is invalid")
            where = " WHERE ordinal>? OR (ordinal=? AND id>?)"
            args = (cursor[0], cursor[0], cursor[1])
        with self.command_admission(), self._engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            try:
                queue_revision, _, _ = queue_control(connection)
                rows = connection.exec_driver_sql(
                    "SELECT id,queue_revision,ordinal,state,source_device,source_inode,source_size,source_mtime_ns,source_ctime_ns,failure_code,operation_id FROM archive_import_queue" + where + " ORDER BY ordinal,id LIMIT ?",
                    (*args, limit + 1),
                ).fetchall()
            finally:
                connection.exec_driver_sql("ROLLBACK")
        items = tuple(QueueItem(str(row[0]), int(row[1]), int(row[2]), ImportQueueState(str(row[3])), tuple(int(value) for value in row[4:9]), row[9], row[10]) for row in rows[:limit])
        return QueuePage(items, (items[-1].ordinal, items[-1].id) if len(rows) > limit else None, queue_revision)

    def reorder_archive_imports(self, expected_queue_revision: int, ordered_ids: tuple[str, ...], *, now) -> tuple[QueueItem, ...]:
        self._ensure_open()
        if type(expected_queue_revision) is not int or expected_queue_revision < 0:
            raise StateError("archive import queue revision is invalid")
        with self.command_admission(), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                queue_revision, claimed_queue_id, owner_epoch = queue_control(connection)
                if queue_revision != expected_queue_revision:
                    raise ArchiveImportStoreError("queue reorder revision conflict")
                if claimed_queue_id is not None or owner_epoch is not None:
                    raise ArchiveImportStoreError("queue reorder is unavailable while an import is active")
                rows = connection.exec_driver_sql("SELECT id,queue_revision,ordinal,state,source_device,source_inode,source_size,source_mtime_ns,source_ctime_ns,failure_code,operation_id FROM archive_import_queue WHERE state='QUEUED' ORDER BY ordinal,id").fetchall()
                current = tuple(QueueItem(str(row[0]),int(row[1]),int(row[2]),ImportQueueState(str(row[3])),tuple(int(x) for x in row[4:9]),row[9],row[10]) for row in rows)
                changed = reorder_queue(current, ordered_ids)
                for item in changed:
                    if connection.exec_driver_sql("UPDATE archive_import_queue SET ordinal=?,queue_revision=? WHERE id=? AND queue_revision=? AND state='QUEUED'", (item.ordinal,item.revision,item.id,item.revision-1)).rowcount != 1:
                        raise ArchiveImportStoreError("queue reorder compare-and-set failed")
                advance_queue_control(
                    connection, queue_revision, claimed_queue_id=None, owner_epoch=None,
                )
                self._commit_attachment_transaction(connection, "reorder archive imports")
                return changed
            except (ArchiveImportStoreError, ValueError) as exc:
                self._rollback_attachment_transaction(connection, "reorder archive imports")
                raise StateError(str(exc)) from exc
            finally:
                self._close_attachment_connection(connection, "reorder archive imports")

    def remove_waiting_archive_import(self, queue_id: str, *, expected_revision: int, now) -> QueueItem:
        self._ensure_open()
        with self.command_admission(), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                removed = cancel_waiting(connection, queue_id, expected_revision, now=utc_iso(now), queued_only=True)
                self._commit_attachment_transaction(connection, "remove waiting archive import")
                return removed
            except ArchiveImportStoreError as exc:
                self._rollback_attachment_transaction(connection, "remove waiting archive import")
                raise StateError(str(exc)) from exc
            finally:
                self._close_attachment_connection(connection, "remove waiting archive import")

    def retry_archive_import(self, queue_id: str, *, now) -> QueueItem:
        self._ensure_open(); timestamp = utc_iso(now)
        with self.command_admission(), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                row = connection.exec_driver_sql("SELECT source_path,resolver_roots,options,state FROM archive_import_queue WHERE id=?", (queue_id,)).first()
                if row is None or str(row[3]) not in {"FAILED", "CANCELLED"}:
                    raise ArchiveImportStoreError("only failed or cancelled imports may retry")
                fingerprint = source_fingerprint(Path(str(row[0])))
                ordinal = int(connection.exec_driver_sql("SELECT coalesce(max(ordinal),-1)+1 FROM archive_import_queue").scalar_one())
                item = QueueItem(str(uuid7()), 1, ordinal, ImportQueueState.QUEUED, fingerprint)
                enqueue_archive_import(connection, item, source_path=str(row[0]), resolver_roots=tuple(json.loads(str(row[1]))), options=json.loads(str(row[2])), enqueued_at=timestamp, retry_of=queue_id)
                self._commit_attachment_transaction(connection, "retry archive import")
                return item
            except (ArchiveImportStoreError, ValueError, OSError) as exc:
                self._rollback_attachment_transaction(connection, "retry archive import")
                raise StateError(str(exc)) from exc
            finally:
                self._close_attachment_connection(connection, "retry archive import")

    def clear_archive_import_history(self, ids: tuple[str, ...]) -> None:
        self._ensure_open()
        if not ids or len(set(ids)) != len(ids) or any(not value for value in ids):
            raise StateError("archive import clear request is invalid")
        markers = ",".join("?" for _ in ids)
        with self.command_admission(), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                rows = connection.exec_driver_sql(f"SELECT id,state FROM archive_import_queue WHERE id IN ({markers})", ids).fetchall()
                if len(rows) != len(ids) or any(str(row[1]) not in {"COMPLETED", "FAILED", "CANCELLED"} for row in rows):
                    raise ArchiveImportStoreError("only terminal import history may clear")
                connection.exec_driver_sql(f"DELETE FROM archive_import_queue WHERE id IN ({markers})", ids)
                revision, claimed_queue_id, owner_epoch = queue_control(connection)
                advance_queue_control(
                    connection, revision, claimed_queue_id=claimed_queue_id, owner_epoch=owner_epoch,
                )
                self._commit_attachment_transaction(connection, "clear archive import history")
            except ArchiveImportStoreError as exc:
                self._rollback_attachment_transaction(connection, "clear archive import history")
                raise StateError(str(exc)) from exc
            finally:
                self._close_attachment_connection(connection, "clear archive import history")

    def stage_archive_import(
        self,
        queue_id: str,
        *,
        now,
        operation_id: str | None = None,
    ) -> QueueItem:
        """Capture, validate, then durably cross the import cancellation cutoff."""
        self._ensure_open()
        if not queue_id:
            raise StateError("archive import queue ID is invalid")
        # Read queued intake facts first. No state changes occur if capture,
        # resolver admission, or wire validation fails.
        with self.command_admission():
            with self._engine.connect() as connection:
                row = connection.exec_driver_sql(
                    "SELECT source_path,source_device,source_inode,source_size,source_mtime_ns,source_ctime_ns,resolver_roots,state,queue_revision FROM archive_import_queue WHERE id=?",
                    (queue_id,),
                ).first()
        if row is None:
            raise StateError("archive import queue item is absent")
        if str(row[7]) != ImportQueueState.QUEUED.value:
            raise StateError("archive import queue item is no longer waiting")
        try:
            roots_raw = json.loads(str(row[6]))
            if type(roots_raw) is not list or any(type(root) is not str for root in roots_raw):
                raise ValueError
            path = resolve_source(Path(str(row[0])), tuple(Path(root) for root in roots_raw))
            plan = capture_validated_archive(path, tuple(int(value) for value in row[1:6]))
            plan = resolve_external_payloads(plan, tuple(Path(root) for root in roots_raw))
        except ImportErrorCode as exc:
            raise StateError(str(exc)) from exc
        except (ValueError, ArchiveImportStoreError) as exc:
            raise StateError("archive import intake record is malformed") from exc
        timestamp = utc_iso(now)
        operation = operation_id or str(uuid7())
        try:
            digest = bytes.fromhex(plan.binding.sha256)
            graph_digest = plan.plan.graph_plan_sha256
        except ValueError as exc:
            close_payload_snapshots(plan)
            raise StateError("archive import capture digest is malformed") from exc
        intent = JournalIntent(
            operation, plan.archive_id, plan.archive_version, plan.logical_content_digest,
            plan.source_chat_id, plan.source_chat_revision, digest, plan.binding.byte_size,
            timestamp, timestamp, graph_digest,
            self._sealed_import_graph(plan.plan),
            tuple({"digest": bytes.fromhex(digest), "size": raw.size} for digest, raw in plan.payloads),
        )
        with self.command_admission(independent=True):
            with self._authority.transition():
                connection = self._engine.connect()
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    current = archive_queue_item(connection, queue_id)
                    if current.state is not ImportQueueState.QUEUED or current.revision != int(row[8]):
                        raise ArchiveImportStoreError("archive import queue changed during preflight")
                    claimed = claim(connection, queue_id, current.revision, now=timestamp, owner_epoch=operation)
                    staged = begin_staging(connection, queue_id, claimed.revision, intent)
                    self._commit_attachment_transaction(connection, "stage archive import")
                except ArchiveImportStoreError as exc:
                    self._rollback_attachment_transaction(connection, "stage archive import")
                    close_payload_snapshots(plan)
                    raise StateError(str(exc)) from exc
                except BaseException:
                    self._rollback_attachment_transaction(connection, "stage archive import")
                    close_payload_snapshots(plan)
                    raise
                finally:
                    connection.close()
        close_payload_snapshots(plan)
        return staged

    def preflight_archive_import(
        self, queue_id: str, *, owner_epoch: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ArchiveImportPreflight:
        """Capture one queue item before its first durable import effect.

        The returned object owns anonymous payload snapshots.  Its caller must
        pass it to :meth:`settle_archive_import` or close its snapshots if the
        cancellable preflight is abandoned before the cutoff.
        """
        self._ensure_open()
        if not queue_id:
            raise StateError("archive import queue ID is invalid")
        owner = owner_epoch or str(uuid7())
        with self.command_admission(independent=True), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                current = archive_queue_item(connection, queue_id)
                claimed = claim(
                    connection, queue_id, current.revision, now=utc_iso(datetime.now(UTC)),
                    owner_epoch=owner,
                )
                self._commit_attachment_transaction(connection, "archive import preflight claim")
            except ArchiveImportStoreError as exc:
                self._rollback_attachment_transaction(connection, "archive import preflight claim")
                raise StateError(str(exc)) from exc
            except BaseException:
                self._rollback_attachment_transaction(connection, "archive import preflight claim")
                raise
            finally:
                self._close_attachment_connection(connection, "archive import preflight claim")
        with self.command_admission(independent=True), self._engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT source_path,source_device,source_inode,source_size,source_mtime_ns,source_ctime_ns,resolver_roots,options,state,queue_revision FROM archive_import_queue WHERE id=?",
                (queue_id,),
            ).first()
        if (row is None or str(row[8]) != ImportQueueState.PREFLIGHTING.value
                or int(row[9]) != claimed.revision):
            raise StateError("archive import queue item is no longer waiting")
        try:
            roots = json.loads(str(row[6])); options = json.loads(str(row[7]))
            if (type(roots) is not list or any(type(root) is not str for root in roots)
                    or type(options) is not dict or set(options) != {"import_as_archived"}
                    or type(options["import_as_archived"]) is not bool):
                raise ValueError
            source = resolve_source(Path(str(row[0])), tuple(Path(root) for root in roots))
            captured = resolve_external_payloads(
                capture_validated_archive(
                    source, tuple(int(value) for value in row[1:6]), cancelled=cancelled,
                ),
                tuple(Path(root) for root in roots),
                cancelled=cancelled,
            )
        except ImportErrorCode as exc:
            self._settle_archive_import_preflight(
                queue_id, claimed.revision, owner,
                target=(ImportQueueState.QUEUED if str(exc) == "RESOLUTION_CANCELLED" else ImportQueueState.FAILED),
                failure_code=None if str(exc) == "RESOLUTION_CANCELLED" else str(exc),
            )
            raise StateError(str(exc)) from exc
        except (ValueError, ArchiveImportStoreError) as exc:
            self._settle_archive_import_preflight(
                queue_id, claimed.revision, owner,
                target=ImportQueueState.FAILED, failure_code="ARCHIVE_INVALID",
            )
            raise StateError("archive import intake record is malformed") from exc
        try:
            with self._authority.operation(), self._engine.connect() as connection:
                # Receiver-local automatic equivalence is decided before the
                # first import journal effect and folded into the same sealed
                # plan as source identities.  Publication must consume these
                # facts; it may not query today's catalogue and bless a new
                # target after the cutoff.
                captured = replace(
                    captured,
                    plan=with_initial_receiver_continuation(
                        captured.plan,
                        self._plan_initial_receiver_continuation(captured.plan),
                    ),
                )
                captured = replace(
                    captured,
                    verified_ready_payloads=self._assert_import_payload_capacity(
                        captured, connection, verify_ready=True,
                    ),
                )
        except StateError as exc:
            close_payload_snapshots(captured)
            self._settle_archive_import_preflight(
                queue_id, claimed.revision, owner,
                target=ImportQueueState.FAILED, failure_code="RESOURCE_LIMIT",
            )
            raise
        return ArchiveImportPreflight(
            queue_id=queue_id,
            queue_revision=int(row[9]),
            owner_epoch=owner,
            import_as_archived=bool(options["import_as_archived"]),
            captured=captured,
        )

    def _settle_archive_import_preflight(
        self, queue_id: str, expected_revision: int, owner_epoch: str, *,
        target: ImportQueueState, failure_code: str | None,
    ) -> QueueItem:
        """Close an owned, journal-free preflight with its claim release."""
        timestamp = utc_iso(datetime.now(UTC))
        with self.command_admission(independent=True), self._authority.transition():
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                result = settle_preflight_without_journal(
                    connection, queue_id, expected_revision, owner_epoch,
                    target=target, failure_code=failure_code, now=timestamp,
                )
                self._commit_attachment_transaction(connection, "archive import preflight settlement")
                return result
            except ArchiveImportStoreError as exc:
                self._rollback_attachment_transaction(connection, "archive import preflight settlement")
                raise StateError(str(exc)) from exc
            except BaseException:
                self._rollback_attachment_transaction(connection, "archive import preflight settlement")
                raise
            finally:
                self._close_attachment_connection(connection, "archive import preflight settlement")

    @staticmethod
    def _round_capacity(value: int, unit: int) -> int:
        return ((value + unit - 1) // unit) * unit

    def _assert_import_payload_capacity(self, captured, connection, *, verify_ready: bool) -> tuple[tuple[str, int], ...]:
        """Check the exact retained payload plan without reading payload bytes.

        The preflight snapshots already consume their scratch allocation.  The
        only later scratch need is one bounded read buffer per distinct retained
        snapshot; destination needs one rooted capture per *new* digest plus
        conservative journal/SQLite/directory metadata.
        """
        distinct: dict[str, object] = {}
        for digest, snapshot in captured.payloads:
            prior = distinct.setdefault(digest, snapshot)
            if prior.size != snapshot.size:
                raise StateError("sealed payload digest has contradictory sizes")
        destination_device, destination_free, destination_unit = (
            self._attachment_manager.namespace_capacity("captures")
        )
        object_device, object_free, object_unit = self._attachment_manager.namespace_capacity("objects")
        if destination_device != object_device:
            raise StateError("attachment capture and object namespaces are on different devices")
        new_bytes = 0
        retained_ready = {
            digest: (size, identity)
            for digest, size, identity in captured.verified_ready_payloads
        }
        verified_ready: list[tuple[str, int, object]] = []
        for digest, snapshot in distinct.items():
            try:
                digest_bytes = bytes.fromhex(digest)
            except ValueError as exc:
                raise StateError("sealed archive payload digest is malformed") from exc
            existing = connection.execute(
                select(attachment_blobs.c.state, attachment_blobs.c.byte_size).where(
                    attachment_blobs.c.digest == digest_bytes
                )
            ).first()
            if existing is None:
                new_bytes += self._round_capacity(snapshot.size, destination_unit)
            elif existing.state != "ready" or int(existing.byte_size) != snapshot.size:
                raise StateError("archive payload lifecycle is not ready")
            elif verify_ready:
                # This is deliberately outside the cutoff transaction.  The
                # later transition only rechecks the retained identity.
                verified_ready.append((
                    digest, snapshot.size,
                    self._attachment_manager.verified_object_identity(
                        digest_bytes, expected_size=snapshot.size,
                    ),
                ))
            elif digest not in retained_ready:
                raise StateError("ready archive payload was not verified before cutoff")
            else:
                try:
                    current_identity = self._attachment_manager.object_identity(digest_bytes)
                except AttachmentIntegrityError as exc:
                    raise StateError("ready archive payload changed after preflight verification") from exc
                if current_identity != retained_ready[digest][1]:
                    raise StateError("ready archive payload changed after preflight verification")
        graph_bytes = len(canonical_json_bytes(self._sealed_import_graph(captured.plan)))
        payload_bytes = len(canonical_json_bytes([
            {"digest": digest, "size": snapshot.size}
            for digest, snapshot in distinct.items()
        ]))
        metadata = (
            graph_bytes + payload_bytes + _PHASE9_PAYLOAD_JOURNAL_OVERHEAD
            + len(distinct) * _PHASE9_PAYLOAD_ROW_OVERHEAD
        )
        destination_required = new_bytes + self._round_capacity(metadata, destination_unit)
        page_size = int(connection.exec_driver_sql("PRAGMA page_size").scalar_one())
        page_count = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
        if page_size <= 0 or page_count < 1:
            raise StateError("archive import database page size is invalid")
        schema_objects = int(connection.exec_driver_sql(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','index') "
            "AND name NOT LIKE 'sqlite_%'"
        ).scalar_one())
        if schema_objects < 1:
            raise StateError("archive import database schema capacity is invalid")
        # A SQLite rollback/WAL journal can transiently retain the complete
        # existing database.  For new graph growth, charge every current
        # table/index object one rounded sealed-graph image; this deliberately
        # overestimates the rows and all PK/unique/FK index fan-out without
        # relying on an unjustified fixed index count per row.
        database_journal_ceiling = page_count * page_size
        database_growth = self._round_capacity(
            (graph_bytes + payload_bytes + metadata + page_size) * schema_objects,
            page_size,
        )
        database_required = database_journal_ceiling + database_growth
        database_fd = self._authority._directory_fd("database")
        try:
            database_status = os.fstatvfs(database_fd)
            database_identity = os.fstat(database_fd)
            database_unit = database_status.f_frsize or database_status.f_bsize
        except OSError as exc:
            raise StateError("archive import database capacity is unavailable") from exc
        if database_unit <= 0:
            raise StateError("archive import database capacity is invalid")
        database_free = database_status.f_bavail * database_unit
        combined_required = destination_required + database_required
        if (
            destination_free < destination_required
            or object_free < self._round_capacity(metadata, object_unit)
            or database_free < database_required
            or (
                database_identity.st_dev == destination_device
                and destination_free < combined_required
            )
        ):
            raise StateError("archive import destination capacity is insufficient")
        return tuple(verified_ready)

    def execute_archive_import(
        self,
        queue_id: str,
        *,
        now,
        operation_id: str | None = None,
    ) -> str:
        """Import one queued archive from its one exact captured plan.

        This is intentionally separate from the stage-only diagnostic surface.
        It retains the canonical plan from descriptor capture through the
        authority-owned cutoff and known graph settlement; it never reopens the
        intake pathname after the plan has been sealed.
        """
        return self.settle_archive_import(
            self.cross_archive_import_cutoff(
                self.preflight_archive_import(queue_id, owner_epoch=operation_id),
                now=now, operation_id=operation_id,
            )
        )

    def cross_archive_import_cutoff(
        self,
        preflight: ArchiveImportPreflight,
        *,
        now,
        operation_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ArchiveImportSettlement:
        """Persist the first irreversible journal effect for one sealed plan."""
        self._ensure_open()
        queue_id = preflight.queue_id
        captured = preflight.captured
        # One frozen capture supplies both graph and payload plan.  External
        # candidates were verified during preflight; unresolved
        # ``missing-external`` rows remain truthful graph facts.
        timestamp = utc_iso(now)
        operation = operation_id or preflight.owner_epoch
        if operation != preflight.owner_epoch:
            close_payload_snapshots(captured)
            raise StateError("archive import operation does not own its preflight claim")
        try:
            intent = JournalIntent(
                operation, captured.archive_id, captured.archive_version,
                captured.logical_content_digest, captured.source_chat_id,
                captured.source_chat_revision, bytes.fromhex(captured.binding.sha256),
                captured.binding.byte_size, timestamp, timestamp,
                captured.plan.graph_plan_sha256, self._sealed_import_graph(captured.plan),
                tuple({"digest": bytes.fromhex(digest), "size": raw.size} for digest, raw in captured.payloads),
            )
        except ValueError as exc:
            close_payload_snapshots(captured)
            raise StateError("archive import capture digest is malformed") from exc
        with self._authority.transition():
            # A captured plan owns open payload snapshots until the durable
            # cutoff connection has closed successfully.  A committed journal
            # without a known close outcome remains an authority failure, and
            # must not leak those private handles while recovery determines
            # the durable result.
            try:
                connection = self._engine.connect()
            except BaseException:
                close_payload_snapshots(captured)
                raise
            handed_off = False
            close_pending = True
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                current = archive_queue_item(connection, queue_id)
                if current.state is ImportQueueState.CANCELLED:
                    raise StateError("RESOLUTION_CANCELLED")
                if (current.state is not ImportQueueState.PREFLIGHTING
                        or current.revision != preflight.queue_revision):
                    raise ArchiveImportStoreError("archive import queue changed during preflight")
                control = connection.exec_driver_sql(
                    "SELECT claimed_queue_id,owner_epoch FROM archive_import_queue_control WHERE singleton=1"
                ).first()
                if (control is None or str(control[0]) != queue_id
                        or str(control[1]) != operation):
                    raise ArchiveImportStoreError("archive import preflight owner changed")
                # This is the cancellation/effect linearization point.  The
                # transition to PREFLIGHTING is still inside this uncommitted
                # transaction; cancellation here rolls it back with no journal
                # or graph effect.  Once begin_staging executes, the journal
                # owns all later settlement.
                if cancelled is not None and cancelled():
                    raise StateError("RESOLUTION_CANCELLED")
                # Capacity may have changed after preflight.  Recheck only
                # retained-plan metadata and filesystem counters here; never
                # hash or reopen a source under the transition.
                self._assert_import_payload_capacity(captured, connection, verify_ready=False)
                staged = begin_staging(connection, queue_id, current.revision, intent)
                self._commit_attachment_transaction(connection, "archive import durable cutoff")
                settlement = ArchiveImportSettlement(
                    preflight=preflight, operation_id=operation, timestamp=timestamp,
                    staging_revision=staged.revision,
                )
            except ArchiveImportStoreError as exc:
                self._rollback_attachment_transaction(connection, "archive import durable cutoff")
                raise StateError(str(exc)) from exc
            except BaseException:
                self._rollback_attachment_transaction(connection, "archive import durable cutoff")
                raise
            else:
                # The sealed capture may be handed to settlement only after
                # the cutoff connection has a known close result.  Otherwise
                # restart recovery owns the durable journal, never live file
                # descriptors from this process.
                try:
                    close_pending = False
                    self._close_attachment_connection(connection, "archive import durable cutoff")
                except BaseException:
                    raise
                handed_off = True
                return settlement
            finally:
                if not handed_off:
                    try:
                        if close_pending:
                            self._close_attachment_connection(connection, "archive import durable cutoff")
                    finally:
                        close_payload_snapshots(captured)

    def settle_archive_import(self, settlement: ArchiveImportSettlement) -> str:
        """Settle the exact cutoff plan without reopening its intake source."""
        self._ensure_open()
        preflight = settlement.preflight
        captured = preflight.captured
        queue_id = preflight.queue_id
        operation = settlement.operation_id
        timestamp = settlement.timestamp
        with self._authority.transition():
            connection = None
            commit_attempted = False
            try:
                self._publish_import_payloads(captured, operation, timestamp)
                connection = self._engine.connect()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                committing = begin_graph_commit(
                    connection, queue_id, settlement.staging_revision, operation, now=timestamp,
                )
                self._assert_sealed_import_graph(connection, operation, captured.plan)
                source_revision = self._publish_import_graph(
                    connection, captured, operation, timestamp,
                    import_as_archived=preflight.import_as_archived,
                )
                completed = complete_known_graph(
                    connection, queue_id, committing.revision, operation,
                    captured.plan.local_id("chat", captured.source_chat_id), now=timestamp,
                )
                if completed.state is not ImportQueueState.COMPLETED:
                    raise ArchiveImportStoreError("import queue terminal marker is invalid")
                commit_attempted = True
                self._commit_attachment_transaction(connection, "archive import graph commit")
                self._accept_search_receipt(
                    SearchReceipt(
                        source_revision,
                        frozenset(
                            {
                                f"chat:{captured.plan.local_id('chat', captured.source_chat_id)}",
                                *(f"message:{captured.plan.local_id('message', str(item['source_id']))}" for item in captured.plan.messages),
                                *(f"attachment:{captured.plan.local_id('attachment', str(item['source_id']))}" for item in captured.plan.attachments),
                            }
                        ),
                    )
                )
                return captured.plan.local_id("chat", captured.source_chat_id)
            except ArchiveImportStoreError as exc:
                if not commit_attempted and connection is not None:
                    self._rollback_attachment_transaction(connection, "archive import graph commit")
                raise StateError(str(exc)) from exc
            except BaseException:
                if not commit_attempted and connection is not None:
                    self._rollback_attachment_transaction(connection, "archive import graph commit")
                raise
            finally:
                try:
                    if connection is not None:
                        self._close_attachment_connection(connection, "archive import graph commit")
                finally:
                    # This includes rollback and post-commit connection-close
                    # failures.  Payload snapshots are process resources, not
                    # recovery inputs, so recovery must never inherit them.
                    close_payload_snapshots(captured)

    def _publish_import_payloads(self, captured, operation_id: str, timestamp: str) -> None:
        """Publish sealed embedded bytes without creating a normal attachment ID."""
        for digest_text, raw in captured.payloads:
            try:
                digest = bytes.fromhex(digest_text)
            except ValueError as exc:
                raise StateError("sealed archive payload digest is malformed") from exc
            connection = self._engine.connect()
            captured_blob = None
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                existing = connection.execute(
                    select(attachment_blobs).where(attachment_blobs.c.digest == digest)
                ).first()
                if existing is not None:
                    if existing.state != "ready" or int(existing.byte_size) != raw.size:
                        raise StateError("archive payload lifecycle is not ready")
                    # Verify the existing canonical object before allocating a
                    # second capture.  This hashes in bounded chunks only.
                    self._attachment_manager.verify_object(digest, expected_size=raw.size)
                    changed = connection.exec_driver_sql(
                        "UPDATE archive_import_payload_reservations SET publication_state='READY' "
                        "WHERE operation_id=? AND digest=? AND size=? AND publication_state='PLANNED'",
                        (operation_id, digest, raw.size),
                    ).rowcount
                    if changed != 1:
                        raise StateError("archive payload reservation changed during publication")
                    self._commit_attachment_transaction(connection, "archive import payload ready dedup")
                    continue
                self._commit_attachment_transaction(connection, "archive import payload dedup absence")
                connection.close(); connection = None
                captured_blob = self._attachment_manager.capture_verified_snapshot(
                    raw.handle, digest, raw.size,
                )
                connection = self._engine.connect(); connection.exec_driver_sql("BEGIN IMMEDIATE")
                existing = connection.execute(
                    select(attachment_blobs).where(attachment_blobs.c.digest == digest)
                ).first()
                if existing is not None:
                    # The transition gate normally excludes this race.  If a
                    # recovered native publication won it, discard our exact
                    # capture and use the verified ready object.
                    if existing.state != "ready" or int(existing.byte_size) != raw.size:
                        raise StateError("archive payload lifecycle is not ready")
                    self._attachment_manager.verify_object(digest, expected_size=raw.size)
                    self._attachment_manager.discard_capture(captured_blob.operation_id)
                else:
                    arm_phase6_blob_transition(connection, digest, "", "staging", operation_id=captured_blob.operation_id, stage_name=captured_blob.operation_id, byte_size=raw.size)
                    try:
                        connection.execute(insert(attachment_blobs).values(digest=digest, byte_size=raw.size, state="staging", operation_id=captured_blob.operation_id, stage_name=captured_blob.operation_id, gc_id=None, created_at=timestamp)); require_phase6_consumed(connection)
                    finally: clear_phase6(connection)
                    self._commit_attachment_transaction(connection, "archive import payload staging")
                    connection.close(); connection = None
                    self._attachment_manager.capture_to_stage(captured_blob.operation_id, digest, raw.size)
                    self._attachment_manager.stage_to_object(captured_blob.operation_id, digest, raw.size)
                    self._attachment_manager.prove_staging_publication(captured_blob.operation_id, digest, raw.size)
                    connection = self._engine.connect(); connection.exec_driver_sql("BEGIN IMMEDIATE")
                    arm_phase6_blob_transition(connection, digest, "staging", "ready", byte_size=raw.size)
                    try:
                        connection.execute(update(attachment_blobs).where(attachment_blobs.c.digest == digest).values(state="ready", operation_id=None, stage_name=None, gc_id=None)); require_phase6_consumed(connection)
                    finally: clear_phase6(connection)
                changed = connection.exec_driver_sql(
                    "UPDATE archive_import_payload_reservations SET publication_state='READY' "
                    "WHERE operation_id=? AND digest=? AND size=? AND publication_state='PLANNED'",
                    (operation_id, digest, raw.size),
                ).rowcount
                if changed != 1:
                    raise StateError("archive payload reservation changed during publication")
                self._commit_attachment_transaction(connection, "archive import payload ready")
            except BaseException:
                if connection is not None:
                    self._rollback_attachment_transaction(connection, "archive import payload publication")
                raise
            finally:
                if connection is not None:
                    connection.close()

    def _recover_archive_imports(
        self, *, now, reclaim_interrupted_preflight: bool,
        startup: bool,
    ) -> tuple[tuple[str, str], ...]:
        """Mechanically settle only durable, non-contradictory import evidence.

        Recovery never reopens an intake pathname or replays a plan.  A durable
        pregraph operation with no graph becomes a retained failed result;
        committed evidence remains intact.  Any partial graph or contradictory
        marker poisons the authority through the normal transaction fence.
        """
        self._ensure_open()
        timestamp = utc_iso(now)
        outcomes: list[tuple[str, str]] = []
        admission = nullcontext() if startup else self.command_admission(independent=True)
        with admission:
            with self._authority.transition():
                connection = self._engine.connect()
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    rows = connection.exec_driver_sql(
                        "SELECT id FROM archive_import_operations "
                        "WHERE state IN ('STAGING','COMMITTING','COMMITTED') ORDER BY id"
                    ).fetchall()
                    for (operation_id,) in rows:
                        operation = str(operation_id)
                        mapped = connection.exec_driver_sql(
                            "SELECT chat_id FROM archive_import_chats WHERE operation_id=?",
                            (operation,),
                        ).fetchall()
                        def graph_exists(chat_id: str) -> bool:
                            return (
                                len(mapped) == 1
                                and str(mapped[0][0]) == chat_id
                                and connection.exec_driver_sql(
                                    "SELECT count(*) FROM chats WHERE id=?", (chat_id,)
                                ).scalar_one() == 1
                            )
                        result = recover_known_operation(
                            connection, operation, graph_exists=graph_exists
                        )
                        if result == "COMMITTED":
                            self._assert_reconstructed_import_graph(connection, operation)
                        if result == "KNOWN_NO_COMMIT":
                            queue = connection.exec_driver_sql(
                                "SELECT id,queue_revision FROM archive_import_queue WHERE operation_id=?",
                                (operation,),
                            ).first()
                            if queue is None:
                                raise ArchiveImportStoreError("pregraph operation lacks its queue item")
                            fail_known_pregraph(
                                connection, str(queue[0]), int(queue[1]), operation,
                                code="RECOVERED_NO_GRAPH", now=timestamp,
                            )
                        outcomes.append((operation, result))
                    revision, claimed_queue_id, owner_epoch = queue_control(connection)
                    if claimed_queue_id is not None:
                        row = connection.exec_driver_sql(
                            "SELECT state,operation_id,queue_revision FROM archive_import_queue WHERE id=?",
                            (claimed_queue_id,),
                        ).first()
                        if row is None:
                            raise ArchiveImportStoreError("queue controller refers to an absent item")
                        if (
                            reclaim_interrupted_preflight
                            and str(row[0]) == ImportQueueState.PREFLIGHTING.value
                            and row[1] is None
                            and owner_epoch is not None
                        ):
                            settle_preflight_without_journal(
                                connection, str(claimed_queue_id), int(row[2]), str(owner_epoch),
                                target=ImportQueueState.QUEUED, failure_code=None,
                                now=timestamp,
                            )
                            outcomes.append((str(claimed_queue_id), "RECLAIMED_PREFLIGHT"))
                        else:
                            raise ArchiveImportStoreError(
                                "interrupted queue claim cannot be safely reclaimed"
                            )
                    self._commit_attachment_transaction(connection, "recover archive imports")
                except ArchiveImportStoreError as exc:
                    self._rollback_attachment_transaction(connection, "recover archive imports")
                    self._authority.poison("archive import recovery evidence is contradictory")
                    raise StateError("archive import recovery requires controlled restart") from exc
                except BaseException:
                    self._rollback_attachment_transaction(connection, "recover archive imports")
                    raise
                finally:
                    connection.close()
        return tuple(outcomes)

    def recover_archive_imports(self, *, now) -> tuple[tuple[str, str], ...]:
        return self._recover_archive_imports(
            now=now, reclaim_interrupted_preflight=False, startup=False,
        )

    def _recover_archive_imports_startup(self, *, now) -> tuple[tuple[str, str], ...]:
        """Recover only after startup has proved the prior authority is gone."""
        return self._recover_archive_imports(
            now=now, reclaim_interrupted_preflight=True, startup=True,
        )

    @staticmethod
    def _sealed_import_graph(plan) -> dict[str, object]:
        """The exact preflight graph admitted at the durable cutoff."""
        return {
            "inventory": list(plan.graph_inventory), "chat": plan.chat,
            "messages": list(plan.messages), "attempts": list(plan.attempts),
            "attachments": list(plan.attachments), "context_plans": list(plan.context_plans),
            "message_attachment_relations": list(plan.message_attachment_relations),
            "attempt_attachment_relations": list(plan.attempt_attachment_relations),
            "chat_configuration": plan.chat_configuration,
            "archive_provenance": plan.archive_provenance, "manifest": plan.manifest,
            "source_binding": plan.source_binding,
            "object_provenance": list(plan.object_provenance),
            "provenance_node_ids": list(plan.provenance_node_ids),
            "continuation_history": plan.continuation_history,
            "history_bindings": list(plan.history_bindings),
            "initial_receiver_continuation": list(plan.initial_receiver_continuation),
        }

    @staticmethod
    def _journal_graph(connection, operation_id: str) -> dict[str, object]:
        row = connection.exec_driver_sql(
            "SELECT graph_plan_sha256,graph_inventory FROM archive_import_journal WHERE operation_id=?",
            (operation_id,),
        ).first()
        if row is None:
            raise ArchiveImportStoreError("import graph journal is absent")
        try:
            graph = json.loads(str(row[1]))
        except (TypeError, ValueError) as exc:
            raise ArchiveImportStoreError("import graph journal is malformed") from exc
        if not isinstance(graph, dict) or bytes(row[0]) != hashlib.sha256(canonical_json_bytes(graph)).digest():
            raise ArchiveImportStoreError("import graph journal digest is contradictory")
        return graph

    def _assert_sealed_import_graph(self, connection, operation_id: str, plan) -> None:
        if self._journal_graph(connection, operation_id) != self._sealed_import_graph(plan):
            raise ArchiveImportStoreError("import graph admission differs from its sealed journal")

    def _assert_reconstructed_import_graph(self, connection, operation_id: str) -> None:
        """Refuse a committed marker unless durable rows reconstruct its sealed graph."""
        graph = self._journal_graph(connection, operation_id)
        required = {"inventory", "chat", "messages", "attempts", "attachments", "message_attachment_relations", "attempt_attachment_relations"}
        if not required <= set(graph):
            raise ArchiveImportStoreError("import graph journal lacks full reconstruction evidence")
        chat = connection.exec_driver_sql(
            "SELECT c.id,c.title,c.created_at,c.updated_at,c.head_message_id,c.revision,c.archived_at,"
            "i.source_chat_id,i.source_chat_revision,i.source_archived_at,i.source_node_id,i.source_configuration,i.source_continuation_history "
            "FROM chats AS c JOIN archive_import_chats AS i ON i.chat_id=c.id WHERE i.operation_id=?", (operation_id,)
        ).mappings().one_or_none()
        expected_chat = graph["chat"]
        def same_time(actual: object, expected: object) -> bool:
            if actual is None or expected is None:
                return actual is expected
            return utc_iso(parse_utc(str(actual))) == utc_iso(parse_utc(str(expected)))
        # The live chat is allowed to evolve after the terminal import: a
        # native continuation advances its head/revision and ordinary owner
        # actions may rename or archive it.  Recovery verifies the immutable
        # source snapshot in archive_import_chats below, not those live fields.
        if chat is None:
            raise ArchiveImportStoreError("committed import chat contradicts sealed graph")
        operation = connection.exec_driver_sql(
            "SELECT archive_id,archive_version,logical_content_digest,source_chat_id,source_chat_revision,"
            "source_content_sha256,source_byte_size,imported_at "
            "FROM archive_import_operations WHERE id=?", (operation_id,)
        ).mappings().one_or_none()
        if operation is None:
            raise ArchiveImportStoreError("committed import operation is absent")
        manifest = graph.get("manifest")
        binding = graph.get("source_binding")
        if not isinstance(manifest, dict) or not isinstance(binding, dict):
            raise ArchiveImportStoreError("sealed import operation evidence is malformed")
        try:
            operation_matches_sealed_source = (
                str(operation["archive_id"]) == str(manifest["archive_id"])
                and int(operation["archive_version"]) == int(manifest["archive_version"])
                and str(operation["logical_content_digest"])
                == str(manifest["logical_content_digest"])
                and str(operation["source_chat_id"]) == str(expected_chat["source_id"])
                and int(operation["source_chat_revision"]) == int(expected_chat["revision"])
                and bytes(operation["source_content_sha256"]).hex() == str(binding["sha256"])
                and int(operation["source_byte_size"]) == int(binding["byte_size"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveImportStoreError("sealed import operation evidence is malformed") from exc
        if not operation_matches_sealed_source:
            raise ArchiveImportStoreError("committed import operation contradicts sealed graph")
        try:
            sealed_nodes = {
                (str(row["object_kind"]), str(row["source_id"]), int(row["ordinal"])): str(row["node_id"])
                for row in graph["provenance_node_ids"]
            }
            provenance_rows = graph["object_provenance"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveImportStoreError("sealed provenance node plan is malformed") from exc
        expected_nodes: list[tuple[object, ...]] = []
        receiver_node_ids: dict[tuple[str, str], str] = {}
        for provenance in provenance_rows:
            if not isinstance(provenance, dict):
                raise ArchiveImportStoreError("sealed provenance node plan is malformed")
            kind, source_id = str(provenance["object_kind"]), str(provenance["object_id"])
            source = provenance.get("source")
            if not isinstance(source, dict):
                raise ArchiveImportStoreError("sealed provenance node plan is malformed")
            hops = [] if source.get("immediate") is None else [source["immediate"]]
            hops.extend(source.get("prior_chain", ()))
            prior_id = None
            for ordinal, hop in enumerate(reversed(hops)):
                node_id = sealed_nodes[(kind, source_id, ordinal)]
                expected_nodes.append((node_id, kind, str(hop["archive_id"]), str(hop["logical_content_digest"]), str(hop["object_id"]), str(hop["imported_at"]), int(hop["archive_version"]), prior_id))
                prior_id = node_id
            node_id = sealed_nodes[(kind, source_id, len(hops))]
            receiver_node_ids[(kind, source_id)] = node_id
            expected_nodes.append((node_id, kind, str(operation["archive_id"]), str(operation["logical_content_digest"]), source_id, str(operation["imported_at"]), int(operation["archive_version"]), prior_id))
        actual_nodes = connection.exec_driver_sql(
            "SELECT id,object_kind,archive_id,logical_content_digest,source_object_id,imported_at,source_format,prior_node_id "
            "FROM archive_lineage_nodes WHERE id IN (" + ",".join("?" for _ in expected_nodes) + ")",
            tuple(row[0] for row in expected_nodes),
        ).fetchall() if expected_nodes else []
        if {tuple(row) for row in actual_nodes} != set(expected_nodes):
            raise ArchiveImportStoreError("committed import provenance nodes contradict sealed graph")
        if str(chat["source_node_id"]) != receiver_node_ids[("chat", str(expected_chat["source_id"]))]:
            raise ArchiveImportStoreError("committed import chat node mapping contradicts sealed graph")
        expected_head = expected_chat.get("head_message_id")
        if expected_head is not None:
            # The durable value is local; only the mapping can prove its source identity.
            mapped_head = connection.exec_driver_sql(
                "SELECT source_message_id FROM archive_import_messages WHERE message_id=?", (chat["head_message_id"],)
            ).scalar_one_or_none()
            # A later native continuation legitimately owns the live head;
            # only an imported head must still map to the sealed source head.
            if mapped_head is not None and str(mapped_head) != str(expected_head):
                raise ArchiveImportStoreError("committed import chat head contradicts sealed graph")
        expected_configuration = graph.get("chat_configuration", {})
        expected_history = graph.get("continuation_history") or {}
        try:
            configuration = json.loads(str(chat["source_configuration"]))
            history = json.loads(str(chat["source_continuation_history"]))
        except ValueError as exc:
            raise ArchiveImportStoreError("committed import chat evidence is malformed") from exc
        if (str(chat["source_chat_id"]) != str(expected_chat["source_id"])
                or int(chat["source_chat_revision"]) != int(expected_chat["revision"])
                or not same_time(chat["source_archived_at"], expected_chat.get("archived_at"))
                or configuration != expected_configuration or history != expected_history):
            raise ArchiveImportStoreError("committed import chat metadata contradicts sealed graph")
        expected_branches = expected_history.get("branches", []) if isinstance(expected_history, dict) else []
        if not isinstance(expected_branches, list):
            raise ArchiveImportStoreError("sealed import branch history is malformed")
        imported_branches = connection.exec_driver_sql(
            "SELECT b.base_key,b.choice_snapshot,first_source.source_message_id AS first_source_id,"
            "base_source.source_message_id AS base_source_id,a.source_attempt_id "
            "FROM archive_imported_branch_choices b "
            "JOIN archive_import_messages first_source ON first_source.message_id=b.first_message_id AND first_source.chat_id=b.chat_id "
            "LEFT JOIN archive_import_messages base_source ON base_source.message_id=b.base_key AND base_source.chat_id=b.chat_id "
            "JOIN archive_imported_attempts a ON a.id=b.attempt_id AND a.chat_id=b.chat_id "
            "WHERE b.chat_id=?", (chat["id"],)
        ).mappings().all()
        if len(imported_branches) != len(expected_branches):
            raise ArchiveImportStoreError("committed import branch history count contradicts sealed graph")
        expected_branch_snapshots = {
            canonical_json_bytes(item).decode("utf-8").removesuffix("\n") for item in expected_branches
        }
        observed_branch_snapshots: set[str] = set()
        for branch in imported_branches:
            try:
                snapshot = json.loads(str(branch["choice_snapshot"]))
            except ValueError as exc:
                raise ArchiveImportStoreError("committed import branch snapshot is malformed") from exc
            canonical_snapshot = canonical_json_bytes(snapshot).decode("utf-8").removesuffix("\n")
            expected_base = "empty" if str(branch["base_key"]) == "empty" else branch["base_source_id"]
            if (
                canonical_snapshot not in expected_branch_snapshots
                or canonical_snapshot != str(branch["choice_snapshot"])
                or snapshot.get("anchor_key") != expected_base
                or snapshot.get("first_message_id") != str(branch["first_source_id"])
                or snapshot.get("attempt_id") != str(branch["source_attempt_id"])
            ):
                raise ArchiveImportStoreError("committed import branch history contradicts sealed graph")
            observed_branch_snapshots.add(canonical_snapshot)
        if observed_branch_snapshots != expected_branch_snapshots:
            raise ArchiveImportStoreError("committed import branch history contradicts sealed graph")
        expected_messages = {str(item["source_id"]): item for item in graph["messages"]}
        rows = connection.exec_driver_sql(
            "SELECT i.source_message_id,i.source_lineage_id,i.source_node_id,m.parent_id,m.role,m.state,m.content,m.sequence,m.created_at,m.lineage_id,m.revision,m.supersedes_id "
            "FROM archive_import_messages AS i JOIN messages AS m ON m.id=i.message_id "
        "WHERE i.chat_id=?", (chat["id"],)
        ).mappings().all()
        if len(rows) != len(expected_messages):
            raise ArchiveImportStoreError("committed import message count contradicts sealed graph")
        for row in rows:
            expected = expected_messages.get(str(row["source_message_id"]))
            if (expected is None
                    or str(row["source_node_id"]) != receiver_node_ids[("message", str(row["source_message_id"]))]
                    or str(row["lineage_id"]) != next(item["local_id"] for item in graph["inventory"] if item["object_kind"] == "lineage" and item["source_id"] == str(expected["lineage_id"]))
                    or any(str(row[key]) != str(expected[key]) for key in ("role", "state", "content", "sequence", "revision"))
                    or not same_time(row["created_at"], expected["created_at"])):
                raise ArchiveImportStoreError("committed import message contradicts sealed graph")
            for source_key, local_key in (("parent_id", "parent_id"), ("supersedes_id", "supersedes_id")):
                actual = row[local_key]
                if expected.get(source_key) is None:
                    if actual is not None:
                        raise ArchiveImportStoreError("committed import message relation contradicts sealed graph")
                else:
                    mapped = connection.exec_driver_sql(
                        "SELECT source_message_id FROM archive_import_messages WHERE message_id=?", (actual,)
                    ).scalar_one_or_none()
                    if str(mapped) != str(expected[source_key]):
                        raise ArchiveImportStoreError("committed import message relation contradicts sealed graph")
            if str(row["source_lineage_id"]) != str(expected["lineage_id"]):
                raise ArchiveImportStoreError("committed import message lineage contradicts sealed graph")
        expected_attempts = {str(item["source_id"]): item for item in graph["attempts"]}
        attempts = connection.exec_driver_sql(
            "SELECT source_attempt_id,state,started_at,ended_at,source_attempt,source_evidence_binding FROM archive_imported_attempts WHERE chat_id=?", (chat["id"],)
        ).mappings().all()
        if len(attempts) != len(expected_attempts):
            raise ArchiveImportStoreError("committed import attempt count contradicts sealed graph")
        for row in attempts:
            expected = expected_attempts.get(str(row["source_attempt_id"]))
            if expected is None:
                raise ArchiveImportStoreError("committed import attempt contradicts sealed graph")
            try:
                stored = json.loads(str(row["source_attempt"]))
            except ValueError as exc:
                raise ArchiveImportStoreError("committed import attempt evidence is malformed") from exc
            source_attempt_id = str(row["source_attempt_id"])
            expected_binding = [
                binding_row
                for binding_row in graph.get("history_bindings", [])
                if isinstance(binding_row, dict)
                and binding_row.get("attempt_id") == source_attempt_id
            ]
            try:
                binding = json.loads(str(row["source_evidence_binding"]))
            except ValueError as exc:
                raise ArchiveImportStoreError("committed import attempt binding is malformed") from exc
            state = "incomplete" if str(expected["state"]) == "truncated" else str(expected["state"])
            if (stored != expected or binding != expected_binding
                    or str(row["state"]) != state
                    or not same_time(row["started_at"], expected["started_at"])
                    or not same_time(row["ended_at"], expected["ended_at"])):
                raise ArchiveImportStoreError("committed import attempt contradicts sealed graph")
        expected_attachments = {str(item["source_id"]): item for item in graph["attachments"]}
        attachments = connection.exec_driver_sql(
            "SELECT source_attachment_id,expected_digest,expected_size,source_metadata,availability,attachment_id "
            "FROM archive_import_attachment_refs WHERE operation_id=?", (operation_id,)
        ).mappings().all()
        if len(attachments) != len(expected_attachments):
            raise ArchiveImportStoreError("committed import attachment count contradicts sealed graph")
        for row in attachments:
            expected = expected_attachments.get(str(row["source_attachment_id"]))
            try:
                metadata = json.loads(str(row["source_metadata"]))
            except ValueError as exc:
                raise ArchiveImportStoreError("committed import attachment metadata is malformed") from exc
            if (expected is None or metadata != expected
                    or bytes(row["expected_digest"]).hex() != str(expected["blob_digest"])
                    or int(row["expected_size"]) != int(expected["byte_size"])
                    or str(row["availability"]) not in {"READY", "MISSING_EXTERNAL"}
                    or ((row["attachment_id"] is None) != (str(row["availability"]) == "MISSING_EXTERNAL"))):
                raise ArchiveImportStoreError("committed import attachment contradicts sealed graph")
        expected_message_links = {
            (str(row["message_id"]), str(row["attachment_id"]), int(row["ordinal"]))
            for row in graph["message_attachment_relations"]
        }
        expected_attempt_links = {
            (str(row["attempt_id"]), str(row["attachment_id"]), int(row["ordinal"]))
            for row in graph["attempt_attachment_relations"]
        }
        message_links = connection.exec_driver_sql(
            "SELECT m.source_message_id,r.source_attachment_id,l.ordinal "
            "FROM archive_import_message_attachment_refs AS l "
            "JOIN archive_import_messages AS m ON m.message_id=l.message_id "
            "JOIN archive_import_attachment_refs AS r ON r.id=l.attachment_ref_id WHERE m.chat_id=?", (chat["id"],)
        ).fetchall()
        attempt_links = connection.exec_driver_sql(
            "SELECT a.source_attempt_id,r.source_attachment_id,l.ordinal "
            "FROM archive_import_attempt_attachment_refs AS l "
            "JOIN archive_imported_attempts AS a ON a.id=l.attempt_id "
            "JOIN archive_import_attachment_refs AS r ON r.id=l.attachment_ref_id WHERE a.chat_id=?", (chat["id"],)
        ).fetchall()
        if {tuple((str(row[0]), str(row[1]), int(row[2]))) for row in message_links} != expected_message_links:
            raise ArchiveImportStoreError("committed import message attachment links contradict sealed graph")
        if {tuple((str(row[0]), str(row[1]), int(row[2]))) for row in attempt_links} != expected_attempt_links:
            raise ArchiveImportStoreError("committed import attempt attachment links contradict sealed graph")
        expected_contexts = {str(item["attempt_id"]): item for item in graph.get("context_plans", [])}
        contexts = connection.exec_driver_sql(
            "SELECT a.source_attempt_id,c.source_plan,c.source_plan_digest,c.local_bindings "
            "FROM archive_imported_context_plans AS c JOIN archive_imported_attempts AS a ON a.id=c.attempt_id WHERE a.chat_id=?", (chat["id"],)
        ).mappings().all()
        if len(contexts) != len(expected_contexts):
            raise ArchiveImportStoreError("committed import context count contradicts sealed graph")
        for row in contexts:
            expected = expected_contexts.get(str(row["source_attempt_id"]))
            try:
                source_plan = json.loads(str(row["source_plan"]))
                bindings = json.loads(str(row["local_bindings"]))
            except ValueError as exc:
                raise ArchiveImportStoreError("committed import context evidence is malformed") from exc
            if (expected is None or source_plan != expected
                    or str(row["source_plan_digest"]) != hashlib.sha256(canonical_json_bytes(expected)).hexdigest()
                    or str(bindings.get("source_attempt_id")) != str(row["source_attempt_id"])):
                raise ArchiveImportStoreError("committed import context contradicts sealed graph")
        # Only derivations whose object is part of this imported operation are
        # immutable import facts.  Later native continuations share the chat but
        # must remain admissible after a legitimate reopen.
        expected_derivations = {
            (
                str(row["object_kind"]),
                str(row["object_id"]),
                str(row["derivation"]["predecessor"]["object_id"]),
            )
            for row in graph.get("object_provenance", [])
            if isinstance(row, dict)
            and row.get("object_kind") in {"message", "attempt"}
            and isinstance(row.get("derivation"), dict)
            and row["derivation"].get("kind") == "local-continuation"
            and isinstance(row["derivation"].get("predecessor"), dict)
        }
        imported_derivations = connection.exec_driver_sql(
            "SELECT d.object_kind,"
            "CASE d.object_kind WHEN 'message' THEN im.source_message_id "
            "WHEN 'attempt' THEN ia.source_attempt_id END AS source_object_id,"
            "pm.source_message_id AS predecessor_source_id "
            "FROM archive_object_derivations d "
            "LEFT JOIN archive_import_messages im ON d.object_kind='message' AND im.message_id=d.object_id "
            "LEFT JOIN archive_imported_attempts ia ON d.object_kind='attempt' AND ia.id=d.object_id "
            "JOIN archive_import_messages pm ON pm.message_id=d.predecessor_message_id "
            "WHERE d.chat_id=? AND (im.chat_id=? OR ia.chat_id=?)",
            (chat["id"], chat["id"], chat["id"]),
        ).fetchall()
        observed_derivations = {
            (str(kind), str(source_object_id), str(predecessor_source_id))
            for kind, source_object_id, predecessor_source_id in imported_derivations
        }
        if observed_derivations != expected_derivations:
            raise ArchiveImportStoreError("committed import derivations contradict sealed graph")

        # These rows are the immutable imported continuation projection.  A
        # later native branch may add anchors, choices, requirements, and
        # candidates in the same chat, so reconstruct only the rows owned by
        # the sealed imported bases and imported attempts.
        inventory = {
            (str(row["object_kind"]), str(row["source_id"])): str(row["local_id"])
            for row in graph["inventory"]
            if isinstance(row, dict)
        }
        history = graph.get("continuation_history") or {}
        if not isinstance(history, dict):
            raise ArchiveImportStoreError("sealed continuation history is malformed")
        anchors = history.get("anchors", [])
        if not isinstance(anchors, list):
            raise ArchiveImportStoreError("sealed continuation anchors are malformed")
        source_bases = ["empty"] + [
            str(row["source_id"])
            for row in graph["messages"]
            if isinstance(row, dict)
            and str(row.get("role")) == "assistant"
            and str(row.get("state")) in {"complete", "incomplete", "failed", "aborted", "truncated"}
        ]
        expected_anchor_rows: set[tuple[object, ...]] = set()
        for source_base in dict.fromkeys(source_bases):
            if source_base != "empty" and ("message", source_base) not in inventory:
                raise ArchiveImportStoreError("sealed continuation anchor base is malformed")
            source_anchor = next(
                (row for row in anchors if isinstance(row, dict) and row.get("anchor_key") == source_base),
                {},
            )
            configuration = source_anchor.get("source_configuration", expected_configuration)
            if not isinstance(configuration, dict):
                raise ArchiveImportStoreError("sealed continuation anchor configuration is malformed")
            local_base = None if source_base == "empty" else inventory[("message", source_base)]
            expected_anchor_rows.add((
                "empty" if local_base is None else local_base, local_base, 1,
                json.dumps(configuration, sort_keys=True, separators=(",", ":")),
            ))
        actual_anchor_rows = connection.exec_driver_sql(
            "SELECT base_key,base_message_id,revision,source_configuration "
            "FROM archive_continuation_anchors WHERE chat_id=?",
            (chat["id"],),
        ).fetchall()
        actual_imported_anchors = {
            (str(base_key), None if base_message_id is None else str(base_message_id), int(revision), str(configuration))
            for base_key, base_message_id, revision, configuration in actual_anchor_rows
            if str(base_key) in {row[0] for row in expected_anchor_rows}
        }
        if actual_imported_anchors != expected_anchor_rows:
            raise ArchiveImportStoreError("committed import continuation anchors contradict sealed graph")

        expected_source_choices: set[tuple[object, ...]] = set()
        choices = history.get("choices", [])
        if not isinstance(choices, list):
            raise ArchiveImportStoreError("sealed continuation choices are malformed")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ArchiveImportStoreError("sealed continuation choice is malformed")
            source_base = choice.get("anchor_key")
            revision = choice.get("choice_revision")
            settings = choice.get("explicit_settings")
            exclusions = choice.get("excluded_context_refs")
            descriptor = choice.get("mapped_model")
            if (
                not isinstance(source_base, str) or type(revision) is not int
                or not isinstance(settings, dict) or not isinstance(exclusions, list)
                or not isinstance(descriptor, dict)
            ):
                raise ArchiveImportStoreError("sealed continuation choice is malformed")
            local_base = "empty" if source_base == "empty" else inventory.get(("message", source_base))
            if local_base is None:
                raise ArchiveImportStoreError("sealed continuation choice base is malformed")
            local_exclusions: list[dict[str, object]] = []
            for exclusion in exclusions:
                if not isinstance(exclusion, dict):
                    raise ArchiveImportStoreError("sealed continuation exclusion is malformed")
                local_exclusion = dict(exclusion)
                attachment_id = local_exclusion.get("attachment_id")
                if attachment_id is not None:
                    if not isinstance(attachment_id, str) or ("attachment", attachment_id) not in inventory:
                        raise ArchiveImportStoreError("sealed continuation exclusion is malformed")
                    local_exclusion["attachment_id"] = inventory[("attachment", attachment_id)]
                local_exclusions.append(local_exclusion)
            decision = choice.get("decision_kind")
            if decision == "operator-resolution":
                decision = "operator_resolution"
            if decision not in {"equivalent", "operator_resolution"}:
                raise ArchiveImportStoreError("sealed continuation choice is malformed")
            try:
                chosen_at = utc_iso(parse_utc(str(choice["chosen_at"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveImportStoreError("sealed continuation choice is malformed") from exc
            expected_source_choices.add((
                local_base, revision,
                json.dumps(settings, sort_keys=True, separators=(",", ":")),
                1 if local_exclusions else 0,
                json.dumps(local_exclusions, separators=(",", ":")), str(decision),
                json.dumps(descriptor, sort_keys=True, separators=(",", ":")), chosen_at,
            ))
        actual_source_choices = {
            (str(base_key), int(revision), str(settings), int(degraded), str(exclusions), str(decision), str(descriptor), str(created_at))
            for base_key, revision, settings, degraded, exclusions, decision, descriptor, created_at in connection.exec_driver_sql(
                "SELECT base_key,choice_revision,explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at "
                "FROM archive_continuation_choices WHERE chat_id=? "
                "AND local_connection_id IS NULL AND local_model_entry_id IS NULL",
                (chat["id"],),
            ).fetchall()
        }
        if actual_source_choices != expected_source_choices:
            raise ArchiveImportStoreError("committed import continuation choices contradict sealed graph")

        # A receiver-local automatic equivalent is not source provenance, but
        # it was admitted from the pre-cutoff receiver snapshot and is sealed
        # in this journal.  Compare that immutable initial row directly;
        # later operator choices may append higher revisions without causing
        # recovery to reinterpret the old target through today's catalogue.
        initial_receiver = graph.get("initial_receiver_continuation", [])
        if not isinstance(initial_receiver, list):
            raise ArchiveImportStoreError("sealed receiver continuation is malformed")
        expected_receiver_choices: set[tuple[object, ...]] = set()
        expected_receiver_anchors: set[tuple[object, ...]] = set()
        for item in initial_receiver:
            if not isinstance(item, dict):
                raise ArchiveImportStoreError("sealed receiver continuation is malformed")
            try:
                base_key = str(item["base_key"])
                connection_id = str(item["connection_id"])
                model_id = str(item["model_id"])
                settings = item["explicit_settings"]
                descriptor = item["safe_target_descriptor"]
                resolution = str(item["resolution"])
                evidence = item["resolution_evidence"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveImportStoreError("sealed receiver continuation is malformed") from exc
            if (not isinstance(settings, dict) or not isinstance(descriptor, dict)
                    or resolution != "EQUIVALENT" or not isinstance(evidence, dict)):
                raise ArchiveImportStoreError("sealed receiver continuation is malformed")
            expected_receiver_choices.add((
                base_key, 1, connection_id, model_id,
                json.dumps(settings, sort_keys=True, separators=(",", ":")),
                0, "[]", "equivalent",
                json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
                str(operation["imported_at"]),
            ))
            expected_receiver_anchors.add((
                base_key, resolution,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            ))
        receiver_keys = tuple(sorted(item[0] for item in expected_receiver_choices))
        if receiver_keys:
            placeholders = ",".join("?" for _ in receiver_keys)
            receiver_rows = connection.exec_driver_sql(
                "SELECT base_key,choice_revision,local_connection_id,local_model_entry_id,explicit_settings,"
                "degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at "
                "FROM archive_continuation_choices WHERE chat_id=? AND base_key IN (" + placeholders + ") "
                "AND choice_revision=1",
                (chat["id"], *receiver_keys),
            ).fetchall()
            actual_receiver_choices = {
                (str(base), int(revision), str(connection_id), str(model_id), str(settings), int(degraded),
                 str(exclusions), str(decision), str(descriptor), str(created_at))
                for base, revision, connection_id, model_id, settings, degraded, exclusions, decision, descriptor, created_at
                in receiver_rows
            }
            anchor_rows = connection.exec_driver_sql(
                "SELECT base_key,resolution,resolution_evidence FROM archive_continuation_anchors "
                "WHERE chat_id=? AND base_key IN (" + placeholders + ")",
                (chat["id"], *receiver_keys),
            ).fetchall()
            actual_receiver_anchors = {
                (str(base), str(resolution), str(evidence))
                for base, resolution, evidence in anchor_rows
            }
            if actual_receiver_choices != expected_receiver_choices:
                raise ArchiveImportStoreError("initial receiver continuation choices contradict sealed graph")
            if actual_receiver_anchors != expected_receiver_anchors:
                raise ArchiveImportStoreError("initial receiver continuation anchors contradict sealed graph")

        attachment_by_source = {
            str(row["source_id"]): row for row in graph["attachments"] if isinstance(row, dict)
        }
        selected_by_attempt: dict[str, list[dict[str, object]]] = {}
        for relation in graph["attempt_attachment_relations"]:
            if not isinstance(relation, dict):
                raise ArchiveImportStoreError("sealed continuation relation is malformed")
            selected_by_attempt.setdefault(str(relation["attempt_id"]), []).append(relation)
        bindings_by_attempt: dict[str, list[dict[str, object]]] = {}
        for binding in graph.get("history_bindings", []):
            if not isinstance(binding, dict) or binding.get("binding_kind") != "context-source":
                continue
            snapshot, current = binding.get("snapshot_source"), binding.get("current_binding")
            if (
                not isinstance(snapshot, dict) or snapshot.get("kind") != "attachment"
                or not isinstance(binding.get("attempt_id"), str) or type(binding.get("ordinal")) is not int
                or (current is not None and (not isinstance(current, dict) or current.get("object_kind") != "attachment"))
            ):
                continue
            bindings_by_attempt.setdefault(str(binding["attempt_id"]), []).append(binding)
        expected_requirements: set[tuple[object, ...]] = set()
        expected_candidates: set[tuple[object, ...]] = set()
        terminal_attempts = {
            str(row["source_id"]): str(row["assistant_message_id"])
            for row in graph["attempts"] if isinstance(row, dict)
            and str(row.get("state")) in {"complete", "incomplete", "failed", "aborted", "truncated"}
        }
        for source_attempt, source_base in terminal_attempts.items():
            local_base = inventory.get(("message", source_base))
            local_attempt = inventory.get(("attempt", source_attempt))
            if local_base is None or local_attempt is None:
                raise ArchiveImportStoreError("sealed continuation requirement owner is malformed")
            selected = sorted(selected_by_attempt.get(source_attempt, []), key=lambda row: int(row["ordinal"]))
            bindings = sorted(bindings_by_attempt.get(source_attempt, []), key=lambda row: int(row["ordinal"]))
            if selected and not bindings:
                continue
            for binding in bindings:
                snapshot = binding["snapshot_source"]
                evidence_digest = str(snapshot.get("evidence_digest"))
                source_status = snapshot.get("source_id_status")
                ordinal = int(binding["ordinal"])
                if source_status == "available" and binding.get("binding_state") == "bound":
                    source_attachment = str(snapshot.get("source_id"))
                    current = binding.get("current_binding")
                    if not isinstance(current, dict) or str(current.get("object_id")) != source_attachment:
                        continue
                    candidates = [row for row in selected if str(row["attachment_id"]) == source_attachment]
                    binding_kind = "identity"
                elif (
                    binding.get("binding_state") in {"unavailable", "historical-only"}
                    and binding.get("current_binding") is None
                ):
                    candidates = [
                        row for row in selected
                        if str(attachment_by_source.get(str(row["attachment_id"]), {}).get("text_digest")) == evidence_digest
                        and attachment_by_source.get(str(row["attachment_id"]), {}).get("text_eligibility") == "eligible"
                    ]
                    binding_kind = "digest"
                else:
                    continue
                if not candidates:
                    continue
                candidate_rows = [attachment_by_source.get(str(row["attachment_id"])) for row in candidates]
                if any(row is None for row in candidate_rows):
                    raise ArchiveImportStoreError("sealed continuation candidate is malformed")
                digests = {str(row["blob_digest"]) for row in candidate_rows}
                sizes = {int(row["byte_size"]) for row in candidate_rows}
                if len(digests) != 1 or len(sizes) != 1:
                    continue
                digest, size = digests.pop(), sizes.pop()
                bound_ref = inventory[("attachment", str(candidates[0]["attachment_id"]))] if binding_kind == "identity" else None
                expected_requirements.add((
                    local_base, ordinal, local_attempt, bytes.fromhex(digest), size,
                    bytes.fromhex(evidence_digest), binding_kind, bound_ref,
                ))
                expected_candidates.update((
                    local_base, ordinal, inventory[("attachment", str(candidate["attachment_id"]))],
                    inventory[("attachment", str(candidate["attachment_id"]))],
                ) for candidate in candidates)
        actual_requirements = {
            (str(base_key), int(ordinal), str(source_attempt), bytes(expected_digest), int(expected_size),
             bytes(representation_digest), str(binding_kind), None if bound_ref is None else str(bound_ref))
            for base_key, ordinal, source_attempt, expected_digest, expected_size, representation_digest, binding_kind, bound_ref in connection.exec_driver_sql(
                "SELECT r.base_key,r.ordinal,r.source_imported_attempt_id,r.expected_digest,r.expected_size,"
                "r.representation_digest,r.binding_kind,r.bound_imported_ref_id "
                "FROM archive_continuation_requirements r JOIN archive_imported_attempts a ON a.id=r.source_imported_attempt_id "
                "WHERE a.chat_id=?", (chat["id"],),
            ).fetchall()
        }
        if actual_requirements != expected_requirements:
            raise ArchiveImportStoreError("committed import continuation requirements contradict sealed graph")
        actual_candidates = {
            (str(base_key), int(ordinal), str(candidate_id), str(imported_ref_id))
            for base_key, ordinal, candidate_id, imported_ref_id in connection.exec_driver_sql(
                "SELECT c.base_key,c.ordinal,c.candidate_attachment_id,c.imported_ref_id "
                "FROM archive_continuation_requirement_candidates c "
                "JOIN archive_continuation_requirements r ON r.chat_id=c.chat_id AND r.base_key=c.base_key AND r.ordinal=c.ordinal "
                "JOIN archive_imported_attempts a ON a.id=r.source_imported_attempt_id WHERE a.chat_id=?",
                (chat["id"],),
            ).fetchall()
        }
        if actual_candidates != expected_candidates:
            raise ArchiveImportStoreError("committed import continuation candidates contradict sealed graph")

    def _phase9_fake_candidate_is_runnable(
        self,
        model_entry_id: str,
        explicit_settings: GenerationSettings,
    ) -> bool:
        """Apply the native Phase 5 admission rules before auto-selecting a fake.

        Archive import only has enough authority to choose a deterministic fake
        target.  That choice must nevertheless survive the same capability and
        effective-settings checks that a later continuation uses.
        """
        model = self.get_model_catalogue_entry(model_entry_id)
        if model is None:
            return False
        provider = self.get_provider_connection(model.connection_id)
        if provider is None:
            return False
        facts = tuple(
            fact for fact in self.list_capability_facts(model.id)
            if not (
                fact.source in {
                    CapabilitySource.CONFIRMED_ENDPOINT,
                    CapabilitySource.PROVIDER_METADATA,
                }
                and fact.source_revision is not None
                and fact.source_revision != provider.catalogue_revision
            )
        )
        overrides = {item.key: item for item in self.list_capability_overrides(model.id)}
        capabilities = {
            key: resolve_capability(facts, overrides.get(key), key)
            for key in CAPABILITY_KEYS
        }
        application_settings = self.get_application_generation_settings()
        model_settings = self.get_model_generation_settings(model.id)
        values = {
            key: (
                getattr(explicit_settings, key)
                if getattr(explicit_settings, key) is not None
                else getattr(model_settings, key)
                if model_settings is not None and getattr(model_settings, key) is not None
                else getattr(application_settings, key)
            )
            for key in ("temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds")
        }
        try:
            resolved = GenerationSettings(**values)
            _validate_settings(resolved)
        except (TypeError, ValueError, StateError):
            return False
        for key in (
            CapabilityKey.STREAMING.value,
            CapabilityKey.TEMPERATURE.value,
            CapabilityKey.MAX_OUTPUT_TOKENS.value,
        ):
            if capabilities[key].state is not CapabilityState.SUPPORTED:
                return False
        if resolved.reasoning_effort is not None and (
            capabilities[CapabilityKey.REASONING_NONE.value].state
            is not CapabilityState.SUPPORTED
        ):
            return False
        output_limit = capabilities[CapabilityKey.OUTPUT_TOKENS.value]
        request_limit = capabilities[CapabilityKey.MAX_OUTPUT_TOKENS.value]
        if (
            output_limit.state is CapabilityState.SUPPORTED
            and output_limit.value is not None
            and resolved.max_output_tokens > output_limit.value
        ):
            return False
        if (
            (output_limit.state is not CapabilityState.SUPPORTED or output_limit.value is None)
            and request_limit.value is not None
            and resolved.max_output_tokens > request_limit.value
        ):
            return False
        return True

    def _plan_initial_receiver_continuation(self, plan) -> tuple[dict[str, object], ...]:
        """Freeze deterministic fake equivalence before the import cutoff.

        This deliberately sees only the validated portable source
        configuration and receiver-safe catalogue facts.  It records no
        provider credentials and does not create provider rows.  The result
        is a receiver-local internal plan, not archive provenance.
        """
        history = plan.continuation_history or {}
        anchor_rows = history.get("anchors", []) if isinstance(history, dict) else []
        automatic_anchors = (
            anchor_rows if isinstance(anchor_rows, list) and anchor_rows
            else [{
                "anchor_key": plan.chat.get("head_message_id") or "empty",
                "source_configuration": plan.chat_configuration,
            }]
        )
        source_choices = history.get("choices", []) if isinstance(history, dict) else []
        rows: list[dict[str, object]] = []
        with self._engine.connect() as connection:
            for anchor in automatic_anchors:
                if not isinstance(anchor, dict):
                    continue
                source_key = anchor.get("anchor_key")
                configuration = anchor.get("source_configuration")
                selection = configuration.get("selection") if isinstance(configuration, dict) else None
                source_model = selection.get("model") if isinstance(selection, dict) else None
                overrides = configuration.get("overrides") if isinstance(configuration, dict) else None
                if not isinstance(source_key, str) or not isinstance(source_model, dict) or not isinstance(overrides, list):
                    continue
                required = {
                    "source_model_entry_id", "source_model_entry_id_status",
                    "source_connection_id", "source_connection_id_status",
                    "backend_type", "backend_type_status", "provider_profile",
                    "provider_profile_status", "provider_model_id",
                    "provider_model_id_status", "display_name", "display_name_status",
                    "origin", "origin_status", "availability", "availability_status",
                    "model_revision", "connection_revision", "catalogue_revision",
                }
                capabilities_recorded = "capabilities" in source_model
                optional_capabilities = source_model.get("capabilities", [])
                if (
                    (set(source_model) != required and set(source_model) != required | {"capabilities"})
                    or source_model.get("backend_type") != "fake"
                    or source_model.get("provider_profile") != "generic"
                    or source_model.get("availability") != "available"
                    or any(source_model.get(f"{key}_status") != "available" for key in (
                        "backend_type", "provider_profile", "provider_model_id", "availability",
                    ))
                    or not isinstance(optional_capabilities, list)
                    or any(isinstance(item, dict) and item.get("anchor_key") == source_key for item in source_choices)
                ):
                    continue
                matching_overrides = [
                    row for row in overrides
                    if isinstance(row, dict)
                    and set(row) == {"model", "revision", "temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}
                    and row["model"] == source_model and type(row["revision"]) is int and row["revision"] >= 1
                ]
                if len(matching_overrides) > 1:
                    continue
                settings = (
                    {key: matching_overrides[0][key] for key in ("temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds")}
                    if matching_overrides else
                    {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
                )
                try:
                    requested_settings = GenerationSettings(**settings)
                    _validate_settings(requested_settings)
                except (TypeError, ValueError):
                    continue
                source_caps = {
                    (item.get("key"), item.get("state"), item.get("value"))
                    for item in optional_capabilities
                    if isinstance(item, dict) and set(item) == {"key", "state", "value"}
                }
                if len(source_caps) != len(optional_capabilities):
                    continue
                candidates = connection.exec_driver_sql(
                    "SELECT m.id AS model_id,p.id AS connection_id FROM model_catalogue_entries m "
                    "JOIN provider_connections p ON p.id=m.connection_id "
                    "WHERE p.backend_type='fake' AND p.profile='generic' AND p.enabled=1 AND p.retired=0 "
                    "AND m.availability='available' AND m.provider_model_id=? ORDER BY p.id,m.id",
                    (source_model["provider_model_id"],),
                ).mappings().all()
                chosen = None
                for candidate in candidates:
                    actual_caps = {
                        (str(item["capability_key"]), str(item["state"]), item["value"])
                        for item in connection.exec_driver_sql(
                            "SELECT capability_key,state,value FROM capability_facts WHERE model_entry_id=?",
                            (candidate["model_id"],),
                        ).mappings().all()
                    }
                    if (self._phase9_fake_candidate_is_runnable(str(candidate["model_id"]), requested_settings)
                            and (not capabilities_recorded or actual_caps == source_caps)):
                        chosen = candidate
                        break
                if chosen is None:
                    continue
                local_key = "empty" if source_key == "empty" else plan.local_id("message", source_key)
                descriptor = {
                    "backend_type": "fake", "provider_profile": "generic",
                    "provider_model_id": source_model["provider_model_id"],
                    "availability": "available", "capabilities": optional_capabilities,
                }
                rows.append({
                    "base_key": local_key,
                    "connection_id": str(chosen["connection_id"]),
                    "model_id": str(chosen["model_id"]),
                    "explicit_settings": settings,
                    "safe_target_descriptor": descriptor,
                    "resolution": "EQUIVALENT",
                    "resolution_evidence": {"kind": "deterministic-fake-equivalence"},
                })
        return tuple(rows)

    def _publish_import_graph(
        self, connection, captured, operation_id: str, timestamp: str, *, import_as_archived: bool = False,
    ) -> int:
        """Insert an already validated, payload-free canonical graph atomically."""
        plan = captured.plan
        def canonical_time(value: object) -> str:
            return utc_iso(parse_utc(str(value)))
        identities = tuple(
            (entry.object_kind, entry.local_id)
            for entry in plan.identities
            if entry.object_kind in {"chat", "message", "attachment"}
        )
        # Messages and the head use dedicated 0012 current-schema admission;
        # Phase 7 remains independently armed and verifies exactly one source
        # revision at the end of the complete transaction.
        chat_id = plan.local_id("chat", captured.source_chat_id)
        provenance_by_identity = {
            (str(row["object_kind"]), str(row["object_id"])): row
            for row in plan.object_provenance
        }
        sealed_node_ids = {
            (str(row["object_kind"]), str(row["source_id"]), int(row["ordinal"])): str(row["node_id"])
            for row in plan.provenance_node_ids
        }
        # Node IDs and their predecessor links are sealed before the graph arm.
        # The insert trigger consumes these complete rows; no later UUID choice
        # can cross-link a valid planned provenance node.
        node_grants: list[tuple[object, ...]] = []
        node_ids: dict[tuple[str, str], str] = {}
        for identity in plan.identities:
            row = provenance_by_identity[(identity.object_kind, identity.source_id)]
            source = row["source"]
            hops = []
            if source.get("immediate") is not None:
                hops.append(source["immediate"])
            hops.extend(source.get("prior_chain", ()))
            prior_id = None
            for ordinal, hop in enumerate(reversed(hops)):
                node_id = sealed_node_ids[(identity.object_kind, identity.source_id, ordinal)]
                node_grants.append((
                    node_id, identity.object_kind, str(hop["archive_id"]), str(hop["logical_content_digest"]),
                    str(hop["object_id"]), str(hop["imported_at"]), int(hop["archive_version"]), prior_id,
                ))
                prior_id = node_id
            node_id = sealed_node_ids[(identity.object_kind, identity.source_id, len(hops))]
            node_grants.append((
                node_id, identity.object_kind, captured.archive_id, captured.logical_content_digest,
                identity.source_id, timestamp, captured.archive_version, prior_id,
            ))
            node_ids[(identity.object_kind, identity.source_id)] = node_id
        message_grants = tuple(
            (
                plan.local_id("message", str(item["source_id"])), chat_id,
                None if item.get("parent_id") is None else plan.local_id("message", str(item["parent_id"])),
                int(item["sequence"]), str(item["role"]), str(item["state"]), str(item["content"]),
                canonical_time(item["created_at"]), plan.local_id("lineage", str(item["lineage_id"])),
                int(item["revision"]), None if item.get("supersedes_id") is None else plan.local_id("message", str(item["supersedes_id"])),
            )
            for item in plan.messages
        )
        head = plan.chat.get("head_message_id")
        initial_archived_at = timestamp if import_as_archived else None
        chat_grants = (
            (chat_id, str(plan.chat["title"]), canonical_time(plan.chat["created_at"]), canonical_time(plan.chat["updated_at"]), None, 0, initial_archived_at),
            (chat_id, str(plan.chat["title"]), canonical_time(plan.chat["created_at"]), canonical_time(plan.chat["updated_at"]), None if head is None else plan.local_id("message", str(head)), 1, initial_archived_at),
        )
        link_grants = tuple(
            ("message-attachment", plan.local_id("message", str(row["message_id"])),
             plan.local_id("attachment", str(row["attachment_id"])), int(row["ordinal"]))
            for row in plan.message_attachment_relations
        ) + tuple(
            ("attempt-attachment", plan.local_id("attempt", str(row["attempt_id"])),
             plan.local_id("attachment", str(row["attachment_id"])), int(row["ordinal"]))
            for row in plan.attempt_attachment_relations
        )
        attempt_grants = tuple(
            (
                plan.local_id("attempt", str(item["source_id"])), chat_id,
                plan.local_id("message", str(item["user_message_id"])),
                plan.local_id("message", str(item["assistant_message_id"])), str(item["source_id"]),
                node_ids[("attempt", str(item["source_id"]))],
                "incomplete" if str(item["state"]) == "truncated" else str(item["state"]),
                canonical_time(item["started_at"]), canonical_time(item["ended_at"]),
                json.dumps(item, sort_keys=True, separators=(",", ":")),
                json.dumps(
                    [row for row in plan.history_bindings if row.get("attempt_id") == str(item["source_id"])],
                    sort_keys=True, separators=(",", ":"),
                ),
            )
            for item in plan.attempts
        )
        context_grants = tuple(
            (
                plan.local_id("attempt", str(item["attempt_id"])),
                canonical_json_bytes(item).decode("utf-8"),
                hashlib.sha256(canonical_json_bytes(item)).hexdigest(),
                json.dumps(
                    {"source_attempt_id": str(item["attempt_id"]),
                     "local_attempt_id": plan.local_id("attempt", str(item["attempt_id"]))},
                    sort_keys=True, separators=(",", ":"),
                ),
            )
            for item in plan.context_plans
        )
        attachment_grants = tuple(
            (
                plan.local_id("attachment", str(item["source_id"])), operation_id, str(item["source_id"]),
                node_ids[("attachment", str(item["source_id"]))],
                plan.local_id("attachment", str(item["source_id"])) if connection.execute(
                    select(attachment_blobs.c.digest).where(
                        attachment_blobs.c.digest == bytes.fromhex(str(item["blob_digest"])),
                        attachment_blobs.c.byte_size == int(item["byte_size"]),
                        attachment_blobs.c.state == "ready",
                    )
                ).first() is not None else None,
                bytes.fromhex(str(item["blob_digest"])), int(item["byte_size"]),
                json.dumps(item, sort_keys=True, separators=(",", ":")),
                "READY" if connection.execute(
                    select(attachment_blobs.c.digest).where(
                        attachment_blobs.c.digest == bytes.fromhex(str(item["blob_digest"])),
                        attachment_blobs.c.byte_size == int(item["byte_size"]),
                        attachment_blobs.c.state == "ready",
                    )
                ).first() is not None else "MISSING_EXTERNAL",
                timestamp,
                timestamp if connection.execute(
                    select(attachment_blobs.c.digest).where(
                        attachment_blobs.c.digest == bytes.fromhex(str(item["blob_digest"])),
                        attachment_blobs.c.byte_size == int(item["byte_size"]),
                        attachment_blobs.c.state == "ready",
                    )
                ).first() is not None else None,
            )
            for item in plan.attachments
        )
        source_grants = ((
            chat_id, operation_id, captured.source_chat_id, captured.source_chat_revision,
            plan.chat.get("archived_at"), node_ids[("chat", captured.source_chat_id)], timestamp,
            json.dumps(plan.chat_configuration, sort_keys=True, separators=(",", ":")),
            json.dumps(plan.continuation_history or {}, sort_keys=True, separators=(",", ":")),
        ),)
        message_source_grants = tuple(
            (
                plan.local_id("message", str(item["source_id"])), chat_id,
                str(item["source_id"]), str(item["lineage_id"]),
                node_ids[("message", str(item["source_id"]))],
            )
            for item in plan.messages
        )
        lineage_grants = tuple(
            (chat_id, plan.local_id("lineage", source_lineage), source_lineage,
             node_ids[("lineage", source_lineage)])
            for source_lineage in dict.fromkeys(str(item["lineage_id"]) for item in plan.messages)
        )
        derivation_grants = tuple(
            (
                chat_id,
                str(row["object_kind"]),
                plan.local_id(str(row["object_kind"]), str(row["object_id"])),
                plan.local_id("message", str(row["derivation"]["predecessor"]["object_id"])),
            )
            for row in plan.object_provenance
            if isinstance(row.get("derivation"), dict)
            and row["derivation"].get("kind") == "local-continuation"
        )
        history = plan.continuation_history or {}
        source_branches = history.get("branches", []) if isinstance(history, dict) else []

        def sealed_imported_branch(branch: object) -> tuple[object, ...]:
            if not isinstance(branch, dict):
                raise ArchiveImportStoreError("continuation branch is malformed")
            source_key, source_first, source_attempt = (
                branch.get("anchor_key"), branch.get("first_message_id"), branch.get("attempt_id")
            )
            revision = branch.get("choice_revision")
            if (not isinstance(source_key, str) or not isinstance(source_first, str)
                    or not isinstance(source_attempt, str) or type(revision) is not int):
                raise ArchiveImportStoreError("continuation branch is malformed")
            local_key = "empty" if source_key == "empty" else plan.local_id("message", source_key)
            snapshot = {
                "anchor_key": source_key,
                "choice_revision": revision,
                "first_message_id": source_first,
                "attempt_id": source_attempt,
                "created_at": branch.get("created_at"),
            }
            return (
                plan.local_id("message", source_first), chat_id,
                plan.local_id("attempt", source_attempt), local_key,
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
            )

        imported_branch_grants = tuple(
            sealed_imported_branch(branch) for branch in source_branches
        )
        anchor_rows = history.get("anchors", []) if isinstance(history, dict) else []
        terminal_source_bases = (
            ["empty"] + [
                str(row["anchor_key"]) for row in anchor_rows
                if isinstance(row, dict) and row.get("anchor_key") != "empty"
            ]
            if isinstance(anchor_rows, list) and anchor_rows
            else ["empty"] + [
                str(item["source_id"]) for item in plan.messages
                if str(item["role"]) == "assistant"
                and str(item["state"]) in {"complete", "incomplete", "failed", "aborted", "truncated"}
            ]
        )
        continuation_anchor_grants = tuple(
            (
                chat_id, "empty" if base_key == "empty" else plan.local_id("message", base_key),
                None if base_key == "empty" else plan.local_id("message", base_key), 1,
                json.dumps(
                    next((row for row in anchor_rows if row.get("anchor_key") == base_key), {}).get(
                        "source_configuration", plan.chat_configuration
                    ), sort_keys=True, separators=(",", ":"),
                ),
                "UNRESOLVED",
                json.dumps({"kind":"imported-source","reason":"provider mapping requires explicit local admission"}, sort_keys=True, separators=(",", ":")),
            )
            for base_key in dict.fromkeys(terminal_source_bases)
        )
        arm_phase9_import_graph(
            connection, operation_id, identities, messages=message_grants, chats=chat_grants,
            links=link_grants, attempts=attempt_grants, contexts=context_grants, attachments=attachment_grants,
            sources=source_grants,
            nodes=tuple(node_grants),
            imported_branches=imported_branch_grants,
            message_sources=message_source_grants,
            lineages=lineage_grants,
            continuation_anchors=continuation_anchor_grants,
            derivations=derivation_grants,
        )
        arm_phase7_source_mutation(connection, "archive import graph")
        try:
            connection.exec_driver_sql(
                "INSERT INTO chats(id,title,created_at,updated_at,head_message_id,revision,archived_at) VALUES (?,?,?,?,NULL,0,?)",
                (chat_id, str(plan.chat["title"]), canonical_time(plan.chat["created_at"]), canonical_time(plan.chat["updated_at"]),
                 timestamp if import_as_archived else None),
            )
            self._insert_import_lineage_nodes(connection, tuple(node_grants))
            chat_node = node_ids[("chat", captured.source_chat_id)]
            connection.exec_driver_sql(
                "INSERT INTO archive_import_chats(chat_id,operation_id,source_chat_id,source_chat_revision,source_archived_at,source_node_id,imported_at,source_configuration,source_continuation_history) VALUES (?,?,?,?,?,?,?,?,?)",
                (chat_id, operation_id, captured.source_chat_id, captured.source_chat_revision,
                 plan.chat.get("archived_at"), chat_node, timestamp,
                 json.dumps(plan.chat_configuration, sort_keys=True, separators=(",", ":")),
                 json.dumps(plan.continuation_history or {}, sort_keys=True, separators=(",", ":"))),
            )
            for item in plan.messages:
                local_id = plan.local_id("message", str(item["source_id"]))
                local_parent = None if item.get("parent_id") is None else plan.local_id("message", str(item["parent_id"]))
                local_supersedes = None if item.get("supersedes_id") is None else plan.local_id("message", str(item["supersedes_id"]))
                local_lineage = plan.local_id("lineage", str(item["lineage_id"]))
                connection.exec_driver_sql(
                    "INSERT INTO messages(id,chat_id,parent_id,sequence,role,state,content,created_at,lineage_id,revision,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (local_id, chat_id, local_parent, int(item["sequence"]), str(item["role"]), str(item["state"]), str(item["content"]), canonical_time(item["created_at"]), local_lineage, int(item["revision"]), local_supersedes),
                )
                connection.exec_driver_sql(
                    "INSERT INTO archive_import_messages(message_id,chat_id,source_message_id,source_lineage_id,source_node_id) VALUES (?,?,?,?,?)",
                    (local_id, chat_id, str(item["source_id"]), str(item["lineage_id"]), node_ids[("message", str(item["source_id"]))]),
                )
            payload_text_facts = {
                digest: (snapshot.text_representation_id, snapshot.ineligibility_reason)
                for digest, snapshot in captured.payloads
            }
            for item in plan.attachments:
                source_id = str(item["source_id"])
                attachment_id = plan.local_id("attachment", source_id)
                digest = bytes.fromhex(str(item["blob_digest"]))
                archived_filename = (
                    str(item["filename"])
                    if item.get("filename_status") == "available" and isinstance(item.get("filename"), str)
                    else "imported-attachment"
                )
                backing = connection.execute(
                    select(attachments).select_from(attachments.join(attachment_blobs, attachments.c.blob_digest == attachment_blobs.c.digest))
                    .where(attachment_blobs.c.digest == digest, attachment_blobs.c.byte_size == int(item["byte_size"]), attachment_blobs.c.state == "ready")
                    .order_by(attachments.c.id).limit(1)
                ).first()
                blob_ready = connection.execute(select(attachment_blobs.c.digest).where(attachment_blobs.c.digest == digest, attachment_blobs.c.byte_size == int(item["byte_size"]), attachment_blobs.c.state == "ready")).first()
                if backing is not None or blob_ready is not None:
                    text_representation_id, ineligibility_reason = (
                        (backing.text_representation_id, backing.ineligibility_reason)
                        if backing is not None else payload_text_facts.get(str(item["blob_digest"]), (None, None))
                    )
                    if backing is None and str(item["blob_digest"]) not in payload_text_facts:
                        raise ArchiveImportStoreError(
                            "ready imported payload lacks sealed text classification"
                        )
                    arm_phase6_attachment_insert(connection, attachment_id)
                    try:
                        connection.execute(insert(attachments).values(
                            id=attachment_id, blob_digest=digest, filename=archived_filename,
                            source_kind="imported", source_name=archived_filename,
                            text_representation_id=text_representation_id,
                            text_digest=text_representation_id,
                            ineligibility_reason=ineligibility_reason, created_at=timestamp,
                        ))
                        require_phase6_consumed(connection)
                    finally:
                        clear_phase6(connection)
                connection.exec_driver_sql(
                    "INSERT INTO archive_import_attachment_refs(id,operation_id,source_attachment_id,source_node_id,attachment_id,expected_digest,expected_size,source_metadata,availability,created_at,healed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (attachment_id, operation_id, source_id,
                     node_ids[("attachment", source_id)], attachment_id if (backing is not None or blob_ready is not None) else None, digest,
                     int(item["byte_size"]), json.dumps(item, sort_keys=True, separators=(",", ":")),
                     "READY" if (backing is not None or blob_ready is not None) else "MISSING_EXTERNAL", timestamp,
                     timestamp if (backing is not None or blob_ready is not None) else None),
                )
            for relation in plan.message_attachment_relations:
                connection.exec_driver_sql(
                    "INSERT INTO archive_import_message_attachment_refs(message_id,attachment_ref_id,ordinal) VALUES (?,?,?)",
                    (plan.local_id("message", str(relation["message_id"])),
                     plan.local_id("attachment", str(relation["attachment_id"])), int(relation["ordinal"])),
                )
            for source_lineage in dict.fromkeys(str(item["lineage_id"]) for item in plan.messages):
                connection.exec_driver_sql(
                    "INSERT INTO archive_import_lineages(chat_id,local_lineage_id,source_lineage_id,source_node_id) VALUES (?,?,?,?)",
                    (chat_id, plan.local_id("lineage", source_lineage), source_lineage, node_ids[("lineage", source_lineage)]),
                )
            for item in plan.attempts:
                source_id = str(item["source_id"])
                state = "incomplete" if str(item["state"]) == "truncated" else str(item["state"])
                connection.exec_driver_sql(
                    "INSERT INTO archive_imported_attempts(id,chat_id,user_message_id,assistant_message_id,source_attempt_id,source_node_id,state,started_at,ended_at,source_attempt,source_evidence_binding) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (plan.local_id("attempt", source_id), chat_id,
                     plan.local_id("message", str(item["user_message_id"])),
                     plan.local_id("message", str(item["assistant_message_id"])),
                     source_id, node_ids[("attempt", source_id)], state,
                     canonical_time(item["started_at"]), canonical_time(item["ended_at"]),
                     json.dumps(item, sort_keys=True, separators=(",", ":")),
                     json.dumps([row for row in plan.history_bindings if row.get("attempt_id") == source_id], sort_keys=True, separators=(",", ":"))),
                )
            for derivation in derivation_grants:
                connection.exec_driver_sql(
                    "INSERT INTO archive_object_derivations(chat_id,object_kind,object_id,predecessor_message_id) VALUES (?,?,?,?)",
                    derivation,
                )
            for item in plan.context_plans:
                source_attempt_id = str(item["attempt_id"])
                local_attempt_id = plan.local_id("attempt", source_attempt_id)
                source_plan = canonical_json_bytes(item).decode("utf-8")
                connection.exec_driver_sql(
                    "INSERT INTO archive_imported_context_plans(attempt_id,source_plan,source_plan_digest,local_bindings) VALUES (?,?,?,?)",
                    (local_attempt_id, source_plan, hashlib.sha256(source_plan.encode("utf-8")).hexdigest(),
                     json.dumps({"source_attempt_id": source_attempt_id, "local_attempt_id": local_attempt_id}, sort_keys=True, separators=(",", ":"))),
                )
            anchor_rows = history.get("anchors", [])
            terminal_bases = (
                ["empty"] + [
                    str(row["anchor_key"]) for row in anchor_rows
                    if isinstance(row, dict) and row.get("anchor_key") != "empty"
                ]
                if isinstance(anchor_rows, list) and anchor_rows
                else ["empty"] + [
                    str(item["source_id"]) for item in plan.messages
                    if str(item["role"]) == "assistant"
                    and str(item["state"]) in {"complete", "incomplete", "failed", "aborted", "truncated"}
                ]
            )
            for base_key in dict.fromkeys(terminal_bases):
                source_anchor = next(
                    (row for row in anchor_rows if row.get("anchor_key") == base_key), {}
                ) if isinstance(anchor_rows, list) else {}
                configuration = source_anchor.get("source_configuration", plan.chat_configuration)
                local_base = None if base_key == "empty" else plan.local_id("message", base_key)
                local_key = "empty" if local_base is None else local_base
                connection.exec_driver_sql(
                    "INSERT INTO archive_continuation_anchors("
                    "chat_id,base_key,base_message_id,revision,source_configuration,resolution,resolution_evidence) "
                    "VALUES (?,?,?,?,?,'UNRESOLVED',?)",
                    (chat_id, local_key, local_base, 1,
                     json.dumps(configuration, sort_keys=True, separators=(",", ":")),
                     json.dumps({"kind":"imported-source","reason":"provider mapping requires explicit local admission"}, sort_keys=True, separators=(",", ":"))),
                )
            # Deterministic receiver equivalence was selected during preflight
            # and sealed with the internal import graph.  Do not query or
            # reselect receiver catalogue state after the durable cutoff.
            for automatic in plan.initial_receiver_continuation:
                if not isinstance(automatic, dict):
                    raise ArchiveImportStoreError("sealed receiver continuation is malformed")
                try:
                    local_key = str(automatic["base_key"])
                    connection_id = str(automatic["connection_id"])
                    model_id = str(automatic["model_id"])
                    settings = automatic["explicit_settings"]
                    descriptor = automatic["safe_target_descriptor"]
                    resolution = str(automatic["resolution"])
                    evidence = automatic["resolution_evidence"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ArchiveImportStoreError("sealed receiver continuation is malformed") from exc
                if (not isinstance(settings, dict) or not isinstance(descriptor, dict)
                        or resolution != "EQUIVALENT" or not isinstance(evidence, dict)):
                    raise ArchiveImportStoreError("sealed receiver continuation is malformed")
                try:
                    _validate_settings(GenerationSettings(**settings))
                except (TypeError, ValueError) as exc:
                    raise ArchiveImportStoreError("sealed receiver continuation is malformed") from exc
                # The preflight target is sealed by identity.  Revalidate that
                # exact pair at graph admission, but never fall through to a
                # newly discovered candidate if it was disabled, retired, or
                # otherwise ceased to be runnable after preflight.
                target = connection.exec_driver_sql(
                    "SELECT p.backend_type,p.profile,p.enabled,p.retired,m.availability,m.provider_model_id "
                    "FROM model_catalogue_entries m JOIN provider_connections p ON p.id=m.connection_id "
                    "WHERE p.id=? AND m.id=?",
                    (connection_id, model_id),
                ).mappings().one_or_none()
                if (target is None or str(target["backend_type"]) != "fake"
                        or str(target["profile"]) != "generic" or not bool(target["enabled"])
                        or bool(target["retired"]) or str(target["availability"]) != "available"
                        or str(target["provider_model_id"]) != str(descriptor.get("provider_model_id"))
                        or not self._phase9_fake_candidate_is_runnable(
                            model_id, GenerationSettings(**settings),
                        )):
                    raise ArchiveImportStoreError("sealed receiver continuation target is no longer runnable")
                automatic_choice = (
                    chat_id, local_key, 1, connection_id, model_id,
                    json.dumps(settings, sort_keys=True, separators=(",", ":")), 0, "[]",
                    "equivalent", json.dumps(descriptor, sort_keys=True, separators=(",", ":")), timestamp,
                )
                arm_phase9_import_continuation_rows(connection, choices=(automatic_choice,))
                connection.exec_driver_sql(
                    "INSERT INTO archive_continuation_choices(chat_id,base_key,choice_revision,local_connection_id,local_model_entry_id,explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at) VALUES (?,?,1,?,?,?,0,'[]','equivalent',?,?)",
                    (chat_id, local_key, connection_id, model_id,
                     json.dumps(settings, sort_keys=True, separators=(",", ":")),
                     json.dumps(descriptor, sort_keys=True, separators=(",", ":")), timestamp),
                )
                connection.exec_driver_sql(
                    "UPDATE archive_continuation_anchors SET resolution=?,resolution_evidence=? WHERE chat_id=? AND base_key=?",
                    (resolution, json.dumps(evidence, sort_keys=True, separators=(",", ":")), chat_id, local_key),
                )
            # Archive choices are historical source facts.  They retain their
            # portable descriptor/settings/exclusions but deliberately have
            # no local target IDs: importing must never create or select a
            # provider.  A later local choice is appended separately.
            source_choices = history.get("choices", []) if isinstance(history, dict) else []
            for choice in source_choices:
                if not isinstance(choice, dict):
                    raise ArchiveImportStoreError("continuation choice is malformed")
                source_key = choice.get("anchor_key")
                revision = choice.get("choice_revision")
                if not isinstance(source_key, str) or type(revision) is not int:
                    raise ArchiveImportStoreError("continuation choice is malformed")
                local_key = "empty" if source_key == "empty" else plan.local_id("message", source_key)
                exclusions = choice.get("excluded_context_refs")
                descriptor = choice.get("mapped_model")
                settings = choice.get("explicit_settings")
                if not isinstance(exclusions, list) or not isinstance(descriptor, dict) or not isinstance(settings, dict):
                    raise ArchiveImportStoreError("continuation choice is malformed")
                local_exclusions = []
                for exclusion in exclusions:
                    if not isinstance(exclusion, dict):
                        raise ArchiveImportStoreError("continuation exclusion is malformed")
                    mapped = dict(exclusion)
                    if mapped.get("attachment_id") is not None:
                        if not isinstance(mapped["attachment_id"], str):
                            raise ArchiveImportStoreError("continuation exclusion is malformed")
                        mapped["attachment_id"] = plan.local_id("attachment", mapped["attachment_id"])
                    local_exclusions.append(mapped)
                decision_kind = choice.get("decision_kind")
                if decision_kind == "operator-resolution":
                    decision_kind = "operator_resolution"
                if decision_kind not in {"equivalent", "operator_resolution"}:
                    raise ArchiveImportStoreError("continuation choice is malformed")
                source_choice = (
                    chat_id, local_key, revision, None, None,
                    json.dumps(settings, sort_keys=True, separators=(",", ":")),
                    1 if local_exclusions else 0,
                    json.dumps(local_exclusions, separators=(",", ":")), str(decision_kind),
                    json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
                    canonical_time(str(choice.get("chosen_at"))),
                )
                arm_phase9_import_continuation_rows(connection, choices=(source_choice,))
                connection.exec_driver_sql(
                    "INSERT INTO archive_continuation_choices(chat_id,base_key,choice_revision,local_connection_id,local_model_entry_id,explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at) VALUES (?,?,?,NULL,NULL,?,?,?,?,?,?)",
                    (chat_id, local_key, revision,
                     json.dumps(settings, sort_keys=True, separators=(",", ":")),
                     1 if local_exclusions else 0,
                     json.dumps(local_exclusions, separators=(",", ":")), str(decision_kind),
                     json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
                     canonical_time(str(choice.get("chosen_at")))),
                )
            for first_message_id, branch_chat_id, attempt_id, base_key, choice_snapshot in imported_branch_grants:
                choice_revision = json.loads(str(choice_snapshot)).get("choice_revision")
                if connection.exec_driver_sql(
                    "SELECT 1 FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=?",
                    (branch_chat_id, base_key, choice_revision),
                ).first() is None:
                    raise ArchiveImportStoreError("imported branch lacks its sealed continuation choice")
                connection.exec_driver_sql(
                    "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                    (branch_chat_id, first_message_id, attempt_id, base_key, choice_snapshot),
                )
            for relation in plan.attempt_attachment_relations:
                connection.exec_driver_sql(
                    "INSERT INTO archive_import_attempt_attachment_refs(attempt_id,attachment_ref_id,ordinal) VALUES (?,?,?)",
                    (plan.local_id("attempt", str(relation["attempt_id"])),
                     plan.local_id("attachment", str(relation["attachment_id"])), int(relation["ordinal"])),
                )
            # Requirements come only from the source attempt's *selected*
            # attachment slots.  A message link is historical graph context,
            # not proof that the attachment was selected for this request.
            # The v2 history binding fixes the source-context ordinal and the
            # representation evidence; neither can be reconstructed from a
            # digest-wide attachment search.
            attachment_by_source = {str(item["source_id"]): item for item in plan.attachments}
            terminal_attempts = {
                str(item["source_id"]): str(item["assistant_message_id"])
                for item in plan.attempts
                if str(item["state"]) in {"complete", "incomplete", "failed", "aborted", "truncated"}
            }
            requirement_bases = list(terminal_attempts.items())
            terminal_attempt_bases = set(terminal_attempts.values())
            for branch in source_branches:
                if branch.get("anchor_key") == "empty":
                    continue
                # A terminal assistant anchor already owns its original source
                # requirement slots.  Branches rooted at that anchor are
                # sibling native plans with their own dense ordinals; they must
                # not overwrite or contradict the historical anchor's slots.
                if str(branch["anchor_key"]) in terminal_attempt_bases:
                    continue
                pair = (str(branch["attempt_id"]), str(branch["anchor_key"]))
                if pair not in requirement_bases:
                    requirement_bases.append(pair)
            bindings_by_attempt: dict[str, list[dict[str, object]]] = {}
            for binding in plan.history_bindings:
                if not isinstance(binding, dict) or binding.get("binding_kind") != "context-source":
                    continue
                snapshot = binding.get("snapshot_source")
                current = binding.get("current_binding")
                if (not isinstance(snapshot, dict) or snapshot.get("kind") != "attachment"
                        or (current is not None and (not isinstance(current, dict) or current.get("object_kind") != "attachment"))
                        or not isinstance(binding.get("attempt_id"), str)
                        or type(binding.get("ordinal")) is not int):
                    continue
                bindings_by_attempt.setdefault(str(binding["attempt_id"]), []).append(binding)
            unrepresentable_bases: set[str] = set()
            for source_attempt, source_base_key in requirement_bases:
                base_key = plan.local_id("message", source_base_key)
                selected = [
                    row for row in plan.attempt_attachment_relations
                    if str(row["attempt_id"]) == source_attempt
                ]
                selected.sort(key=lambda row: int(row["ordinal"]))
                bindings = sorted(bindings_by_attempt.get(source_attempt, []), key=lambda row: int(row["ordinal"]))
                covered_source_attachments: set[str] = set()
                if selected and not bindings:
                    unrepresentable_bases.add(base_key)
                for binding in bindings:
                    snapshot = binding["snapshot_source"]
                    evidence_digest = str(snapshot["evidence_digest"])
                    source_status = snapshot.get("source_id_status")
                    requirement_ordinal = int(binding["ordinal"])
                    candidates: list[dict[str, object]]
                    if source_status == "available" and binding.get("binding_state") == "bound":
                        source_attachment = str(snapshot.get("source_id"))
                        current = binding["current_binding"]
                        if not isinstance(current, dict) or str(current.get("object_id")) != source_attachment:
                            unrepresentable_bases.add(base_key)
                            continue
                        candidates = [row for row in selected if str(row["attachment_id"]) == source_attachment]
                        if len(candidates) != 1:
                            unrepresentable_bases.add(base_key)
                            continue
                        binding_kind = "identity"
                    elif (
                        binding.get("binding_state") in {"unavailable", "historical-only"}
                        and binding.get("current_binding") is None
                    ):
                        # A sealed source slot without an exact typed current
                        # arm can establish content dependency, never an
                        # invented object identity.  Its complete candidate
                        # set is restricted to this selected source attempt's
                        # slots; a global same-digest row is not a candidate.
                        candidates = [
                            row for row in selected
                            if str(attachment_by_source[str(row["attachment_id"])].get("text_digest")) == evidence_digest
                            and attachment_by_source[str(row["attachment_id"])].get("text_eligibility") == "eligible"
                        ]
                        if not candidates:
                            unrepresentable_bases.add(base_key)
                            continue
                        binding_kind = "digest"
                    else:
                        unrepresentable_bases.add(base_key)
                        continue
                    candidate_attachments = [attachment_by_source[str(row["attachment_id"])] for row in candidates]
                    covered_source_attachments.update(str(row["attachment_id"]) for row in candidates)
                    digests = {str(row["blob_digest"]) for row in candidate_attachments}
                    sizes = {int(row["byte_size"]) for row in candidate_attachments}
                    if len(digests) != 1 or len(sizes) != 1:
                        unrepresentable_bases.add(base_key)
                        continue
                    expected_digest = digests.pop()
                    expected_size = sizes.pop()
                    if binding_kind == "identity":
                        bound_ref_id = plan.local_id("attachment", str(candidates[0]["attachment_id"]))
                    else:
                        bound_ref_id = None
                    requirement_grant = (
                        chat_id, base_key, requirement_ordinal,
                        plan.local_id("attempt", source_attempt), None,
                        bytes.fromhex(expected_digest), expected_size, bytes.fromhex(evidence_digest),
                        binding_kind, None, bound_ref_id,
                    )
                    candidate_grants = tuple(
                        (chat_id, base_key, requirement_ordinal,
                         plan.local_id("attachment", str(candidate["attachment_id"])), None,
                         plan.local_id("attachment", str(candidate["attachment_id"])))
                        for candidate in candidates
                    )
                    arm_phase9_import_continuation_rows(
                        connection, requirements=(requirement_grant,), candidates=candidate_grants,
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO archive_continuation_requirements("
                        "chat_id,base_key,ordinal,source_imported_attempt_id,source_native_attempt_id,expected_digest,expected_size,representation_digest,binding_kind,bound_native_attachment_id,bound_imported_ref_id) "
                        "VALUES (?,?,?,?,NULL,?,?,?,?,NULL,?)",
                        (chat_id, base_key, requirement_ordinal, plan.local_id("attempt", source_attempt),
                         bytes.fromhex(expected_digest), expected_size, bytes.fromhex(evidence_digest),
                         binding_kind, bound_ref_id),
                    )
                    for candidate in candidates:
                        ref_id = plan.local_id("attachment", str(candidate["attachment_id"]))
                        connection.exec_driver_sql(
                            "INSERT INTO archive_continuation_requirement_candidates(chat_id,base_key,ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id) VALUES (?,?,?,?,NULL,?)",
                            (chat_id, base_key, requirement_ordinal, ref_id, ref_id),
                        )
                if any(str(row["attachment_id"]) not in covered_source_attachments for row in selected):
                    unrepresentable_bases.add(base_key)
            for base_key in unrepresentable_bases:
                connection.exec_driver_sql(
                    "UPDATE archive_continuation_anchors SET resolution='NEEDS_OPERATOR',resolution_evidence=? WHERE chat_id=? AND base_key=?",
                    (json.dumps({"kind":"source-context","reason":"selected attachment slot lacks an exact typed history binding"}, sort_keys=True, separators=(",", ":")), chat_id, base_key),
                )
            if head is not None:
                connection.exec_driver_sql(
                    "UPDATE chats SET head_message_id=?,revision=1 WHERE id=? AND revision=0 AND head_message_id IS NULL",
                    (plan.local_id("message", str(head)), chat_id),
                )
            return self._require_phase7_source_consumed(connection)
        finally:
            clear_phase9_import_graph(connection)
            clear_phase9_object_derivations(connection)
            clear_phase7_source_mutation(connection)

    @staticmethod
    def _insert_import_lineage_nodes(connection, nodes: tuple[tuple[object, ...], ...]) -> None:
        """Persist the exact preallocated per-object provenance rows."""
        for node in nodes:
            connection.exec_driver_sql(
                "INSERT INTO archive_lineage_nodes(id,object_kind,archive_id,logical_content_digest,source_object_id,imported_at,source_format,prior_node_id) VALUES (?,?,?,?,?,?,?,?)",
                node,
            )

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
                # A caller cannot distinguish a successful SQLite commit
                # followed by a transport/runtime failure from an uncertain
                # commit return.  Exercise that exact post-return boundary in
                # deterministic fault tests and fail closed here.
                _fault(f"after-attachment-commit:{operation}")
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
            # A close which succeeds below SQLite but loses its return path is
            # uncertain to this authority just like a raised close result.
            _fault(f"after-attachment-close:{operation}")
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
            if revision != _PHASE9_REVISION:
                raise StateError("Phase 9 persistence schema is not current")
            validate_phase6_schema(connection)
            validate_phase9_schema(connection)
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

    def admit_import_continuation_choice(
        self, chat_id: str, base_key: str, *, expected_choice_revision: int,
        connection_id: str, model_entry_id: str, explicit_settings: dict[str, object],
        excluded_refs: tuple[dict[str, object], ...], now,
    ) -> int:
        """Append one explicit local continuation choice without creating providers."""
        self._ensure_open()
        if (not chat_id or not base_key or type(expected_choice_revision) is not int
                or expected_choice_revision < 0 or not connection_id or not model_entry_id
                or set(explicit_settings) != {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}
                or any(type(value) is not dict or set(value) != {"requirement_ordinal", "expected_digest", "attachment_id", "reason"} for value in excluded_refs)):
            raise StateError("continuation choice is malformed")
        try:
            _validate_settings(GenerationSettings(**explicit_settings))
        except (TypeError, ValueError) as exc:
            raise StateError("continuation choice settings are malformed") from exc
        timestamp = utc_iso(now)
        with self._authority.transition(), self._search_transaction("admit imported continuation choice") as connection:
            anchor = connection.exec_driver_sql(
                "SELECT 1 FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?", (chat_id, base_key)
            ).first()
            if anchor is None:
                raise StateError("imported continuation anchor is absent")
            latest = connection.exec_driver_sql(
                "SELECT COALESCE(MAX(choice_revision),0) FROM archive_continuation_choices WHERE chat_id=? AND base_key=?",
                (chat_id, base_key),
            ).scalar_one()
            if int(latest) != expected_choice_revision:
                raise RevisionConflict("continuation choice revision changed")
            ordinals = [item["requirement_ordinal"] for item in excluded_refs]
            if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)) or any(type(value) is not int or value < 0 for value in ordinals):
                raise StateError("continuation exclusions are not ordered unique requirements")
            for item in excluded_refs:
                requirement = connection.exec_driver_sql(
                    "SELECT hex(expected_digest) AS expected_digest,binding_kind FROM archive_continuation_requirements WHERE chat_id=? AND base_key=? AND ordinal=?",
                    (chat_id, base_key, item["requirement_ordinal"]),
                ).mappings().one_or_none()
                attachment_id = item["attachment_id"]
                if (requirement is None or str(requirement["expected_digest"]).lower() != str(item["expected_digest"]).lower()
                        or item["reason"] not in {"missing-external", "operator-excluded"}
                        or (attachment_id is None and str(requirement["binding_kind"]) != "digest")
                        or (attachment_id is not None and not isinstance(attachment_id, str))):
                    raise StateError("continuation exclusion does not match its source requirement")
                if attachment_id is not None and connection.exec_driver_sql(
                    "SELECT 1 FROM archive_continuation_requirement_candidates WHERE chat_id=? AND base_key=? AND ordinal=? AND candidate_attachment_id=?",
                    (chat_id, base_key, item["requirement_ordinal"], attachment_id),
                ).first() is None:
                    raise StateError("continuation exclusion attachment is not a source requirement candidate")
            target = connection.execute(
                select(
                    provider_connections.c.id, provider_connections.c.backend_type,
                    provider_connections.c.profile, provider_connections.c.endpoint,
                    provider_connections.c.enabled, provider_connections.c.retired,
                    provider_connections.c.revision.label("connection_revision"),
                    model_catalogue_entries.c.id.label("model_entry_id"),
                    model_catalogue_entries.c.connection_id, model_catalogue_entries.c.provider_model_id,
                    model_catalogue_entries.c.availability, model_catalogue_entries.c.revision.label("model_revision"),
                ).select_from(model_catalogue_entries.join(provider_connections, model_catalogue_entries.c.connection_id == provider_connections.c.id))
                .where(provider_connections.c.id == connection_id, model_catalogue_entries.c.id == model_entry_id)
            ).mappings().one_or_none()
            if (target is None or not bool(target["enabled"]) or bool(target["retired"])
                    or str(target["availability"]).lower() != "available"):
                raise StateError("continuation target is not runnable")
            descriptor = {
                "connection_id": connection_id, "model_entry_id": model_entry_id,
                "backend_type": str(target["backend_type"]), "provider_profile": str(target["profile"]),
                "provider_model_id": str(target["provider_model_id"]),
                "availability": str(target["availability"]),
                "connection_revision": int(target["connection_revision"]), "model_revision": int(target["model_revision"]),
            }
            next_revision = expected_choice_revision + 1
            connection.exec_driver_sql(
                "INSERT INTO archive_continuation_choices(chat_id,base_key,choice_revision,local_connection_id,local_model_entry_id,explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at) VALUES (?,?,?,?,?,?,?,?,'operator_resolution',?,?)",
                (chat_id, base_key, next_revision, connection_id, model_entry_id,
                 json.dumps(explicit_settings, sort_keys=True, separators=(",", ":")),
                 1 if excluded_refs else 0,
                 json.dumps(list(excluded_refs), separators=(",", ":")),
                 json.dumps(descriptor, sort_keys=True, separators=(",", ":")), timestamp),
            )
        return next_revision

    def prepare_imported_user_edit_continuation(
        self, chat_id: str, user_message_id: str, *, now,
    ) -> ContinuationReadiness | None:
        """Prepare one explicit local edit branch for an imported user turn.

        The imported user remains immutable.  This method creates or reuses a
        local continuation anchor on that user, copies the imported turn's
        attachment requirements, and appends the edit's explicit local choice.
        It returns ``None`` for an ordinary native user message.
        """
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            donor = connection.exec_driver_sql(
                "SELECT a.assistant_message_id "
                "FROM archive_imported_attempts AS a "
                "JOIN archive_import_messages AS m "
                "ON m.message_id=a.user_message_id AND m.chat_id=a.chat_id "
                "WHERE a.chat_id=? AND a.user_message_id=? "
                "ORDER BY a.started_at,a.id LIMIT 1",
                (chat_id, user_message_id),
            ).first()
            if donor is None:
                return None
            donor_base_key = str(donor[0])
            donor_anchor = connection.exec_driver_sql(
                "SELECT source_configuration FROM archive_continuation_anchors "
                "WHERE chat_id=? AND base_key=?",
                (chat_id, donor_base_key),
            ).first()
            if donor_anchor is None:
                raise StateError("imported user edit lacks its source continuation anchor")
            existing_anchor = connection.exec_driver_sql(
                "SELECT 1 FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?",
                (chat_id, user_message_id),
            ).first()

        _application_settings, default_model_id, _revision = (
            self.get_application_generation_config()
        )
        if default_model_id is None:
            raise StateError("application default model is required to edit imported history")
        model = self.get_model_catalogue_entry(default_model_id)
        if model is None or model.availability is not CatalogueAvailability.AVAILABLE:
            raise StateError("application default model is not available")

        timestamp = utc_iso(now)
        with self._authority.transition(), self._engine.begin() as connection:
            if existing_anchor is None:
                connection.exec_driver_sql(
                    "INSERT INTO archive_continuation_anchors("
                    "chat_id,base_key,base_message_id,revision,source_configuration,resolution,resolution_evidence"
                    ") VALUES (?,?,?,?,?,'UNRESOLVED',?)",
                    (chat_id, user_message_id, user_message_id, 1, donor_anchor[0],
                     json.dumps({
                         "kind": "imported-user-edit-branch",
                         "source_base_key": donor_base_key,
                     }, sort_keys=True, separators=(",", ":"))),
                )
                connection.exec_driver_sql(
                    "INSERT INTO archive_continuation_requirements("
                    "chat_id,base_key,ordinal,source_imported_attempt_id,source_native_attempt_id,"
                    "expected_digest,expected_size,representation_digest,binding_kind,"
                    "bound_native_attachment_id,bound_imported_ref_id"
                    ") SELECT chat_id,?,ordinal,source_imported_attempt_id,source_native_attempt_id,"
                    "expected_digest,expected_size,representation_digest,binding_kind,"
                    "bound_native_attachment_id,bound_imported_ref_id "
                    "FROM archive_continuation_requirements WHERE chat_id=? AND base_key=?",
                    (user_message_id, chat_id, donor_base_key),
                )
                connection.exec_driver_sql(
                    "INSERT INTO archive_continuation_requirement_candidates("
                    "chat_id,base_key,ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id"
                    ") SELECT chat_id,?,ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id "
                    "FROM archive_continuation_requirement_candidates WHERE chat_id=? AND base_key=?",
                    (user_message_id, chat_id, donor_base_key),
                )

            latest = int(connection.exec_driver_sql(
                "SELECT COALESCE(MAX(choice_revision),0) FROM archive_continuation_choices "
                "WHERE chat_id=? AND base_key=?",
                (chat_id, user_message_id),
            ).scalar_one())
            target = connection.execute(
                select(
                    provider_connections.c.id, provider_connections.c.backend_type,
                    provider_connections.c.profile, provider_connections.c.endpoint,
                    provider_connections.c.enabled, provider_connections.c.retired,
                    provider_connections.c.revision.label("connection_revision"),
                    model_catalogue_entries.c.id.label("model_entry_id"),
                    model_catalogue_entries.c.connection_id,
                    model_catalogue_entries.c.provider_model_id,
                    model_catalogue_entries.c.availability,
                    model_catalogue_entries.c.revision.label("model_revision"),
                ).select_from(
                    model_catalogue_entries.join(
                        provider_connections,
                        provider_connections.c.id == model_catalogue_entries.c.connection_id,
                    )
                ).where(
                    provider_connections.c.id == model.connection_id,
                    model_catalogue_entries.c.id == model.id,
                )
            ).mappings().one_or_none()
            if (target is None or not bool(target["enabled"]) or bool(target["retired"])
                    or str(target["availability"]).lower() != "available"):
                raise StateError("imported edit continuation target is not runnable")
            descriptor = {
                "connection_id": model.connection_id,
                "model_entry_id": model.id,
                "backend_type": str(target["backend_type"]),
                "provider_profile": str(target["profile"]),
                "provider_model_id": str(target["provider_model_id"]),
                "availability": str(target["availability"]),
                "connection_revision": int(target["connection_revision"]),
                "model_revision": int(target["model_revision"]),
            }
            connection.exec_driver_sql(
                "INSERT INTO archive_continuation_choices("
                "chat_id,base_key,choice_revision,local_connection_id,local_model_entry_id,"
                "explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at"
                ") VALUES (?,?,?,?,?,?,0,'[]','operator_resolution',?,?)",
                (chat_id, user_message_id, latest + 1, model.connection_id, model.id,
                 json.dumps({
                     "temperature": None, "max_output_tokens": None,
                     "reasoning_effort": None, "timeout_seconds": None,
                 }, sort_keys=True, separators=(",", ":")),
                 json.dumps(descriptor, sort_keys=True, separators=(",", ":")), timestamp),
            )
        return self.import_continuation_readiness(chat_id, user_message_id)

    def import_continuation_readiness(self, chat_id: str, base_key: str) -> ContinuationReadiness | None:
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT a.source_configuration,a.resolution,a.resolution_evidence,"
                "c.choice_revision,c.local_connection_id,c.local_model_entry_id,c.explicit_settings,c.degraded,c.excluded_refs "
                "FROM archive_continuation_anchors AS a LEFT JOIN archive_continuation_choices AS c "
                "ON c.chat_id=a.chat_id AND c.base_key=a.base_key AND c.choice_revision=(SELECT MAX(x.choice_revision) FROM archive_continuation_choices AS x WHERE x.chat_id=a.chat_id AND x.base_key=a.base_key) "
                "WHERE a.chat_id=? AND a.base_key=?", (chat_id, base_key)
            ).mappings().one_or_none()
            requirement_rows = connection.exec_driver_sql(
                "SELECT r.ordinal,r.source_imported_attempt_id,r.source_native_attempt_id,"
                "hex(r.expected_digest) AS expected_digest,r.expected_size,"
                "hex(r.representation_digest) AS representation_digest,r.binding_kind,"
                "r.bound_imported_ref_id,r.bound_native_attachment_id,"
                "MAX(CASE WHEN ref.availability='READY' OR native_blob.state='ready' THEN 1 ELSE 0 END) AS identity_ready,"
                "MAX(CASE WHEN candidate_ref.availability='READY' OR candidate_blob.state='ready' THEN 1 ELSE 0 END) AS candidate_ready "
                "FROM archive_continuation_requirements AS r "
                "LEFT JOIN archive_import_attachment_refs AS ref ON ref.id=r.bound_imported_ref_id "
                "LEFT JOIN attachments AS native_ref ON native_ref.id=r.bound_native_attachment_id "
                "LEFT JOIN attachment_blobs AS native_blob ON native_blob.digest=native_ref.blob_digest "
                "LEFT JOIN archive_continuation_requirement_candidates AS candidate "
                "ON candidate.chat_id=r.chat_id AND candidate.base_key=r.base_key AND candidate.ordinal=r.ordinal "
                "LEFT JOIN archive_import_attachment_refs AS candidate_ref ON candidate_ref.id=candidate.imported_ref_id "
                "LEFT JOIN attachments AS candidate_native ON candidate_native.id=candidate.native_attachment_id "
                "LEFT JOIN attachment_blobs AS candidate_blob ON candidate_blob.digest=candidate_native.blob_digest "
                "WHERE r.chat_id=? AND r.base_key=? "
                "GROUP BY r.chat_id,r.base_key,r.ordinal,r.source_imported_attempt_id,r.source_native_attempt_id,"
                "r.expected_digest,r.expected_size,r.representation_digest,r.binding_kind,r.bound_imported_ref_id,"
                "r.bound_native_attachment_id ORDER BY r.ordinal",
                (chat_id, base_key),
            ).mappings().all()
        if row is None:
            return None
        requirements = tuple(
            ContinuationRequirement(
                base_key, int(item["ordinal"]),
                None if item["source_imported_attempt_id"] is None else str(item["source_imported_attempt_id"]),
                None if item["source_native_attempt_id"] is None else str(item["source_native_attempt_id"]),
                str(item["expected_digest"]).lower(), int(item["expected_size"]),
                str(item["representation_digest"]).lower(), str(item["binding_kind"]),
                None if item["bound_imported_ref_id"] is None else str(item["bound_imported_ref_id"]),
                None if item["bound_native_attachment_id"] is None else str(item["bound_native_attachment_id"]),
                ("missing-external" if str(item["binding_kind"]) == "identity"
                 and int(item["identity_ready"] or 0) == 0 else
                 "missing-external" if str(item["binding_kind"]) == "digest"
                 and int(item["candidate_ready"] or 0) == 0 else None),
            )
            for item in requirement_rows
        )
        excluded = () if row["excluded_refs"] is None else tuple(json.loads(str(row["excluded_refs"])))
        excluded_ordinals = {int(item["requirement_ordinal"]) for item in excluded}
        blocked = next((
            item for item in requirements
            if item.blocked_reason is not None and item.ordinal not in excluded_ordinals
        ), None)
        resolution = "UNAVAILABLE" if blocked is not None else str(row["resolution"])
        evidence = json.loads(str(row["resolution_evidence"]))
        if blocked is not None:
            evidence = {"kind": "attachment-requirement", "reason": blocked.blocked_reason,
                        "ordinal": blocked.ordinal, "expected_digest": blocked.expected_digest}
        return ContinuationReadiness(chat_id, base_key, json.loads(str(row["source_configuration"])),
            resolution, evidence,
            0 if row["choice_revision"] is None else int(row["choice_revision"]),
            None if row["local_connection_id"] is None else str(row["local_connection_id"]),
            None if row["local_model_entry_id"] is None else str(row["local_model_entry_id"]),
            None if row["explicit_settings"] is None else json.loads(str(row["explicit_settings"])),
            excluded,
            requirements,
        )

    def materialize_import_continuation_attachments(self, chat_id: str, base_key: str) -> tuple[str, ...]:
        """Resolve one local READY candidate per frozen source requirement.

        This is deliberately a read projection: it neither creates attachment
        identities nor adds message links to the imported user message.
        Candidate rows retain source-slot multiplicity, so consuming a local
        identity twice is rejected rather than silently collapsing slots.
        """
        self._ensure_open()
        readiness = self.import_continuation_readiness(chat_id, base_key)
        if readiness is None:
            return ()
        excluded_ordinals = {
            int(item["requirement_ordinal"]) for item in readiness.excluded_refs
        }
        if readiness.resolution == "UNAVAILABLE" or any(
            requirement.blocked_reason is not None
            and requirement.ordinal not in excluded_ordinals
            for requirement in readiness.requirements
        ):
            raise StateError("imported continuation attachment requirements are unavailable")
        with self._authority.operation(), self._engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT r.ordinal,r.binding_kind,r.bound_imported_ref_id,r.bound_native_attachment_id,"
                "c.imported_ref_id,c.native_attachment_id,"
                "COALESCE(ref.attachment_id,c.native_attachment_id) AS attachment_id "
                "FROM archive_continuation_requirements r "
                "JOIN archive_continuation_requirement_candidates c "
                "ON c.chat_id=r.chat_id AND c.base_key=r.base_key AND c.ordinal=r.ordinal "
                "LEFT JOIN archive_import_attachment_refs ref ON ref.id=c.imported_ref_id "
                "LEFT JOIN attachments native_attachment ON native_attachment.id=c.native_attachment_id "
                "LEFT JOIN attachment_blobs native_blob ON native_blob.digest=native_attachment.blob_digest "
                "WHERE r.chat_id=? AND r.base_key=? AND (ref.availability='READY' OR native_blob.state='ready') "
                "ORDER BY r.ordinal, CASE WHEN c.imported_ref_id=r.bound_imported_ref_id OR c.native_attachment_id=r.bound_native_attachment_id THEN 0 ELSE 1 END, c.candidate_attachment_id",
                (chat_id, base_key),
            ).mappings().all()
        by_ordinal: dict[int, list[dict[str, object]]] = {}
        for row in rows:
            by_ordinal.setdefault(int(row["ordinal"]), []).append(dict(row))
        materialized: list[str] = []
        used: set[str] = set()
        for requirement in readiness.requirements:
            if requirement.ordinal in excluded_ordinals:
                continue
            candidates = by_ordinal.get(requirement.ordinal, [])
            if requirement.binding_kind == "identity":
                candidates = [row for row in candidates if (
                    row["imported_ref_id"] == requirement.imported_ref_id
                    or row["native_attachment_id"] == requirement.native_attachment_id
                )]
            chosen = next((row for row in candidates if str(row["attachment_id"]) not in used), None)
            if chosen is None:
                raise StateError("imported continuation lacks distinct READY attachment candidates")
            attachment_id = str(chosen["attachment_id"])
            used.add(attachment_id)
            materialized.append(attachment_id)
        return tuple(materialized)

    def _inherit_continuation_anchor(
        self, connection, *, chat_id: str, source_base_key: str, choice_revision: int,
        assistant_message_id: str, attempt_id: str, attachment_ids: tuple[str, ...], created_at: str,
    ) -> None:
        """Append a native child anchor from one admitted branch choice.

        The original anchor and its immutable source configuration remain
        untouched.  The child owns a copied, append-only local choice and
        native requirement projection, so later sends never need to mutate a
        chat-global selection or reinterpret imported history.
        """
        inherited = connection.exec_driver_sql(
            "SELECT a.source_configuration,c.local_connection_id,c.local_model_entry_id,"
            "c.explicit_settings,c.degraded,c.excluded_refs,c.decision_kind,c.safe_target_descriptor "
            "FROM archive_continuation_anchors a JOIN archive_continuation_choices c "
            "ON c.chat_id=a.chat_id AND c.base_key=a.base_key AND c.choice_revision=? "
            "WHERE a.chat_id=? AND a.base_key=?",
            (choice_revision, chat_id, source_base_key),
        ).mappings().one_or_none()
        if inherited is None:
            raise StateError("continuation child lacks its admitted source choice")
        connection.exec_driver_sql(
            "INSERT INTO archive_continuation_anchors(chat_id,base_key,base_message_id,revision,source_configuration,resolution,resolution_evidence) VALUES (?,?,?,?,?,'UNRESOLVED',?)",
            (chat_id, assistant_message_id, assistant_message_id, 1,
             inherited["source_configuration"],
             json.dumps({"kind":"native-child","source_base_key":source_base_key,"source_choice_revision":choice_revision}, sort_keys=True, separators=(",", ":"))),
        )
        connection.exec_driver_sql(
            "INSERT INTO archive_continuation_choices(chat_id,base_key,choice_revision,local_connection_id,local_model_entry_id,explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, assistant_message_id, 1, inherited["local_connection_id"],
             inherited["local_model_entry_id"], inherited["explicit_settings"], inherited["degraded"],
             inherited["excluded_refs"], inherited["decision_kind"], inherited["safe_target_descriptor"], created_at),
        )
        for ordinal, attachment_id in enumerate(attachment_ids):
            attachment = connection.exec_driver_sql(
                "SELECT a.blob_digest,b.byte_size,a.text_digest FROM attachments a "
                "JOIN attachment_blobs b ON b.digest=a.blob_digest AND b.state='ready' WHERE a.id=?",
                (attachment_id,),
            ).first()
            if attachment is None or attachment[2] is None:
                raise StateError("native continuation source attachment is not ready and text-eligible")
            connection.exec_driver_sql(
                "INSERT INTO archive_continuation_requirements(chat_id,base_key,ordinal,source_imported_attempt_id,source_native_attempt_id,expected_digest,expected_size,representation_digest,binding_kind,bound_native_attachment_id,bound_imported_ref_id) VALUES (?,?,?,NULL,?,?,?,?,'identity',?,NULL)",
                (chat_id, assistant_message_id, ordinal, attempt_id, attachment[0], int(attachment[1]), attachment[2], attachment_id),
            )
            connection.exec_driver_sql(
                "INSERT INTO archive_continuation_requirement_candidates(chat_id,base_key,ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id) VALUES (?,?,?,?,?,NULL)",
                (chat_id, assistant_message_id, ordinal, attachment_id, attachment_id),
            )

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
                            healed_ref_ids = self._heal_imported_attachment_references(
                                connection, captured, created_at
                            )
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
                                frozenset(
                                    {f"attachment:{attachment_id}"}
                                    | {f"attachment:{ref_id}" for ref_id in healed_ref_ids}
                                ),
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
                        healed_ref_ids = self._heal_imported_attachment_references(
                            connection, captured, created_at
                        )
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
                        frozenset(
                            {f"attachment:{attachment_id}"}
                            | {f"attachment:{ref_id}" for ref_id in healed_ref_ids}
                        ),
                    )
                )
            except BaseException as exc:
                self._poisoned = True
                self._authority.poison("attachment publication failed after durable staging")
                raise StateError(
                    "attachment publication failed; restart recovery is required"
                ) from exc
        return self._attachment_from_authoritative_row(row)

    def _heal_imported_attachment_references(
        self, connection, captured, healed_at: str
    ) -> frozenset[str]:
        """Turn every exact missing external reservation into its reserved attachment.

        A missing-external archive owns a stable local attachment identity before
        bytes exist.  Once ordinary intake has independently captured and
        verified matching bytes, healing must use that identity rather than
        manufacture a replacement.  This runs in the same source mutation and
        SQLite transaction as the verified intake, so observers cannot see a
        ready reference without its native attachment row.
        """
        contradiction = connection.exec_driver_sql(
            "SELECT 1 FROM archive_import_attachment_refs "
            "WHERE expected_digest=? AND expected_size<>? LIMIT 1",
            (bytes(captured.digest), captured.byte_size),
        ).first()
        if contradiction is not None:
            raise StateError("imported references contradict a digest size identity")
        rows = connection.exec_driver_sql(
            "SELECT id,source_metadata FROM archive_import_attachment_refs "
            "WHERE expected_digest=? AND expected_size=? "
            "AND availability='MISSING_EXTERNAL' ORDER BY id",
            (bytes(captured.digest), captured.byte_size),
        ).fetchall()
        prepared: list[tuple[str, str]] = []
        for ref_id, source_metadata in rows:
            try:
                metadata = json.loads(str(source_metadata))
            except (TypeError, ValueError) as exc:
                raise StateError("imported attachment reservation metadata is corrupt") from exc
            if not isinstance(metadata, dict):
                raise StateError("imported attachment reservation metadata is corrupt")
            filename = metadata.get("filename")
            if (not isinstance(filename, str)
                    or metadata.get("filename_status") not in {"available", "redacted"}):
                filename = captured.filename
            prepared.append((str(ref_id), filename))
        if not prepared:
            return frozenset()
        arm_phase9_attachment_healing(
            connection, tuple(ref_id for ref_id, _filename in prepared)
        )
        healed_ids: set[str] = set()
        try:
            for ref_id, filename in prepared:
                arm_phase6_attachment_insert(connection, ref_id)
                try:
                    result = connection.execute(
                        insert(attachments).values(
                            id=ref_id,
                            blob_digest=captured.digest,
                            filename=filename,
                            source_kind="imported",
                            source_name=filename,
                            text_representation_id=captured.representation_id,
                            text_digest=captured.representation_id,
                            ineligibility_reason=captured.ineligibility_reason,
                            created_at=healed_at,
                        )
                    )
                    require_phase6_consumed(connection)
                    if result.rowcount != 1:
                        raise StateError("imported attachment healing did not affect one row")
                finally:
                    clear_phase6(connection)
                result = connection.exec_driver_sql(
                    "UPDATE archive_import_attachment_refs "
                    "SET attachment_id=id,availability='READY',healed_at=? "
                    "WHERE id=? AND availability='MISSING_EXTERNAL' AND attachment_id IS NULL",
                    (healed_at, ref_id),
                )
                if result.rowcount != 1:
                    raise StateError("imported attachment reservation changed during healing")
                healed_ids.add(ref_id)
        finally:
            clear_phase9_attachment_healing(connection)
        return frozenset(healed_ids)

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
                imported_ref = connection.exec_driver_sql(
                    "SELECT 1 FROM archive_import_attachment_refs WHERE attachment_id=? LIMIT 1",
                    (attachment_id,),
                ).first()
                if refs is not None or attempt_ref is not None or imported_ref is not None:
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
            links = connection.exec_driver_sql(
                "SELECT attachment_id,ordinal FROM message_attachments WHERE message_id=? "
                "UNION ALL "
                "SELECT r.attachment_id,im.ordinal "
                "FROM archive_import_message_attachment_refs im "
                "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
                "WHERE im.message_id=? AND r.availability='READY' "
                "ORDER BY ordinal,attachment_id",
                (message_id, message_id),
            ).fetchall()
            identifiers = tuple(str(row[0]) for row in links)
            if not identifiers:
                return ()
            rows = connection.execute(
                select(attachments)
                .add_columns(attachment_blobs.c.byte_size)
                .select_from(
                    attachments
                    .join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
                .where(attachments.c.id.in_(identifiers))
            ).fetchall()
        by_id = {str(row._mapping["id"]): self._attachment_from_authoritative_row(row) for row in rows}
        return tuple(by_id[identifier] for identifier in identifiers)

    def list_attempt_attachments(self, attempt_id: str) -> tuple[Attachment, ...]:
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            links = connection.exec_driver_sql(
                "SELECT attachment_id,ordinal FROM attempt_attachments WHERE attempt_id=? "
                "UNION ALL "
                "SELECT r.attachment_id,ia.ordinal "
                "FROM archive_import_attempt_attachment_refs ia "
                "JOIN archive_import_attachment_refs r ON r.id=ia.attachment_ref_id "
                "WHERE ia.attempt_id=? AND r.availability='READY' "
                "ORDER BY ordinal,attachment_id",
                (attempt_id, attempt_id),
            ).fetchall()
            identifiers = tuple(str(row[0]) for row in links)
            if not identifiers:
                return ()
            rows = connection.execute(
                select(attachments)
                .add_columns(attachment_blobs.c.byte_size)
                .select_from(
                    attachments
                    .join(
                        attachment_blobs,
                        attachments.c.blob_digest == attachment_blobs.c.digest,
                    )
                )
                .where(attachments.c.id.in_(identifiers))
            ).fetchall()
        by_id = {str(row._mapping["id"]): self._attachment_from_authoritative_row(row) for row in rows}
        return tuple(by_id[identifier] for identifier in identifiers)

    def list_message_attachment_metadata(self, message_id: str) -> tuple[Attachment, ...]:
        """Read only persisted attachment metadata for the Inspector.

        Payload verification remains mandatory for attachment-consuming paths.
        Inspection deliberately reports only metadata that the durable schema
        already binds to a message and must not open object bytes.
        """
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            links = connection.exec_driver_sql(
                "SELECT attachment_id,ordinal FROM message_attachments WHERE message_id=? "
                "UNION ALL "
                "SELECT r.attachment_id,im.ordinal "
                "FROM archive_import_message_attachment_refs im "
                "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
                "WHERE im.message_id=? AND r.availability='READY' "
                "ORDER BY ordinal,attachment_id",
                (message_id, message_id),
            ).fetchall()
            identifiers = tuple(str(row[0]) for row in links)
            if not identifiers:
                return ()
            rows = connection.execute(
                select(attachments)
                .where(attachments.c.id.in_(identifiers))
            ).fetchall()
            by_id = {str(row._mapping["id"]): _attachment(row) for row in rows}
        return tuple(by_id[identifier] for identifier in identifiers)

    def list_attempt_attachment_metadata(self, attempt_id: str) -> tuple[Attachment, ...]:
        """Read only persisted attempt-attachment metadata for the Inspector."""
        self._ensure_open()
        with self._authority.operation(), self._engine.connect() as connection:
            links = connection.exec_driver_sql(
                "SELECT attachment_id,ordinal FROM attempt_attachments WHERE attempt_id=? "
                "UNION ALL "
                "SELECT r.attachment_id,ia.ordinal "
                "FROM archive_import_attempt_attachment_refs ia "
                "JOIN archive_import_attachment_refs r ON r.id=ia.attachment_ref_id "
                "WHERE ia.attempt_id=? AND r.availability='READY' "
                "ORDER BY ordinal,attachment_id",
                (attempt_id, attempt_id),
            ).fetchall()
            identifiers = tuple(str(row[0]) for row in links)
            if not identifiers:
                return ()
            rows = connection.execute(
                select(attachments)
                .where(attachments.c.id.in_(identifiers))
            ).fetchall()
            by_id = {str(row._mapping["id"]): _attachment(row) for row in rows}
        return tuple(by_id[identifier] for identifier in identifiers)

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
                    ).first() is not None or connection.exec_driver_sql(
                        "SELECT 1 FROM archive_import_payload_reservations AS r "
                        "JOIN archive_import_operations AS o ON o.id=r.operation_id "
                        "WHERE r.digest=? AND r.size=? AND o.state IN ('STAGING','COMMITTING') LIMIT 1",
                        (row.digest, int(row.byte_size)),
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
                .where(text(
                    "NOT EXISTS (SELECT 1 FROM archive_import_payload_reservations AS r "
                    "JOIN archive_import_operations AS o ON o.id=r.operation_id "
                    "WHERE r.digest=attachment_blobs.digest "
                    "AND r.size=attachment_blobs.byte_size "
                    "AND o.state IN ('STAGING','COMMITTING'))"
                ))
            ).fetchall()

    def _reconcile_attachments_startup(self) -> tuple[str, ...]:
        """Private pre-READY recovery; never exposed as a second manager."""
        with self._authority._transition_gate:
            receipts = self._reconcile_attachments_locked()
        for receipt in receipts:
            self._accept_search_receipt(receipt)
        return ()

    def _reconcile_attachments_locked(self) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(select(attachment_blobs)).fetchall()
        try:
            self._recover_attachment_rows(rows)
            self._validate_attachment_payload_rows()
            receipt = self._heal_ready_imported_attachment_refs_startup()
            return () if receipt is None else (receipt,)
        except AttachmentIntegrityError as exc:
            raise StateError(str(exc)) from exc

    def _heal_ready_imported_attachment_refs_startup(self) -> SearchReceipt | None:
        """Heal only durable ready identities already indexed in SQLite.

        Startup never searches a resolver path.  It reads the exact ready
        object once through its rooted descriptor, hash-verifies it, and derives
        the existing Phase 6 text classification before reusing the ordinary
        guarded healing transaction.
        """
        with self._engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT r.id,r.expected_digest,r.expected_size,r.source_metadata "
                "FROM archive_import_attachment_refs r JOIN attachment_blobs b "
                "ON b.digest=r.expected_digest AND b.byte_size=r.expected_size "
                "WHERE r.availability='MISSING_EXTERNAL' AND r.attachment_id IS NULL "
                "AND b.state='ready' ORDER BY r.expected_digest,r.expected_size,r.id"
            ).fetchall()
        captures: list[CapturedAttachment] = []
        seen: set[tuple[bytes, int]] = set()
        for _ref_id, digest, size, source_metadata in rows:
            identity = (bytes(digest), int(size))
            if identity in seen:
                continue
            seen.add(identity)
            try:
                metadata = json.loads(str(source_metadata))
            except (TypeError, ValueError) as exc:
                raise StateError("imported attachment reservation metadata is corrupt") from exc
            if not isinstance(metadata, dict):
                raise StateError("imported attachment reservation metadata is corrupt")
            with self._engine.connect() as backing_connection:
                backing = backing_connection.execute(
                    select(attachments).where(attachments.c.blob_digest == identity[0])
                    .order_by(attachments.c.id).limit(1)
                ).first()
            if backing is None:
                classification = self._attachment_manager.classify_verified_object(
                    identity[0], expected_size=identity[1]
                )
                fallback_filename = metadata.get("filename")
                if (
                    metadata.get("filename_status") != "available"
                    or not isinstance(fallback_filename, str)
                ):
                    fallback_filename = "imported-attachment"
                filename = fallback_filename
                representation_id = classification.representation_id
                ineligibility_reason = classification.ineligibility_reason
            else:
                self._attachment_manager.verify_object(
                    identity[0], expected_size=identity[1]
                )
                filename = str(backing.filename)
                representation_id = backing.text_representation_id
                ineligibility_reason = backing.ineligibility_reason
            captures.append(CapturedAttachment(
                identity[0], identity[1], "startup-imported-healing", filename,
                None, representation_id, ineligibility_reason,
            ))
        if not captures:
            return None
        connection = self._engine.connect()
        committed = False
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            self._arm_phase7_source_mutation(connection, "startup imported attachment healing")
            try:
                healed_ids: set[str] = set()
                for captured in captures:
                    healed_ids.update(self._heal_imported_attachment_references(
                        connection, captured, utc_iso(datetime.now(UTC)),
                    ))
                revision = self._require_phase7_source_consumed(connection)
            finally:
                clear_phase7_source_mutation(connection)
            self._commit_attachment_transaction(connection, "startup imported attachment healing")
            committed = True
        except BaseException:
            if not committed:
                self._rollback_attachment_transaction(connection, "startup imported attachment healing")
            raise
        finally:
            self._close_attachment_connection(connection, "startup imported attachment healing")
        return SearchReceipt(
            revision, frozenset(f"attachment:{ref_id}" for ref_id in healed_ids),
        )

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

    def read_chat_export_source(self, chat_id: str, *, attachment_policy):
        """Read one complete export source projection from a single SQLite cut."""
        from bots5.core.export import ChatExportSource, ExportAttachment

        self._ensure_open()
        with self._authority.transition():
            with self._engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                try:
                    chat_row = connection.execute(
                        select(chats).where(chats.c.id == chat_id)
                    ).first()
                    if chat_row is None:
                        connection.rollback()
                        return ChatExportSource(
                            chat=None, messages=(), attempts=(),
                            message_attachments={}, attempt_attachments={},
                            context_plans={}, chat_configuration={},
                            captured_at=datetime.now(UTC),
                        )
                    chat = _chat(chat_row)
                    message_rows = connection.execute(
                        select(messages).where(messages.c.chat_id == chat_id)
                        .order_by(messages.c.sequence.asc(), messages.c.id.asc())
                    ).fetchall()
                    source_messages = tuple(_message(row) for row in message_rows)
                    attempt_rows = connection.execute(
                        select(
                            generation_attempts,
                            messages.c.id.label("_attempt_user_message_id"),
                            messages.c.content.label("_attempt_user_message_content"),
                        ).select_from(
                            generation_attempts.outerjoin(
                                messages, messages.c.id == generation_attempts.c.user_message_id
                            )
                        ).where(generation_attempts.c.chat_id == chat_id)
                        .order_by(generation_attempts.c.started_at.asc(), generation_attempts.c.id.asc())
                    ).fetchall()
                    source_attempts = []
                    for row in attempt_rows:
                        mapping = row._mapping
                        if mapping["_attempt_user_message_id"] is None:
                            raise StateError(
                                "generation attempt user message not found: " + str(mapping["id"])
                            )
                        source_attempts.append(
                            _attempt(row, mapping["_attempt_user_message_content"])
                        )
                    source_attempts = tuple(source_attempts)
                    imported_attempt_rows = connection.exec_driver_sql(
                        "SELECT id,chat_id,user_message_id,assistant_message_id,state,started_at,ended_at,source_attempt "
                        "FROM archive_imported_attempts WHERE chat_id=? ORDER BY started_at,id",
                        (chat_id,),
                    ).mappings().all()
                    imported_chat = connection.exec_driver_sql(
                        "SELECT 1 FROM archive_import_chats WHERE chat_id=?", (chat_id,)
                    ).first()
                    imported_attempts = []
                    archived_attempt_provenance: dict[str, dict[str, object]] = {}
                    for imported in imported_attempt_rows:
                        try:
                            source_attempt = json.loads(str(imported["source_attempt"]))
                        except (TypeError, ValueError) as exc:
                            raise StateError("imported attempt evidence is malformed") from exc
                        if not isinstance(source_attempt, dict):
                            raise StateError("imported attempt evidence is malformed")
                        provenance = source_attempt.get("request_time_provenance")
                        if not isinstance(provenance, dict):
                            raise StateError("imported attempt provenance is malformed")
                        archived_attempt_provenance[str(imported["id"])] = provenance
                        state = AttemptState(str(imported["state"]))
                        imported_attempts.append(GenerationAttempt(
                            str(imported["id"]), str(imported["chat_id"]),
                            str(imported["user_message_id"]), str(imported["assistant_message_id"]),
                            str(source_attempt.get("backend_id", "[unavailable]")),
                            str(source_attempt.get("model", "[unavailable]")), state, "{}",
                            parse_utc(str(imported["started_at"])),
                            ended_at=parse_utc(str(imported["ended_at"])),
                            provider_id=(None if source_attempt.get("provider_id") is None else str(source_attempt["provider_id"])),
                            returned_model=(None if source_attempt.get("returned_model") is None else str(source_attempt["returned_model"])),
                            finish_reason=(None if source_attempt.get("finish_reason") is None else str(source_attempt["finish_reason"])),
                            remote_outcome_unknown=bool(source_attempt.get("remote_outcome_unknown", False)),
                        ))
                    source_attempts = tuple(sorted(
                        (*source_attempts, *imported_attempts),
                        key=lambda item: (item.started_at, item.id),
                    ))
                    message_ids = tuple(item.id for item in source_messages)
                    attempt_ids = tuple(item.id for item in source_attempts)

                    def attachment_rows(relation, owner_column, owner_ids):
                        if not owner_ids:
                            return []
                        rows = connection.execute(
                            select(
                                owner_column.label("_export_owner_id"),
                                attachments, attachment_blobs.c.byte_size,
                                attachment_blobs.c.state.label("_export_blob_state"),
                            ).select_from(
                                relation.join(attachments, relation.c.attachment_id == attachments.c.id)
                                .join(attachment_blobs, attachments.c.blob_digest == attachment_blobs.c.digest)
                            ).where(owner_column.in_(owner_ids))
                            .order_by(owner_column.asc(), relation.c.ordinal.asc(), attachments.c.id.asc())
                        ).fetchall()
                        return [(str(row._mapping["_export_owner_id"]), row) for row in rows]

                    message_attachment_rows = attachment_rows(
                        message_attachments, message_attachments.c.message_id, message_ids
                    )
                    imported_message_links = connection.exec_driver_sql(
                        "SELECT im.message_id,im.ordinal,r.id AS ref_id,r.attachment_id,r.source_metadata "
                        "FROM archive_import_message_attachment_refs im "
                        "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
                        "JOIN archive_import_messages m ON m.message_id=im.message_id "
                        "WHERE m.chat_id=? AND r.availability='READY' ORDER BY im.message_id,im.ordinal",
                        (chat_id,),
                    ).mappings().all()
                    missing_imported_message_links = connection.exec_driver_sql(
                        "SELECT im.message_id,im.ordinal,r.id,r.expected_digest,r.expected_size,r.source_metadata,r.created_at "
                        "FROM archive_import_message_attachment_refs im "
                        "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
                        "JOIN archive_import_messages m ON m.message_id=im.message_id "
                        "WHERE m.chat_id=? AND r.availability='MISSING_EXTERNAL' "
                        "ORDER BY im.message_id,im.ordinal",
                        (chat_id,),
                    ).mappings().all()
                    attempt_attachment_rows = attachment_rows(
                        attempt_attachments, attempt_attachments.c.attempt_id, attempt_ids
                    )
                    imported_attempt_links = connection.exec_driver_sql(
                        "SELECT ia.attempt_id,ia.ordinal,r.id AS ref_id,r.attachment_id,r.source_metadata "
                        "FROM archive_import_attempt_attachment_refs ia "
                        "JOIN archive_import_attachment_refs r ON r.id=ia.attachment_ref_id "
                        "JOIN archive_imported_attempts a ON a.id=ia.attempt_id "
                        "WHERE a.chat_id=? AND r.availability='READY' ORDER BY ia.attempt_id,ia.ordinal",
                        (chat_id,),
                    ).mappings().all()
                    missing_imported_attempt_links = connection.exec_driver_sql(
                        "SELECT ia.attempt_id,ia.ordinal,r.id,r.expected_digest,r.expected_size,r.source_metadata,r.created_at "
                        "FROM archive_import_attempt_attachment_refs ia "
                        "JOIN archive_import_attachment_refs r ON r.id=ia.attachment_ref_id "
                        "JOIN archive_imported_attempts a ON a.id=ia.attempt_id "
                        "WHERE a.chat_id=? AND r.availability='MISSING_EXTERNAL' "
                        "ORDER BY ia.attempt_id,ia.ordinal",
                        (chat_id,),
                    ).mappings().all()
                    imported_attachment_ids = tuple(str(row["attachment_id"]) for row in imported_attempt_links)
                    if imported_attachment_ids:
                        imported_attachment_rows = connection.execute(
                            select(attachments, attachment_blobs.c.byte_size,
                                   attachment_blobs.c.state.label("_export_blob_state"))
                            .select_from(attachments.join(
                                attachment_blobs, attachments.c.blob_digest == attachment_blobs.c.digest,
                            )).where(attachments.c.id.in_(imported_attachment_ids))
                        ).fetchall()
                        by_imported_attachment = {
                            str(row._mapping["id"]): row for row in imported_attachment_rows
                        }
                    imported_message_attachment_ids = tuple(str(row["attachment_id"]) for row in imported_message_links)
                    if imported_message_attachment_ids:
                        imported_message_attachment_rows = connection.execute(
                            select(attachments, attachment_blobs.c.byte_size,
                                   attachment_blobs.c.state.label("_export_blob_state"))
                            .select_from(attachments.join(
                                attachment_blobs, attachments.c.blob_digest == attachment_blobs.c.digest,
                            )).where(attachments.c.id.in_(imported_message_attachment_ids))
                        ).fetchall()
                        by_imported_message_attachment = {
                            str(row._mapping["id"]): row for row in imported_message_attachment_rows
                        }
                    imported_backing_rows = [
                        by_imported_attachment[str(link["attachment_id"])]
                        for link in imported_attempt_links
                    ] + [
                        by_imported_message_attachment[str(link["attachment_id"])]
                        for link in imported_message_links
                    ]
                    attachments_by_id = {}
                    for _, row in (*message_attachment_rows, *attempt_attachment_rows, *((None, item) for item in imported_backing_rows)):
                        attachment_id = str(row._mapping["id"])
                        if attachment_id in attachments_by_id:
                            continue
                        attachment = _attachment(row)
                        size = int(row._mapping["byte_size"])
                        raw = None
                        status = "missing" if row._mapping["_export_blob_state"] != "ready" else "verified"
                        if status == "verified":
                            try:
                                raw = self._attachment_manager.read_verified(
                                    row.blob_digest, expected_size=size
                                )
                                self._validated_authoritative_attachment_representation(
                                    row, attachment_id, raw=raw
                                )
                            except AttachmentIntegrityError:
                                status = "integrity-failed"
                                raw = None
                        if attachment_policy.value == "embedded" and status != "verified":
                            raise StateError("authoritative attachment payload is unavailable")
                        attachments_by_id[attachment_id] = ExportAttachment(
                            attachment=attachment, byte_size=size, integrity_status=status,
                            payload=raw if attachment_policy.value == "embedded" else None,
                        )

                    def sealed_attachment_metadata(row):
                        try:
                            metadata = json.loads(str(row["source_metadata"]))
                        except (TypeError, ValueError) as exc:
                            raise StateError("imported attachment reservation metadata is malformed") from exc
                        if not isinstance(metadata, dict):
                            raise StateError("imported attachment reservation metadata is malformed")
                        return metadata

                    def missing_external_attachment(row):
                        """Project a reserved imported ref without inventing a backing.

                        The reservation itself is the exporting graph identity.
                        Its sealed safe source metadata supplies the only
                        attachment facts that may cross another v2 hop.
                        """
                        metadata = sealed_attachment_metadata(row)
                        try:
                            created_at = parse_utc(str(metadata.get("created_at", row["created_at"])))
                        except (TypeError, ValueError) as exc:
                            raise StateError("imported attachment reservation timestamp is malformed") from exc
                        digest = bytes(row["expected_digest"]).hex()
                        attachment = Attachment(
                            # These placeholders never cross the wire: the
                            # ExportAttachment below writes `metadata` exactly.
                            id=str(row["id"]), blob_digest=digest, filename="imported-attachment",
                            source_kind="filesystem", source_name="imported-attachment",
                            text_representation_id=(
                                str(metadata["text_representation_id"])
                                if isinstance(metadata.get("text_representation_id"), str) else None
                            ),
                            text_digest=(
                                str(metadata["text_digest"])
                                if isinstance(metadata.get("text_digest"), str) else None
                            ),
                            ineligibility_reason=(
                                str(metadata["ineligibility_reason"])
                                if isinstance(metadata.get("ineligibility_reason"), str) else None
                            ),
                            created_at=created_at,
                        )
                        return ExportAttachment(
                            attachment=attachment, byte_size=int(row["expected_size"]),
                            integrity_status="missing-external", payload=None, source_metadata=metadata,
                        )

                    for row in (*missing_imported_message_links, *missing_imported_attempt_links):
                        ref_id = str(row["id"])
                        if ref_id not in attachments_by_id:
                            attachments_by_id[ref_id] = missing_external_attachment(row)
                    for row in (*imported_message_links, *imported_attempt_links):
                        ref_id = str(row["ref_id"])
                        if ref_id in attachments_by_id:
                            attachments_by_id[ref_id] = replace(
                                attachments_by_id[ref_id], source_metadata=sealed_attachment_metadata(row),
                            )
                    used_imported_refs = connection.exec_driver_sql(
                        "SELECT r.id,r.source_metadata FROM archive_import_attachment_refs r WHERE r.id IN ("
                        "SELECT ma.attachment_id FROM message_attachments ma JOIN messages m ON m.id=ma.message_id WHERE m.chat_id=? "
                        "UNION SELECT aa.attachment_id FROM attempt_attachments aa JOIN generation_attempts a ON a.id=aa.attempt_id WHERE a.chat_id=?"
                        ")",
                        (chat_id, chat_id),
                    ).mappings().all()
                    for row in used_imported_refs:
                        ref_id = str(row["id"])
                        if ref_id in attachments_by_id:
                            attachments_by_id[ref_id] = replace(
                                attachments_by_id[ref_id], source_metadata=sealed_attachment_metadata(row),
                            )
                    message_attachment_rows.extend(
                        (str(row["message_id"]), attachments_by_id[str(row["ref_id"] if "ref_id" in row.keys() else row["id"])])
                        for row in sorted(
                            (*imported_message_links, *missing_imported_message_links),
                            key=lambda item: (str(item["message_id"]), int(item["ordinal"])),
                        )
                    )
                    attempt_attachment_rows.extend(
                        (str(row["attempt_id"]), attachments_by_id[str(row["ref_id"] if "ref_id" in row.keys() else row["id"])])
                        for row in sorted(
                            (*imported_attempt_links, *missing_imported_attempt_links),
                            key=lambda item: (str(item["attempt_id"]), int(item["ordinal"])),
                        )
                    )

                    def grouped(rows):
                        result = {}
                        for owner, row in rows:
                            item = row if isinstance(row, ExportAttachment) else attachments_by_id[str(row._mapping["id"])]
                            result.setdefault(owner, []).append(item)
                        return {key: tuple(value) for key, value in result.items()}

                    context_rows = connection.execute(
                        select(context_plans).where(context_plans.c.attempt_id.in_(attempt_ids))
                        if attempt_ids else select(context_plans).where(text("1 = 0"))
                    ).fetchall()
                    context_source = {
                        str(row._mapping["attempt_id"]): {
                            key: row._mapping[key] for key in (
                                "attempt_id", "plan_version", "canonical_representation",
                                "canonical_digest", "wire_representation_digest", "budget_limit",
                                "budget_provenance", "budget_semantics", "adapter_id",
                                "adapter_version", "input_counts", "envelope_overhead",
                                "output_reserve", "input_units", "total_units", "headroom",
                                "created_at",
                            )
                        }
                        for row in context_rows
                    }
                    imported_context_rows = connection.exec_driver_sql(
                        "SELECT a.id,c.source_plan FROM archive_imported_context_plans c "
                        "JOIN archive_imported_attempts a ON a.id=c.attempt_id WHERE a.chat_id=?",
                        (chat_id,),
                    ).mappings().all()
                    for row in imported_context_rows:
                        try:
                            source_context = json.loads(str(row["source_plan"]))
                        except (TypeError, ValueError) as exc:
                            raise StateError("imported context evidence is malformed") from exc
                        if not isinstance(source_context, dict):
                            raise StateError("imported context evidence is malformed")
                        source_context = dict(source_context)
                        source_context["attempt_id"] = str(row["id"])
                        context_source[str(row["id"])] = source_context
                    selection_row = connection.execute(
                        select(chat_model_selection).where(chat_model_selection.c.chat_id == chat_id)
                    ).first()
                    override_rows = connection.execute(
                        select(chat_model_generation_config).where(
                            chat_model_generation_config.c.chat_id == chat_id
                        ).order_by(chat_model_generation_config.c.model_entry_id.asc())
                    ).fetchall()
                    model_ids = {str(row._mapping["model_entry_id"]) for row in override_rows}
                    if selection_row is not None and selection_row._mapping["model_entry_id"] is not None:
                        model_ids.add(str(selection_row._mapping["model_entry_id"]))
                    model_rows = connection.execute(
                        select(
                            model_catalogue_entries.c.id.label("model_entry_id"),
                            model_catalogue_entries.c.connection_id,
                            model_catalogue_entries.c.provider_model_id,
                            model_catalogue_entries.c.display_name,
                            model_catalogue_entries.c.origin,
                            model_catalogue_entries.c.availability,
                            model_catalogue_entries.c.revision.label("model_revision"),
                            provider_connections.c.backend_type,
                            provider_connections.c.profile,
                            provider_connections.c.revision.label("connection_revision"),
                            provider_connections.c.catalogue_revision,
                        ).select_from(
                            model_catalogue_entries.join(
                                provider_connections,
                                model_catalogue_entries.c.connection_id == provider_connections.c.id,
                            )
                        ).where(model_catalogue_entries.c.id.in_(model_ids))
                    ).fetchall() if model_ids else ()
                    descriptors = {
                        str(row._mapping["model_entry_id"]): {
                            "source_model_entry_id": str(row._mapping["model_entry_id"]),
                            "source_connection_id": str(row._mapping["connection_id"]),
                            "backend_type": str(row._mapping["backend_type"]),
                            "provider_profile": str(row._mapping["profile"]),
                            "provider_model_id": str(row._mapping["provider_model_id"]),
                            "display_name": str(row._mapping["display_name"]),
                            "origin": str(row._mapping["origin"]),
                            "availability": str(row._mapping["availability"]),
                            "model_revision": int(row._mapping["model_revision"]),
                            "connection_revision": int(row._mapping["connection_revision"]),
                            "catalogue_revision": int(row._mapping["catalogue_revision"]),
                        } for row in model_rows
                    }
                    if set(model_ids) != set(descriptors):
                        raise StateError("chat continuation model metadata is unavailable")
                    overrides = []
                    for row in override_rows:
                        mapping = row._mapping
                        settings = _settings(mapping)
                        model_id = str(mapping["model_entry_id"])
                        overrides.append({
                            "model": descriptors[model_id],
                            "revision": int(mapping["revision"]),
                            "temperature": settings.temperature,
                            "max_output_tokens": settings.max_output_tokens,
                            "reasoning_effort": settings.reasoning_effort,
                            "timeout_seconds": settings.timeout_seconds,
                        })
                    selection = None
                    if selection_row is not None:
                        mapping = selection_row._mapping
                        selected_id = mapping["model_entry_id"]
                        selection = {
                            "model": None if selected_id is None else descriptors[str(selected_id)],
                            "selection_required": bool(mapping["selection_required"]),
                            "revision": int(mapping["revision"]),
                        }
                    imported_nodes = connection.exec_driver_sql(
                        "SELECT 'chat' AS kind,chat_id AS local_id,source_node_id FROM archive_import_chats WHERE chat_id=? "
                        "UNION ALL SELECT 'message',message_id,source_node_id FROM archive_import_messages WHERE chat_id=? "
                        "UNION ALL SELECT 'lineage',local_lineage_id,source_node_id FROM archive_import_lineages WHERE chat_id=? "
                        "UNION ALL SELECT 'attachment',r.id,r.source_node_id FROM archive_import_attachment_refs r "
                        "WHERE r.id IN ("
                        "SELECT ml.attachment_ref_id FROM archive_import_message_attachment_refs ml "
                        "JOIN archive_import_messages im ON im.message_id=ml.message_id WHERE im.chat_id=? "
                        "UNION SELECT al.attachment_ref_id FROM archive_import_attempt_attachment_refs al "
                        "JOIN archive_imported_attempts ia ON ia.id=al.attempt_id WHERE ia.chat_id=? "
                        "UNION "
                        "SELECT ma.attachment_id FROM message_attachments ma JOIN messages m ON m.id=ma.message_id WHERE m.chat_id=? "
                        "UNION SELECT aa.attachment_id FROM attempt_attachments aa JOIN generation_attempts a ON a.id=aa.attempt_id WHERE a.chat_id=?"
                        ") "
                        "UNION ALL SELECT 'attempt',id,source_node_id FROM archive_imported_attempts WHERE chat_id=?",
                        (chat_id, chat_id, chat_id, chat_id, chat_id, chat_id, chat_id, chat_id),
                    ).mappings().all()
                    node_by_identity = {
                        (str(row["kind"]), str(row["local_id"])): str(row["source_node_id"])
                        for row in imported_nodes
                    }
                    derivation_by_identity = {
                        (str(row["object_kind"]), str(row["object_id"])): str(row["predecessor_message_id"])
                        for row in connection.exec_driver_sql(
                            "SELECT object_kind,object_id,predecessor_message_id "
                            "FROM archive_object_derivations WHERE chat_id=?",
                            (chat_id,),
                        ).mappings().all()
                    }
                    node_cache = {}
                    def source_chain(node_id):
                        chain = []
                        while node_id is not None:
                            node = node_cache.get(node_id)
                            if node is None:
                                node = connection.exec_driver_sql(
                                    "SELECT id,archive_id,logical_content_digest,source_object_id,imported_at,source_format,prior_node_id "
                                    "FROM archive_lineage_nodes WHERE id=?", (node_id,),
                                ).mappings().one_or_none()
                                if node is None:
                                    raise StateError("imported provenance node is missing")
                                node_cache[node_id] = node
                            chain.append(node)
                            node_id = None if node["prior_node_id"] is None else str(node["prior_node_id"])
                        return chain
                    identities = [("chat", chat.id)]
                    identities.extend(("message", item.id) for item in source_messages)
                    identities.extend(("lineage", item.lineage_id) for item in source_messages)
                    identities.extend(("attachment", item) for item in sorted(attachments_by_id))
                    identities.extend(("attempt", item.id) for item in source_attempts)
                    provenance_rows = []
                    for kind, local_id in dict.fromkeys(identities):
                        node_id = node_by_identity.get((kind, local_id))
                        if node_id is None:
                            source = {"kind": "native", "immediate": None, "prior_chain": []}
                        else:
                            chain = source_chain(node_id)
                            def hop(node):
                                return {"archive_id": str(node["archive_id"]), "archive_version": int(node["source_format"]), "logical_content_digest": str(node["logical_content_digest"]), "object_id": str(node["source_object_id"]), "imported_at": str(node["imported_at"])}
                            source = {
                                "kind": "v1-bootstrap" if len(chain) == 1 and int(chain[0]["source_format"]) == 1 else "imported",
                                "immediate": hop(chain[0]), "prior_chain": [hop(item) for item in chain[1:]],
                            }
                        predecessor_id = derivation_by_identity.get((kind, local_id))
                        derivation = (
                            {"kind": "root", "predecessor": None}
                            if predecessor_id is None else {
                                "kind": "local-continuation",
                                "predecessor": {"object_kind": "message", "object_id": predecessor_id},
                            }
                        )
                        provenance_rows.append({"object_kind": kind, "object_id": local_id, "source": source, "derivation": derivation})
                    anchor_rows = connection.exec_driver_sql(
                        "SELECT base_key,base_message_id,source_configuration,resolution,resolution_evidence "
                        "FROM archive_continuation_anchors WHERE chat_id=? ORDER BY base_key",
                        (chat_id,),
                    ).mappings().all()
                    continuation_anchors = []
                    for anchor in anchor_rows:
                        try:
                            configuration = json.loads(str(anchor["source_configuration"]))
                            evidence = json.loads(str(anchor["resolution_evidence"]))
                        except (TypeError, ValueError) as exc:
                            raise StateError("continuation anchor evidence is malformed") from exc
                        if not isinstance(configuration, dict) or not isinstance(evidence, dict):
                            raise StateError("continuation anchor evidence is malformed")
                        continuation_anchors.append({
                            "anchor_key": str(anchor["base_key"]),
                            "base_message_id": None if anchor["base_message_id"] is None else str(anchor["base_message_id"]),
                            "source_configuration": configuration,
                            "resolution": str(anchor["resolution"]).lower().replace("_", "-"),
                            "resolution_reason": str(evidence.get("kind", "source")),
                        })
                    choice_rows = connection.exec_driver_sql(
                        "SELECT base_key,choice_revision,decision_kind,safe_target_descriptor,explicit_settings,excluded_refs,created_at "
                        "FROM archive_continuation_choices WHERE chat_id=? ORDER BY base_key,choice_revision",
                        (chat_id,),
                    ).mappings().all()
                    continuation_choices = []
                    for choice in choice_rows:
                        try:
                            descriptor = json.loads(str(choice["safe_target_descriptor"]))
                            settings = json.loads(str(choice["explicit_settings"]))
                            exclusions = json.loads(str(choice["excluded_refs"]))
                        except (TypeError, ValueError) as exc:
                            raise StateError("continuation choice evidence is malformed") from exc
                        if not isinstance(descriptor, dict) or not isinstance(settings, dict) or not isinstance(exclusions, list):
                            raise StateError("continuation choice evidence is malformed")
                        continuation_choices.append({
                            "anchor_key": str(choice["base_key"]), "choice_revision": int(choice["choice_revision"]),
                            "decision_kind": str(choice["decision_kind"]).replace("_", "-"),
                            "mapped_model": descriptor, "explicit_settings": settings,
                            "excluded_context_refs": exclusions, "chosen_at": str(choice["created_at"]),
                        })
                    branch_rows = connection.exec_driver_sql(
                        "SELECT base_key,choice_revision,first_message_id,attempt_id,created_at "
                        "FROM archive_continuation_branches WHERE chat_id=? ORDER BY first_message_id",
                        (chat_id,),
                    ).mappings().all()
                    imported_branch_rows = connection.exec_driver_sql(
                        "SELECT base_key,first_message_id,attempt_id,choice_snapshot "
                        "FROM archive_imported_branch_choices WHERE chat_id=? ORDER BY first_message_id",
                        (chat_id,),
                    ).mappings().all()
                    imported_branches = []
                    for branch in imported_branch_rows:
                        try:
                            snapshot = json.loads(str(branch["choice_snapshot"]))
                        except (TypeError, ValueError) as exc:
                            raise StateError("imported branch history is malformed") from exc
                        if (
                            not isinstance(snapshot, dict)
                            or set(snapshot) != {"anchor_key", "choice_revision", "first_message_id", "attempt_id", "created_at"}
                            or type(snapshot.get("choice_revision")) is not int
                            or not isinstance(snapshot.get("created_at"), str)
                        ):
                            raise StateError("imported branch history is malformed")
                        # An archive names this exporting graph's identities.
                        # The persisted snapshot remains the prior source
                        # record; it cannot be recast as a native branch.
                        imported_branches.append({
                            "anchor_key": str(branch["base_key"]),
                            "choice_revision": int(snapshot["choice_revision"]),
                            "first_message_id": str(branch["first_message_id"]),
                            "attempt_id": str(branch["attempt_id"]),
                            "created_at": snapshot["created_at"],
                        })
                    continuation_history = {
                        "active_head_message_id": chat.head_message_id,
                        "anchors": continuation_anchors,
                        "choices": continuation_choices,
                        "branches": [{
                            "anchor_key": str(branch["base_key"]), "choice_revision": int(branch["choice_revision"]),
                            "first_message_id": str(branch["first_message_id"]), "attempt_id": str(branch["attempt_id"]),
                            "created_at": str(branch["created_at"]),
                        } for branch in branch_rows] + imported_branches,
                    }
                    history_bindings = []
                    imported_binding_map = {
                        "attachment": {
                            str(source_id): str(local_id)
                            for source_id, local_id in connection.exec_driver_sql(
                                "SELECT r.source_attachment_id,r.id FROM archive_import_attachment_refs r "
                                "JOIN archive_import_chats c ON c.operation_id=r.operation_id WHERE c.chat_id=?",
                                (chat_id,),
                            ).fetchall()
                        },
                        "message": {
                            str(source_id): str(local_id)
                            for source_id, local_id in connection.exec_driver_sql(
                                "SELECT source_message_id,message_id FROM archive_import_messages WHERE chat_id=?",
                                (chat_id,),
                            ).fetchall()
                        },
                    }
                    for imported in imported_attempt_rows:
                        try:
                            bindings = json.loads(str(connection.exec_driver_sql(
                                "SELECT source_evidence_binding FROM archive_imported_attempts WHERE id=?", (imported["id"],)
                            ).scalar_one()))
                        except (TypeError, ValueError) as exc:
                            raise StateError("imported history bindings are malformed") from exc
                        if not isinstance(bindings, list):
                            raise StateError("imported history bindings are malformed")
                        for binding in bindings:
                            if not isinstance(binding, dict):
                                raise StateError("imported history bindings are malformed")
                            exported = json.loads(json.dumps(binding))
                            current = exported.get("current_binding")
                            if current is not None:
                                if (
                                    not isinstance(current, dict)
                                    or set(current) != {"object_kind", "object_id"}
                                    or not isinstance(current["object_kind"], str)
                                    or not isinstance(current["object_id"], str)
                                ):
                                    raise StateError("imported history current binding is malformed")
                                mapped = imported_binding_map.get(
                                    current["object_kind"], {}
                                ).get(current["object_id"])
                                if mapped is None:
                                    raise StateError("imported history current binding lacks a typed source map")
                                exported["current_binding"] = {
                                    "object_kind": current["object_kind"], "object_id": mapped,
                                }
                            exported["attempt_id"] = str(imported["id"])
                            history_bindings.append(exported)
                    native_binding_rows = connection.exec_driver_sql(
                        "SELECT aa.attempt_id,aa.ordinal,aa.attachment_id,hex(a.text_digest) AS text_digest "
                        "FROM attempt_attachments aa JOIN generation_attempts ga ON ga.id=aa.attempt_id "
                        "JOIN attachments a ON a.id=aa.attachment_id WHERE ga.chat_id=? "
                        "ORDER BY aa.attempt_id,aa.ordinal",
                        (chat_id,),
                    ).mappings().all()
                    native_attachments = {
                        (str(row["attempt_id"]), str(row["attachment_id"])): str(row["text_digest"]).lower()
                        for row in native_binding_rows if row["text_digest"] is not None
                    }
                    messages_by_id = {message.id: message for message in source_messages}
                    imported_attempt_ids = {str(row["id"]) for row in imported_attempt_rows}
                    from bots5.core.export import _archive_snapshot
                    for attempt in source_attempts:
                        if attempt.id in imported_attempt_ids:
                            continue
                        provenance = _archive_snapshot(
                            attempt, messages_by_id[attempt.user_message_id].content,
                        )
                        if provenance.get("status") != "available" or provenance.get("snapshot_version") != 3:
                            continue
                        context = provenance.get("context")
                        sources = context.get("sources") if isinstance(context, dict) else None
                        if not isinstance(sources, list):
                            raise StateError("native safe context evidence is malformed")
                        for ordinal, source in enumerate(sources):
                            if not isinstance(source, dict):
                                raise StateError("native safe context source is malformed")
                            kind, source_id, status = source.get("kind"), source.get("source_id"), source.get("source_id_status")
                            if not isinstance(kind, str) or not isinstance(source_id, str) or status not in {"available", "redacted"}:
                                raise StateError("native safe context source is malformed")
                            if kind == "attachment":
                                digest = source.get("representation_digest"); digest_kind = "representation"
                            else:
                                digest = hashlib.sha256(canonical_json_bytes({key: source.get(key) for key in (
                                    "kind", "role", "content", "state", "eligible", "selected",
                                    "selection_reason", "representation_id", "representation_digest",
                                )})).hexdigest(); digest_kind = "safe-source-record"
                            if not isinstance(digest, str):
                                raise StateError("native safe context digest is malformed")
                            current = None
                            if status == "available":
                                if kind == "attachment" and (attempt.id, source_id) in native_attachments:
                                    if native_attachments[(attempt.id, source_id)] != digest.lower():
                                        raise StateError("native attachment contradicts frozen safe context")
                                    current = {"object_kind": "attachment", "object_id": source_id}
                                elif kind == "current_user" and source_id == attempt.user_message_id and source.get("content") == messages_by_id[source_id].content:
                                    current = {"object_kind": "message", "object_id": source_id}
                                elif kind == "history" and source_id in messages_by_id and source.get("role") == messages_by_id[source_id].role.value and source.get("content") == messages_by_id[source_id].content:
                                    current = {"object_kind": "message", "object_id": source_id}
                            history_bindings.append({
                                "attempt_id": attempt.id, "binding_kind": "context-source", "ordinal": ordinal,
                                "snapshot_source": {"kind": kind, "source_id": source_id,
                                    "source_id_status": status, "evidence_digest": digest.lower(), "digest_kind": digest_kind},
                                "current_binding": current,
                                "binding_state": "bound" if current is not None else (
                                    "historical-only" if kind == "bots_instruction" else "unavailable"),
                            })
                    history_bindings.sort(key=lambda item: (str(item["attempt_id"]), int(item["ordinal"])))
                    result = ChatExportSource(
                        chat=chat, messages=source_messages, attempts=source_attempts,
                        message_attachments=grouped(message_attachment_rows),
                        attempt_attachments=grouped(attempt_attachment_rows),
                        context_plans=context_source,
                        chat_configuration={
                            "semantic": "inert-continuation-hints",
                            "selection": selection, "overrides": overrides,
                        },
                        captured_at=datetime.now(UTC),
                        requires_v2=imported_chat is not None or bool(imported_nodes),
                        object_provenance=tuple(provenance_rows),
                        continuation_history=continuation_history,
                        history_bindings=tuple(history_bindings),
                        archived_attempt_provenance=archived_attempt_provenance,
                    )
                    connection.rollback()
                    return result
                except BaseException:
                    connection.rollback()
                    raise

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

    def inspection_import_provenance(
        self, chat_id: str, message_id: str | None = None,
    ) -> dict[str, str]:
        """Read imported identity evidence only; Inspector never opens bytes."""
        self._ensure_open()
        with self._engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT o.archive_id,o.archive_version,o.logical_content_digest,"
                "i.source_chat_id,i.source_chat_revision,i.imported_at "
                "FROM archive_import_chats AS i JOIN archive_import_operations AS o ON o.id=i.operation_id "
                "WHERE i.chat_id=?", (chat_id,),
            ).mappings().one_or_none()
            if row is None:
                return {}
            result = {
                "Archive ID": str(row["archive_id"]),
                "Archive version": str(row["archive_version"]),
                "Archive digest": str(row["logical_content_digest"]),
                "Source chat ID": str(row["source_chat_id"]),
                "Source chat revision": str(row["source_chat_revision"]),
                "Imported at": str(row["imported_at"]),
            }
            if message_id is not None:
                message = connection.exec_driver_sql(
                    "SELECT source_message_id FROM archive_import_messages WHERE message_id=? AND chat_id=?",
                    (message_id, chat_id),
                ).scalar_one_or_none()
                if message is not None:
                    result["Source message ID"] = str(message)
            return result

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
        attempt: GenerationAttempt, branch_choice_context=None,
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
        _validate_phase6_attempt_authority(connection, attempt, branch_choice_context)
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
        imported_regeneration: bool = False,
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
                elif not imported_regeneration:
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
        continuation_branch: tuple[str, int] | None = None,
        continuation_first_message_id: str | None = None,
        derivation_predecessor_message_id: str | None = None,
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
                    if continuation_branch is not None:
                        base_key, choice_revision = continuation_branch
                        source = connection.exec_driver_sql(
                            "SELECT 1 FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=?",
                            (chat.id, base_key, choice_revision),
                        ).first()
                        if source is None:
                            raise StateError("continuation generation lacks its selected anchor and choice")
                    self._insert_messages_and_attempt(
                        connection,
                        (user_message, assistant_message),
                        attempt, continuation_branch,
                    )
                    if continuation_branch is not None:
                        base_key, choice_revision = continuation_branch
                        first_message_id = continuation_first_message_id or assistant_message.id
                        if base_key == "empty":
                            raise StateError("continuation derivation lacks an earlier message")
                        predecessor_message_id = derivation_predecessor_message_id or base_key
                        derivations = (
                            (chat.id, "message", first_message_id, predecessor_message_id),
                            (chat.id, "attempt", attempt.id, predecessor_message_id),
                        )
                        arm_phase9_object_derivations(connection, derivations)
                        for derivation in derivations:
                            connection.exec_driver_sql(
                                "INSERT INTO archive_object_derivations(chat_id,object_kind,object_id,predecessor_message_id) VALUES (?,?,?,?)",
                                derivation,
                            )
                        connection.exec_driver_sql(
                            "INSERT INTO archive_continuation_branches(chat_id,base_key,choice_revision,first_message_id,attempt_id,created_at) VALUES (?,?,?,?,?,?)",
                            (chat.id, base_key, choice_revision, first_message_id, attempt.id, utc_iso(assistant_message.created_at)),
                        )
                    self._persist_phase6_evidence(
                        connection,
                        attempt_id=attempt.id,
                        message_id=user_message.id,
                        context_plan=context_plan,
                        attachment_ids=attachment_ids,
                    )
                    if continuation_branch is not None:
                        self._inherit_continuation_anchor(
                            connection, chat_id=chat.id, source_base_key=continuation_branch[0],
                            choice_revision=continuation_branch[1], assistant_message_id=assistant_message.id,
                            attempt_id=attempt.id, attachment_ids=attachment_ids,
                            created_at=utc_iso(assistant_message.created_at),
                        )
                    self._advance_chat(
                        connection,
                        chat,
                        assistant_message.id,
                        expected_chat_revision,
                    )
                    source_revision = self._require_phase7_source_consumed(connection)
                finally:
                    clear_phase9_object_derivations(connection)
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
        imported_regeneration: tuple[str, int] | None = None,
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
                    if imported_regeneration is not None:
                        base_key, choice_revision = imported_regeneration
                        source = connection.exec_driver_sql(
                            "SELECT ia.id FROM archive_imported_attempts ia "
                            "JOIN archive_continuation_anchors a ON a.chat_id=ia.chat_id AND a.base_key=? "
                            "JOIN archive_continuation_choices c ON c.chat_id=a.chat_id AND c.base_key=a.base_key AND c.choice_revision=? "
                            "WHERE ia.chat_id=? AND ia.assistant_message_id=? AND ia.user_message_id=?",
                            (base_key, choice_revision, chat.id, base_key, attempt.user_message_id),
                        ).first()
                        if source is None:
                            raise StateError("imported regeneration lacks its selected source anchor and choice")
                    self._insert_messages_and_attempt(
                        connection, (assistant_message,), attempt, imported_regeneration
                    )
                    if imported_regeneration is not None:
                        base_key, choice_revision = imported_regeneration
                        if base_key == "empty":
                            raise StateError("continuation derivation lacks an earlier message")
                        derivations = (
                            (chat.id, "message", assistant_message.id, base_key),
                            (chat.id, "attempt", attempt.id, base_key),
                        )
                        arm_phase9_object_derivations(connection, derivations)
                        for derivation in derivations:
                            connection.exec_driver_sql(
                                "INSERT INTO archive_object_derivations(chat_id,object_kind,object_id,predecessor_message_id) VALUES (?,?,?,?)",
                                derivation,
                            )
                        connection.exec_driver_sql(
                            "INSERT INTO archive_continuation_branches(chat_id,base_key,choice_revision,first_message_id,attempt_id,created_at) VALUES (?,?,?,?,?,?)",
                            (chat.id, base_key, choice_revision, assistant_message.id, attempt.id, utc_iso(assistant_message.created_at)),
                        )
                    self._persist_phase6_evidence(
                        connection,
                        attempt_id=attempt.id,
                        message_id=attempt.user_message_id,
                        context_plan=context_plan,
                        attachment_ids=attachment_ids,
                        reuse_message_attachments=True,
                        imported_regeneration=imported_regeneration is not None,
                    )
                    if imported_regeneration is not None:
                        self._inherit_continuation_anchor(
                            connection, chat_id=chat.id, source_base_key=imported_regeneration[0],
                            choice_revision=imported_regeneration[1], assistant_message_id=assistant_message.id,
                            attempt_id=attempt.id, attachment_ids=attachment_ids,
                            created_at=utc_iso(assistant_message.created_at),
                        )
                    self._advance_chat(
                        connection,
                        chat,
                        assistant_message.id,
                        expected_chat_revision,
                    )
                    source_revision = self._require_phase7_source_consumed(connection)
                finally:
                    clear_phase9_object_derivations(connection)
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
        predicates = ["location.attachment_id=?"]
        parameters: list[object] = [attachment_id]
        if filters.chat_id is not None:
            predicates.append("m.chat_id=?")
            parameters.append(filters.chat_id)
        if not filters.include_archived:
            predicates.append("c.archived_at IS NULL")
        sql = (
            "SELECT m.chat_id, m.id, c.archived_at FROM ("
            "SELECT attachment_id,message_id FROM message_attachments "
            "UNION ALL SELECT r.attachment_id,im.message_id "
            "FROM archive_import_message_attachment_refs im "
            "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
            "WHERE r.availability='READY') location "
            "JOIN messages m ON m.id=location.message_id JOIN chats c ON c.id=m.chat_id "
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
                    "SELECT 1 FROM attachments a JOIN ("
                    "SELECT attachment_id,message_id FROM message_attachments "
                    "UNION ALL SELECT r.attachment_id,im.message_id "
                    "FROM archive_import_message_attachment_refs im "
                    "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
                    "WHERE r.availability='READY') loc "
                    "ON loc.attachment_id=a.id JOIN messages m ON m.id=loc.message_id "
                    "WHERE a.id=? AND loc.message_id=? AND m.chat_id=?",
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
                    inspector_open=state.inspector_open,
                    inspector_message_id=state.inspector_message_id,
                    inspector_leaf_message_id=state.inspector_leaf_message_id,
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
    "read_chat_export_source",
    "list_message_attachments",
    "list_attempt_attachments",
    "list_message_attachment_metadata",
    "list_attempt_attachment_metadata",
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
