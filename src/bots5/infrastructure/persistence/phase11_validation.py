"""Phase 11 scope amendment: additive v4 request-snapshot validation.

A v4 snapshot is the frozen Phase 5 (v2) snapshot contract plus exactly one
additional closed field, ``generation_settings``:

    generation_settings = {
        "values":      {setting key -> canonical value},
        "provenance":  {setting key -> "application"|"model"|"chat_model"|"branch"},
        "states":      {setting key -> emitted|unset|omitted:*},
    }

``states`` covers EVERY key of the normalized registry, so the durable record
always shows, per setting, whether it was emitted, unset, or omitted and why.
Values/provenance carry only configured (non-None) settings.  Unknown setting
keys, unknown states, and states that contradict the recorded values are
rejected — evidence never lies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from bots5.domain.provider import PHASE11_SNAPSHOT_VERSION
from bots5.domain.generation_settings_registry import (
    EMISSION_ORDER,
    SETTING_DEFINITIONS_BY_KEY,
    SETTING_KEYS,
    SETTING_STATES,
)

from .phase5_validation import (
    _SETTING_KEYS,
    _SNAPSHOT_KEYS,
    validate_phase5_snapshot,
)


def _validate_generation_settings_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"values", "provenance", "states"}:
        raise ValueError("Phase 11 generation settings evidence is invalid")
    values = value["values"]
    provenance = value["provenance"]
    states = value["states"]
    if not isinstance(values, dict) or not isinstance(provenance, dict) or not isinstance(states, dict):
        raise ValueError("Phase 11 generation settings evidence is invalid")
    if set(states) != SETTING_KEYS:
        raise ValueError("Phase 11 generation settings states are incomplete")
    allowed_provenance = {"application", "model", "chat_model", "branch"}
    for key, state in states.items():
        if type(state) is not str or state not in SETTING_STATES:
            raise ValueError("Phase 11 generation settings states are invalid")
        definition = SETTING_DEFINITIONS_BY_KEY[key]
        recorded = values.get(key)
        if state == "unset":
            if recorded is not None or key in provenance:
                raise ValueError("Phase 11 generation settings evidence contradicts its states")
            continue
        if recorded is None or key not in provenance:
            raise ValueError("Phase 11 generation settings evidence contradicts its states")
        if type(provenance[key]) is not str or provenance[key] not in allowed_provenance:
            raise ValueError("Phase 11 generation settings provenance is invalid")
        try:
            definition.validate(recorded)
        except ValueError as exc:
            raise ValueError("Phase 11 generation settings values are invalid") from exc
    if set(values) - set(provenance) or set(provenance) - set(values):
        raise ValueError("Phase 11 generation settings evidence is inconsistent")
    return value


def validate_phase11_snapshot(
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
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Phase 5 request snapshot must be valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("Phase 5 request snapshot must be a JSON object")
    if snapshot.get("snapshot_version") != PHASE11_SNAPSHOT_VERSION:
        raise ValueError("unsupported Phase 11 request snapshot version")
    if set(snapshot) != (_SNAPSHOT_KEYS | {"generation_settings"}):
        raise ValueError("Phase 11 request snapshot fields are not the closed schema")
    generation_settings = _validate_generation_settings_evidence(
        snapshot.get("generation_settings")
    )
    # Re-run the complete frozen v2 validation on the base contract so the v4
    # snapshot can never relax a single pre-amendment rule.
    base = {key: item for key, item in snapshot.items() if key != "generation_settings"}
    base["snapshot_version"] = 2
    validate_phase5_snapshot(
        json.dumps(base, sort_keys=True, separators=(",", ":")),
        attempt_id=attempt_id,
        chat_id=chat_id,
        user_message_id=user_message_id,
        backend_id=backend_id,
        model=model,
        provider_id=provider_id,
        user_message_content=user_message_content,
        allow_branch_settings_provenance=allow_branch_settings_provenance,
    )
    return snapshot


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Phase 5 request snapshot contains duplicate keys")
        result[key] = value
    return result


def snapshot_states_cover_catalogue(states: Mapping[str, object]) -> bool:
    return set(states) == SETTING_KEYS and set(EMISSION_ORDER) == SETTING_KEYS and set(_SETTING_KEYS) <= SETTING_KEYS
