from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

from bots5.core.urls import canonical_http_base_url
from bots5.domain.provider import (
    CAPABILITY_KEYS,
    PHASE5_SNAPSHOT_VERSION,
    CapabilityState,
    CredentialSource,
    validate_capability_value,
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_KEYS = frozenset(
    {
        "apikey",
        "apikeyvalue",
        "apitoken",
        "accesstoken",
        "authorization",
        "clientsecret",
        "password",
        "refreshtoken",
        "secret",
        "secretvalue",
        "token",
    }
)


def _normalise_secret_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())
_SNAPSHOT_KEYS = frozenset(
    {
        "snapshot_version",
        "attempt_id",
        "chat_id",
        "user_message_id",
        "backend_id",
        "provider_id",
        "provider_profile",
        "connection_id",
        "connection_name",
        "connection_revision",
        "endpoint",
        "credential_source",
        "credential_reference",
        "credential_status",
        "model_entry_id",
        "model",
        "catalogue_revision",
        "prompt",
        "effective_settings",
        "settings_provenance",
        "capabilities",
        "capability_provenance",
        "manual_overrides",
        "omitted_settings",
    }
)
_REQUIRED_KEYS = _SNAPSHOT_KEYS
_SETTING_KEYS = frozenset(
    {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}
)
_CAPABILITY_SOURCES = frozenset(
    {"manual", "confirmed_endpoint", "provider_metadata", "trusted_registry", "heuristic", "unknown"}
)
_CAPABILITY_STATES = frozenset(item.value for item in CapabilityState)
_PROVENANCE_KEYS = frozenset({"source", "reason", "field", "catalogue_revision"})


def object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Phase 5 request snapshot contains duplicate keys")
        result[key] = value
    return result


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and _normalise_secret_key(key) in _FORBIDDEN_KEYS)
            or _contains_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _require_string(snapshot: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = snapshot.get(key)
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"Phase 5 snapshot field {key} is invalid")
    return value


def _require_nonnegative_int(snapshot: Mapping[str, object], key: str) -> int:
    value = snapshot.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"Phase 5 snapshot field {key} is invalid")
    return value


