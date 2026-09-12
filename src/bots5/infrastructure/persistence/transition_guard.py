from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bots5.core.secrets import is_forbidden_secret_key
from bots5.core.urls import canonical_http_base_url

from .phase3_validation import (
    valid_outcome_fields,
    valid_remote_outcome_transition,
    valid_request_snapshot,
)


_STATE_KEY = "bots5_transition_guard"


def install_transition_guard(dbapi_connection: Any, connection_record: Any | None) -> None:
    state = {
        "phase": None,
        "message_id": None,
        "attempt_id": None,
        "user_message_id": None,
        "phase5_connection_identity_update": None,
        "phase5_catalogue_refresh": None,
        "phase6": None,
        "phase6_consumed": False,
        "phase7_source": None,
        "phase7_expected_source_revision": None,
        "phase7_consumed": False,
        "phase7_source_updated": False,
    }

    def consume_phase6(expected: tuple[object, ...]) -> int:
        if state["phase6"] != expected or state["phase6_consumed"]:
            return 0
        state["phase6_consumed"] = True
        return 1

    def internal_transition(message_id: str | None, attempt_id: str | None, phase: str) -> int:
        if state["phase"] == "start":
            if phase == "start-user":
                return int(state["user_message_id"] == message_id)
            return int(
                phase in {"start-message", "start-attempt"}
                and state["message_id"] == message_id
                and (
                    phase == "start-message"
                    or state["attempt_id"] == attempt_id
                )
            )
        if state["phase"] == "finalize":
            return int(
                phase in {"finalize-message", "finalize-attempt"}
                and state["message_id"] == message_id
                and (
                    phase == "finalize-message"
                    or state["attempt_id"] == attempt_id
                )
            )
        if state["phase"] == "advance":
            return int(
                phase == "advance-chat"
                and state["message_id"] == message_id
                and state["attempt_id"] == attempt_id
            )
        return 0

    def valid_timestamp(value: object) -> int:
        if not isinstance(value, str):
            return 0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return 0
        canonical = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        return int(canonical == value)

    def valid_cost(value: object) -> int:
        if value is None:
            return 1
        if isinstance(value, bool):
            return 0
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return 0
        return int(parsed.is_finite() and parsed >= 0)

    def valid_phase5_settings(
        temperature: object,
        max_output_tokens: object,
        reasoning_effort: object,
        timeout_seconds: object,
        temperature_required: object,
        max_output_required: object,
    ) -> int:
        try:
            if temperature is None:
                if bool(temperature_required):
                    return 0
            else:
                parsed_temperature = Decimal(str(temperature))
                if not parsed_temperature.is_finite() or not 0 <= parsed_temperature <= 2:
                    return 0
            if max_output_tokens is None:
                if bool(max_output_required):
                    return 0
            elif (
                type(max_output_tokens) is not int
                or max_output_tokens < 1
            ):
                return 0
            if reasoning_effort is not None and reasoning_effort != "none":
                return 0
            if timeout_seconds is not None:
                parsed_timeout = Decimal(str(timeout_seconds))
                if not parsed_timeout.is_finite() or parsed_timeout <= 0:
                    return 0
        except (InvalidOperation, TypeError, ValueError):
            return 0
        return 1

    def valid_phase5_connection(
        name: object,
        name_key: object,
        backend_type: object,
        profile: object,
        endpoint: object,
        credential_source: object,
        credential_reference: object,
        enabled: object,
        retired: object,
        revision: object,
        catalogue_revision: object,
    ) -> int:
        if (
            type(name) is not str
            or not name.strip()
            or " ".join(name.split()) != name
            or type(name_key) is not str
            or name_key != name.casefold()
            or type(enabled) is not int
            or enabled not in {0, 1}
            or type(retired) is not int
            or retired not in {0, 1}
            or type(revision) is not int
            or revision < 1
            or type(catalogue_revision) is not int
            or catalogue_revision < 0
            or (retired == 1 and enabled != 0)
        ):
            return 0
        if backend_type == "fake":
            if (
                profile != "generic"
                or endpoint is not None
                or credential_source != "none"
                or credential_reference is not None
            ):
                return 0
        elif backend_type == "openai_compatible_http":
            if profile not in {"generic", "openrouter"}:
                return 0
            if profile == "openrouter" and credential_source == "none":
                return 0
            try:
                if canonical_http_base_url(endpoint) != endpoint:
                    return 0
            except Exception:
                return 0
        else:
            return 0
        if credential_source == "none":
            return int(credential_reference is None)
        if type(credential_reference) is not str or not credential_reference:
            return 0
        if credential_source == "environment":
            return int(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential_reference) is not None)
        return int(credential_source == "secret_service")

    dbapi_connection.create_function("bots5_internal_transition", 3, internal_transition)
    dbapi_connection.create_function("bots5_valid_timestamp", 1, valid_timestamp)
    dbapi_connection.create_function("bots5_valid_cost", 1, valid_cost)
    dbapi_connection.create_function("bots5_valid_phase5_settings", 6, valid_phase5_settings)
    dbapi_connection.create_function("bots5_valid_phase5_connection", 11, valid_phase5_connection)
    dbapi_connection.create_function(
        "bots5_secret_key_forbidden",
        1,
        lambda value: int(is_forbidden_secret_key(value)),
    )
    dbapi_connection.create_function(
        "bots5_phase5_connection_identity_update_allowed",
        5,
        lambda connection_id, old_revision, new_revision, old_catalogue_revision, new_catalogue_revision: int(
            state["phase5_connection_identity_update"]
            == (connection_id, old_revision, old_catalogue_revision)
            and new_revision == old_revision + 1
            and new_catalogue_revision == old_catalogue_revision + 1
        ),
    )
    dbapi_connection.create_function(
        "bots5_phase5_catalogue_refresh_allowed",
        4,
        lambda connection_id, old_catalogue_revision, old_refresh_revision, new_revision: int(
            state["phase5_catalogue_refresh"]
            == (connection_id, old_catalogue_revision, old_refresh_revision, new_revision)
        ),
    )
    dbapi_connection.create_function("bots5_valid_request_snapshot", 8, valid_request_snapshot)
    dbapi_connection.create_function("bots5_valid_request_snapshot", 9, valid_request_snapshot)
    dbapi_connection.create_function("bots5_valid_outcome_fields", 14, valid_outcome_fields)
    dbapi_connection.create_function(
        "bots5_valid_remote_outcome_transition", 6, valid_remote_outcome_transition
    )
    dbapi_connection.create_function(
        "bots5_phase6_blob_transition_allowed", 7,
        lambda digest, old_state, new_state, operation_id, stage_name, gc_id, byte_size: consume_phase6(
            (
                "blob", bytes(digest), old_state or "", new_state,
                operation_id or "", stage_name or "", gc_id or "", byte_size,
            )
        ),
    )
    dbapi_connection.create_function(
        "bots5_phase6_blob_delete_allowed", 2,
        lambda digest, gc_id: consume_phase6(
            ("blob-delete", bytes(digest), gc_id or "")
        ),
    )
    dbapi_connection.create_function(
        "bots5_phase6_attachment_insert_allowed", 1,
        lambda attachment_id: consume_phase6(("attachment-insert", attachment_id)),
    )
    dbapi_connection.create_function(
        "bots5_phase6_attachment_delete_allowed",
        1,
        lambda attachment_id: consume_phase6(("attachment-delete", attachment_id)),
    )
    dbapi_connection.create_function(
        "bots5_phase7_source_mutation_allowed",
        0,
        lambda: int(state["phase7_source"] is not None),
    )

    def phase7_source_revision_update_allowed(
        old_singleton_id: object,
        new_singleton_id: object,
        old_revision: object,
        new_revision: object,
    ) -> int:
        expected = state["phase7_expected_source_revision"]
        if (
            state["phase7_source"] is None
            or type(expected) is not int
            or type(old_singleton_id) is not int
            or type(new_singleton_id) is not int
            or old_singleton_id != 1
            or new_singleton_id != 1
            or type(old_revision) is not int
            or type(new_revision) is not int
            or not state["phase7_consumed"]
        ):
            return 0
        if not state["phase7_source_updated"]:
            if old_revision != expected or new_revision != expected + 1:
                return 0
            state["phase7_source_updated"] = True
            return 1
        # One authoritative transaction may touch several search-visible rows.
        # Later business triggers execute a guarded no-op after the first one
        # has consumed the transaction's single revision increment.
        return int(old_revision == new_revision == expected + 1)

    dbapi_connection.create_function(
        "bots5_phase7_source_revision_update_allowed",
        4,
        phase7_source_revision_update_allowed,
    )

    def consume_phase7_source_revision() -> int:
        if state["phase7_source"] is None:
            return 0
        if state["phase7_consumed"]:
            return 0
        state["phase7_consumed"] = True
        return 1

    dbapi_connection.create_function(
        "bots5_phase7_consume_source_revision",
        0,
        consume_phase7_source_revision,
    )
    if connection_record is not None:
        connection_record.info[_STATE_KEY] = state


def arm_transition(
    connection: Any,
    message_id: str,
    attempt_id: str,
    phase: str,
    *,
    user_message_id: str | None = None,
) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state.update(
        {
            "phase": phase,
            "message_id": message_id,
            "attempt_id": attempt_id,
            "user_message_id": user_message_id,
        }
    )


def clear_transition(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is not None:
        state.update(
            {
                "phase": None,
                "message_id": None,
                "attempt_id": None,
                "user_message_id": None,
            }
        )


def arm_phase6_blob_transition(
    connection: Any,
    digest: bytes,
    old_state: str,
    new_state: str,
    *,
    operation_id: str | None = None,
    stage_name: str | None = None,
    gc_id: str | None = None,
    byte_size: int,
) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state["phase6"] = (
        "blob", bytes(digest), old_state, new_state, operation_id or "",
        stage_name or "", gc_id or "", byte_size,
    )
    state["phase6_consumed"] = False


def arm_phase6_blob_delete(connection: Any, digest: bytes, gc_id: str) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state["phase6"] = ("blob-delete", bytes(digest), gc_id)
    state["phase6_consumed"] = False


def arm_phase6_attachment_insert(connection: Any, attachment_id: str) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state["phase6"] = ("attachment-insert", attachment_id)
    state["phase6_consumed"] = False


def arm_phase6_attachment_delete(connection: Any, attachment_id: str) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state["phase6"] = ("attachment-delete", attachment_id)
    state["phase6_consumed"] = False


def require_phase6_consumed(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None or not state["phase6_consumed"]:
        raise RuntimeError("SQLite Phase 6 transition arm was not consumed exactly once")


def clear_phase6(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is not None:
        state["phase6"] = None
        state["phase6_consumed"] = False


def arm_phase7_source_mutation(
    connection: Any,
    operation: str,
    *,
    expected_revision: int | None = None,
) -> None:
    """Authorize one search-visible authoritative transaction.

    Phase 7 triggers consume the arm at most once no matter how many relevant
    rows the transaction changes.  Callers must clear the arm on both commit
    and rollback paths so pooled connections cannot inherit authority.
    """
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    if state["phase7_source"] is not None:
        raise RuntimeError("SQLite Phase 7 source mutation is already armed")
    if expected_revision is None:
        expected_revision = connection.exec_driver_sql(
            "SELECT source_revision FROM search_source_state WHERE singleton_id=1"
        ).scalar_one()
    if type(expected_revision) is not int or expected_revision < 0:
        raise RuntimeError("SQLite Phase 7 source revision is malformed")
    state["phase7_source"] = operation
    state["phase7_expected_source_revision"] = expected_revision
    state["phase7_consumed"] = False
    state["phase7_source_updated"] = False


def require_phase7_consumed(connection: Any) -> int:
    state = connection.info.get(_STATE_KEY)
    if (
        state is None
        or not state["phase7_consumed"]
        or not state["phase7_source_updated"]
        or type(state["phase7_expected_source_revision"]) is not int
    ):
        raise RuntimeError("SQLite Phase 7 source arm was not consumed")
    revision = connection.exec_driver_sql(
        "SELECT source_revision FROM search_source_state WHERE singleton_id=1"
    ).scalar_one()
    if (
        type(revision) is not int
        or revision != state["phase7_expected_source_revision"] + 1
    ):
        raise RuntimeError("SQLite Phase 7 source revision is malformed")
    return revision


def clear_phase7_source_mutation(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is not None:
        state["phase7_source"] = None
        state["phase7_expected_source_revision"] = None
        state["phase7_consumed"] = False
        state["phase7_source_updated"] = False


def arm_phase5_connection_identity_update(
    connection: Any,
    connection_id: str,
    expected_revision: int,
    expected_catalogue_revision: int,
) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state["phase5_connection_identity_update"] = (
        connection_id,
        expected_revision,
        expected_catalogue_revision,
    )


def clear_phase5_connection_identity_update(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is not None:
        state["phase5_connection_identity_update"] = None


def arm_phase5_catalogue_refresh(
    connection: Any,
    connection_id: str,
    expected_catalogue_revision: int,
    expected_refresh_revision: int,
    new_revision: int,
) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is None:
        raise RuntimeError("SQLite transition guard is not installed")
    state["phase5_catalogue_refresh"] = (
        connection_id,
        expected_catalogue_revision,
        expected_refresh_revision,
        new_revision,
    )


def clear_phase5_catalogue_refresh(connection: Any) -> None:
    state = connection.info.get(_STATE_KEY)
    if state is not None:
        state["phase5_catalogue_refresh"] = None