def validate_phase5_snapshot(
    request_snapshot: object,
    *,
    attempt_id: object,
    chat_id: object,
    user_message_id: object,
    backend_id: object,
    model: object,
    provider_id: object,
    user_message_content: object | None = None,
    allow_branch_settings_provenance: bool = False,
) -> dict[str, object]:
    try:
        snapshot = json.loads(
            request_snapshot,
            object_pairs_hook=object_without_duplicate_keys,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Phase 5 request snapshot must be valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("Phase 5 request snapshot must be a JSON object")
    if _contains_forbidden_key(snapshot):
        raise ValueError("Phase 5 request snapshot must not contain secret values")
    if set(snapshot) != _SNAPSHOT_KEYS:
        raise ValueError("Phase 5 request snapshot fields are not the closed schema")
    if snapshot["snapshot_version"] != PHASE5_SNAPSHOT_VERSION:
        raise ValueError("unsupported Phase 5 request snapshot version")
    expected = {
        "attempt_id": attempt_id,
        "chat_id": chat_id,
        "user_message_id": user_message_id,
        "backend_id": backend_id,
        "model": model,
    }
    for key, value in expected.items():
        if snapshot.get(key) != value:
            raise ValueError(f"Phase 5 request snapshot contradicts {key}")
    if type(provider_id) not in {str, type(None)} or snapshot["provider_id"] != provider_id:
        raise ValueError("Phase 5 request snapshot contradicts provider_id")
    provider_profile = _require_string(snapshot, "provider_profile")
    if provider_profile not in {"generic", "openrouter"}:
        raise ValueError("Phase 5 provider profile is invalid")
    if backend_id == "fake":
        if provider_id is not None or provider_profile != "generic":
            raise ValueError("fake Phase 5 requests have no legacy provider ID")
        if snapshot["endpoint"] is not None:
            raise ValueError("fake Phase 5 requests cannot have an endpoint")
    elif backend_id == "openai_compatible_http":
        if provider_id != provider_profile:
            raise ValueError("Phase 5 provider profile and legacy provider ID disagree")
        endpoint = snapshot["endpoint"]
        try:
            canonical = canonical_http_base_url(
                endpoint,
                error_type=ValueError,
                error_message="Phase 5 endpoint is invalid",
            )
        except Exception:
            raise ValueError("Phase 5 endpoint is invalid") from None
        if endpoint != canonical:
            raise ValueError("Phase 5 endpoint is not normalized")
    else:
        raise ValueError("Phase 5 backend ID is invalid")

    for key in ("connection_id", "connection_name", "model_entry_id", "model"):
        _require_string(snapshot, key)
    _require_nonnegative_int(snapshot, "connection_revision")
    _require_nonnegative_int(snapshot, "catalogue_revision")
    _require_string(snapshot, "prompt", allow_empty=True)
    if user_message_content is not None and snapshot["prompt"] != user_message_content:
        raise ValueError("Phase 5 request snapshot prompt is not the current user message")

    credential_source = snapshot["credential_source"]
    if credential_source not in {item.value for item in CredentialSource}:
        raise ValueError("Phase 5 credential source is invalid")
    reference = snapshot["credential_reference"]
    if reference is not None and (type(reference) is not str or not reference):
        raise ValueError("Phase 5 credential reference is invalid")
    if credential_source == CredentialSource.ENVIRONMENT.value and reference is not None:
        if _ENVIRONMENT_NAME.fullmatch(reference) is None:
            raise ValueError("Phase 5 environment credential reference is invalid")
    if credential_source == CredentialSource.NONE.value and reference is not None:
        raise ValueError("Phase 5 credential reference is invalid")
    if snapshot["credential_status"] not in {
        "not_configured", "available", "missing", "unavailable", "error"
    }:
        raise ValueError("Phase 5 credential status is invalid")
    if credential_source == CredentialSource.NONE.value:
        if snapshot["credential_status"] != "not_configured":
            raise ValueError("Phase 5 credential status contradicts its source")
    elif snapshot["credential_status"] == "not_configured":
        raise ValueError("Phase 5 credential status contradicts its reference")
    if credential_source != CredentialSource.NONE.value and reference is None:
        raise ValueError("Phase 5 credential source requires a reference")
    if provider_profile == "openrouter" and credential_source == CredentialSource.NONE.value:
        raise ValueError("Phase 5 OpenRouter requests require a credential source")

    settings = snapshot["effective_settings"]
    provenance = snapshot["settings_provenance"]
    if not isinstance(settings, dict) or set(settings) != _SETTING_KEYS:
        raise ValueError("Phase 5 effective settings are invalid")
    if not isinstance(provenance, dict) or set(provenance) != _SETTING_KEYS:
        raise ValueError("Phase 5 settings provenance is invalid")
    allowed_provenance = {"application", "model", "chat_model"}
    # Archive continuation has an additive v3 admission path.  Its immutable,
    # branch-owned override is validated by the current Phase 6 caller and
    # persisted choice CAS; plain v2 validation never enables this vocabulary.
    if allow_branch_settings_provenance:
        allowed_provenance.add("branch")
    if any(type(value) is not str or value not in allowed_provenance for value in provenance.values()):
        raise ValueError("Phase 5 settings provenance is invalid")
    temperature = settings["temperature"]
    if type(temperature) not in {int, float} or isinstance(temperature, bool) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("Phase 5 temperature is invalid")
    max_output = settings["max_output_tokens"]
    if type(max_output) is not int or max_output < 1:
        raise ValueError("Phase 5 max output tokens is invalid")
    reasoning = settings["reasoning_effort"]
    if reasoning not in {None, "none"}:
        raise ValueError("Phase 5 reasoning effort is invalid")
    timeout = settings["timeout_seconds"]
    if timeout is not None and (type(timeout) not in {int, float} or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Phase 5 timeout is invalid")

    capabilities = snapshot["capabilities"]
    if not isinstance(capabilities, list) or len(capabilities) != len(CAPABILITY_KEYS):
        raise ValueError("Phase 5 capabilities are invalid")
    capability_keys: set[str] = set()
    for fact in capabilities:
        if not isinstance(fact, dict) or set(fact) != {"key", "state", "source", "source_revision", "value"}:
            raise ValueError("Phase 5 capability fact is invalid")
        if fact["key"] not in CAPABILITY_KEYS or fact["state"] not in _CAPABILITY_STATES:
            raise ValueError("Phase 5 capability fact is invalid")
        if fact["key"] in capability_keys:
            raise ValueError("Phase 5 capability facts contain duplicate keys")
        capability_keys.add(fact["key"])
        if fact["source"] not in _CAPABILITY_SOURCES:
            raise ValueError("Phase 5 capability fact is invalid")
        if fact["source"] in {"confirmed_endpoint", "provider_metadata"} and fact["source_revision"] is None:
            raise ValueError("Phase 5 capability fact revision is missing")
        if fact["source_revision"] is not None and (type(fact["source_revision"]) is not int or fact["source_revision"] < 0):
            raise ValueError("Phase 5 capability fact revision is invalid")
        try:
            validate_capability_value(fact["key"], CapabilityState(fact["state"]), fact["value"])
        except ValueError as exc:
            raise ValueError("Phase 5 capability fact value is invalid") from exc
    if capability_keys != CAPABILITY_KEYS:
        raise ValueError("Phase 5 capability facts are incomplete")
    capability_provenance = snapshot["capability_provenance"]
    if not isinstance(capability_provenance, dict) or set(capability_provenance) != CAPABILITY_KEYS:
        raise ValueError("Phase 5 capability provenance is invalid")
    facts_by_key = {fact["key"]: fact for fact in capabilities}
    for key, provenance_item in capability_provenance.items():
        if not isinstance(provenance_item, dict) or not set(provenance_item) <= _PROVENANCE_KEYS:
            raise ValueError("Phase 5 capability provenance is invalid")
        if provenance_item.get("source") not in _CAPABILITY_SOURCES:
            raise ValueError("Phase 5 capability provenance is invalid")
        if provenance_item["source"] != facts_by_key[key]["source"]:
            raise ValueError("Phase 5 capability provenance contradicts its fact")
        for text_key in ("reason", "field"):
            value = provenance_item.get(text_key)
            if value is not None and (type(value) is not str or len(value) > 256):
                raise ValueError("Phase 5 capability provenance is invalid")
        revision = provenance_item.get("catalogue_revision")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("Phase 5 capability provenance is invalid")
        if revision is not None and revision != facts_by_key[key]["source_revision"]:
            raise ValueError("Phase 5 capability provenance contradicts its fact")
    if not isinstance(snapshot["manual_overrides"], dict) or not set(snapshot["manual_overrides"]) <= CAPABILITY_KEYS:
        raise ValueError("Phase 5 manual overrides are invalid")
    manual_overrides = snapshot["manual_overrides"]
    for key, override in snapshot["manual_overrides"].items():
        if not isinstance(override, dict) or set(override) != {"state", "value", "revision"}:
            raise ValueError("Phase 5 manual overrides are invalid")
        if override["state"] not in _CAPABILITY_STATES:
            raise ValueError("Phase 5 manual overrides are invalid")
        try:
            validate_capability_value(key, CapabilityState(override["state"]), override["value"])
        except ValueError as exc:
            raise ValueError("Phase 5 manual overrides are invalid") from exc
        if type(override["revision"]) is not int or override["revision"] < 1:
            raise ValueError("Phase 5 manual overrides are invalid")
        fact = facts_by_key[key]
        if fact["source"] != "manual" or fact["state"] != override["state"] or fact["value"] != override["value"] or fact["source_revision"] != override["revision"]:
            raise ValueError("Phase 5 manual override contradicts its capability fact")
    for fact in capabilities:
        if fact["source"] == "manual" and fact["key"] not in manual_overrides:
            raise ValueError("Phase 5 manual capability fact has no override")
    omitted = snapshot["omitted_settings"]
    if not isinstance(omitted, dict) or set(omitted) != _SETTING_KEYS:
        raise ValueError("Phase 5 omitted settings are invalid")
    for key, value in omitted.items():
        if type(value) is not str or value not in {"emitted", "unset", "BOTS-owned deadline"}:
            raise ValueError("Phase 5 omitted settings are invalid")
    if omitted["temperature"] != "emitted" or omitted["max_output_tokens"] != "emitted":
        raise ValueError("Phase 5 omitted settings contradict effective settings")
    if (reasoning is None and omitted["reasoning_effort"] != "unset") or (reasoning == "none" and omitted["reasoning_effort"] != "emitted"):
        raise ValueError("Phase 5 omitted reasoning setting contradicts effective settings")
    if (timeout is None and omitted["timeout_seconds"] != "unset") or (timeout is not None and omitted["timeout_seconds"] != "BOTS-owned deadline"):
        raise ValueError("Phase 5 omitted timeout setting contradicts effective settings")
    return snapshot
