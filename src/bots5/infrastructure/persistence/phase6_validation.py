"""Validation for the additive Phase 6 frozen request/context snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from bots5.core.context import DeterministicJsonAdapter


SNAPSHOT_VERSION = 3
_PHASE5_KEYS = frozenset(
    {
        "provider_profile", "connection_id", "connection_name", "connection_revision",
        "endpoint", "credential_source", "credential_reference", "credential_status",
        "model_entry_id", "catalogue_revision", "effective_settings", "settings_provenance",
        "capabilities", "capability_provenance", "manual_overrides", "omitted_settings",
    }
)
_KEYS = frozenset(
    {
        "snapshot_version", "attempt_id", "chat_id", "user_message_id",
        "backend_id", "model", "provider_id", "prompt", "context_plan",
        "settings_revisions",
        *_PHASE5_KEYS,
    }
)
_PLAN_KEYS = frozenset(
    {
        "version", "sources", "included_sources", "excluded_sources",
        "canonical_representation", "canonical_digest", "wire_representation_sha256",
        "budget", "input_counts", "parent_id", "lineage_id",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "source_id", "kind", "role", "content", "state", "eligible", "selected",
        "reason", "representation_id", "representation_digest",
    }
)
_BUDGET_KEYS = frozenset(
    {
        "limit", "provenance", "semantics", "adapter_id", "adapter_version",
        "output_reserve", "envelope_overhead", "input_units", "total_units", "headroom",
    }
)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Phase 6 snapshot contains duplicate keys")
        result[key] = value
    return result


def _secret_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    import re
    return re.sub(r"[^a-z0-9]", "", value.casefold()) in {
        "apikey", "apikeyvalue", "apitoken", "accesstoken", "authorization",
        "clientsecret", "password", "refreshtoken", "secret", "secretvalue", "token",
    }


def _contains_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_secret_key(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _text(value: object, field: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise ValueError(f"Phase 6 snapshot field {field} is invalid")
    return value


def validate_phase6_snapshot(
    request_snapshot: object,
    *,
    attempt_id: object,
    chat_id: object,
    user_message_id: object,
    backend_id: object,
    model: object,
    provider_id: object,
    user_message_content: object | None = None,
) -> dict[str, object]:
    try:
        snapshot = json.loads(request_snapshot, object_pairs_hook=_pairs)
    except (TypeError, ValueError) as exc:
        raise ValueError("Phase 6 request snapshot must be valid JSON") from exc
    if not isinstance(snapshot, dict) or set(snapshot) != _KEYS:
        raise ValueError("Phase 6 request snapshot fields are not the closed schema")
    if _contains_secret(snapshot):
        raise ValueError("Phase 6 request snapshot must not contain secret values")
    if snapshot["snapshot_version"] != SNAPSHOT_VERSION:
        raise ValueError("unsupported Phase 6 request snapshot version")
    # Reuse the closed Phase 5 provider/model boundary for the unchanged
    # frozen request fields.  This keeps v2 readable while making v3
    # additive rather than a second provider-configuration language.
    from .phase5_validation import validate_phase5_snapshot
    legacy = dict(snapshot)
    legacy["snapshot_version"] = 2
    legacy.pop("context_plan", None)
    legacy.pop("settings_revisions", None)
    validate_phase5_snapshot(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        attempt_id=attempt_id, chat_id=chat_id, user_message_id=user_message_id,
        backend_id=backend_id, model=model, provider_id=provider_id,
        user_message_content=user_message_content,
    )
    for key, expected in {
        "attempt_id": attempt_id, "chat_id": chat_id, "user_message_id": user_message_id,
        "backend_id": backend_id, "model": model, "provider_id": provider_id,
    }.items():
        if snapshot[key] != expected:
            raise ValueError(f"Phase 6 request snapshot contradicts {key}")
    _text(snapshot["attempt_id"], "attempt_id")
    _text(snapshot["chat_id"], "chat_id")
    _text(snapshot["user_message_id"], "user_message_id")
    _text(snapshot["backend_id"], "backend_id")
    _text(snapshot["model"], "model")
    _text(snapshot["prompt"], "prompt", empty=True)
    if user_message_content is not None and snapshot["prompt"] != user_message_content:
        raise ValueError("Phase 6 request snapshot prompt is not the current user message")
    plan = snapshot["context_plan"]
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        raise ValueError("Phase 6 context plan fields are not the closed schema")
    if plan["version"] != 3:
        raise ValueError("unsupported Phase 6 context plan version")
    sources = plan["sources"]
    if not isinstance(sources, list):
        raise ValueError("Phase 6 context sources are invalid")
    ids: list[str] = []
    selected: list[str] = []
    excluded: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
            raise ValueError("Phase 6 context source is invalid")
        source_id = _text(source["source_id"], "source_id")
        if source_id in ids:
            raise ValueError("Phase 6 context sources contain duplicate identities")
        ids.append(source_id)
        _text(source["kind"], "kind")
        _text(source["role"], "role")
        _text(source["content"], "content", empty=True)
        _text(source["state"], "state")
        if type(source["eligible"]) is not bool or type(source["selected"]) is not bool:
            raise ValueError("Phase 6 context source eligibility is invalid")
        _text(source["reason"], "reason")
        if source["kind"] == "history" and source["role"] not in {"user", "assistant"}:
            raise ValueError("Phase 6 history source role is invalid")
        if source["kind"] == "bots_instruction" and source["role"] != "system":
            raise ValueError("Phase 6 instruction source role is invalid")
        if source["kind"] == "current_user" and source["role"] != "user":
            raise ValueError("Phase 6 current user source role is invalid")
        if source["kind"] == "attachment":
            representation_id = source["representation_id"]
            representation_digest = source["representation_digest"]
            if (
                type(representation_id) is not str
                or re.fullmatch(r"[0-9a-f]{64}", representation_id) is None
                or representation_digest != representation_id
            ):
                raise ValueError("Phase 6 attachment representation identity is invalid")
        elif source["representation_id"] is not None or source["representation_digest"] is not None:
            raise ValueError("Phase 6 non-attachment representation is invalid")
        if source["selected"]:
            selected.append(source_id)
            if not source["eligible"]:
                raise ValueError("selected Phase 6 context source is ineligible")
        else:
            excluded.append(source_id)
            if source["reason"] != "excluded_oldest_complete_turn":
                raise ValueError("Phase 6 excluded context source has an invalid reason")
            if source["kind"] != "history":
                raise ValueError("Phase 6 mandatory context source cannot be excluded")
    current_sources = [source for source in sources if source["kind"] == "current_user"]
    if len(current_sources) != 1 or current_sources[0]["source_id"] != user_message_id:
        raise ValueError("Phase 6 context must contain exactly the current user source")
    if current_sources[0]["content"] != snapshot["prompt"] or not current_sources[0]["selected"]:
        raise ValueError("Phase 6 current user source does not match the prompt")
    instructions = [source for source in sources if source["kind"] == "bots_instruction"]
    if len(instructions) != 1 or instructions[0]["source_id"] != "bots5-required-context-envelope-v3":
        raise ValueError("Phase 6 context envelope instruction is missing")
    if plan["included_sources"] != selected or plan["excluded_sources"] != excluded:
        raise ValueError("Phase 6 context source inclusion is inconsistent")
    canonical = _text(plan["canonical_representation"], "canonical_representation", empty=True)
    digest = _text(plan["canonical_digest"], "canonical_digest")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Phase 6 context canonical digest is not lowercase SHA-256")
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
        raise ValueError("Phase 6 context canonical digest does not match")
    wire_digest = _text(plan["wire_representation_sha256"], "wire_representation_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", wire_digest) is None:
        raise ValueError("Phase 6 wire representation digest is invalid")
    try:
        canonical_value = json.loads(canonical, object_pairs_hook=_pairs)
    except (TypeError, ValueError) as exc:
        raise ValueError("Phase 6 canonical representation is invalid") from exc
    if not isinstance(canonical_value, dict) or set(canonical_value) != {
        "version", "sources", "included_sources", "excluded_sources", "envelope", "wire_sha256",
    }:
        raise ValueError("Phase 6 canonical representation is not the closed schema")
    if (
        canonical_value["version"] != 3
        or canonical_value["sources"] != sources
        or canonical_value["included_sources"] != plan["included_sources"]
        or canonical_value["excluded_sources"] != plan["excluded_sources"]
        or canonical_value["wire_sha256"] != wire_digest
    ):
        raise ValueError("Phase 6 canonical representation does not match the plan")
    if not isinstance(canonical_value["envelope"], dict) or canonical_value["envelope"] != {
        "version": 3,
        "untrusted_user_context": True,
    }:
        raise ValueError("Phase 6 context envelope is not the deterministic envelope")
    if json.dumps(canonical_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) != canonical:
        raise ValueError("Phase 6 canonical representation is not canonical")
    budget = plan["budget"]
    if not isinstance(budget, dict) or set(budget) != _BUDGET_KEYS:
        raise ValueError("Phase 6 context budget is invalid")
    for key in ("limit", "output_reserve", "envelope_overhead", "input_units", "total_units", "headroom"):
        if type(budget[key]) is not int or budget[key] < 0:
            raise ValueError("Phase 6 context budget is invalid")
    if budget["limit"] <= 0 or budget["total_units"] != budget["input_units"] + budget["envelope_overhead"] + budget["output_reserve"]:
        raise ValueError("Phase 6 context budget accounting is invalid")
    if budget["headroom"] != budget["limit"] - budget["total_units"]:
        raise ValueError("Phase 6 context budget headroom is invalid")
    if budget["headroom"] < 0:
        raise ValueError("Phase 6 context budget exceeds the context window")
    if budget["total_units"] > budget["limit"]:
        raise ValueError("Phase 6 context budget exceeds the context window")
    for key in ("provenance", "semantics", "adapter_id", "adapter_version"):
        _text(budget[key], key)
    if budget["adapter_id"] != "bots5.deterministic-json" or budget["adapter_version"] != "1":
        raise ValueError("Phase 6 accounting adapter is not registered")
    if budget["semantics"] != DeterministicJsonAdapter().semantics:
        raise ValueError("Phase 6 accounting adapter semantics are not canonical")

    # The plan's limit is not an independent caller-selected number.  It is
    # frozen evidence from the resolved model capability used to construct
    # this request.  Binding both the value and the exact provenance string
    # here closes the gap between an arithmetically valid plan and the actual
    # trusted context window.
    context_capabilities = [
        item for item in snapshot["capabilities"]
        if item["key"] == "limits.context_tokens"
    ]
    if len(context_capabilities) != 1:
        raise ValueError("Phase 6 context capability is missing")
    context_capability = context_capabilities[0]
    if (
        context_capability["state"] != "supported"
        or type(context_capability["value"]) is not int
        or context_capability["value"] <= 0
    ):
        raise ValueError("Phase 6 context capability is not an exact supported limit")
    if budget["limit"] != context_capability["value"]:
        raise ValueError("Phase 6 budget limit does not match the frozen context capability")
    context_provenance = snapshot["capability_provenance"]["limits.context_tokens"]
    field = context_provenance.get("field") if isinstance(context_provenance, dict) else None
    if type(field) is not str or not field:
        raise ValueError("Phase 6 context capability provenance is ambiguous")
    source_revision = context_capability["source_revision"]
    expected_provenance = (
        f"{context_capability['source']}:{source_revision if source_revision is not None else 'None'}:{field}"
    )
    if budget["provenance"] != expected_provenance:
        raise ValueError("Phase 6 budget provenance does not match the frozen context capability")
    messages = tuple(
        {"role": source["role"], "content": source["content"]}
        for source in sources
        if source["selected"]
    )
    adapter = DeterministicJsonAdapter()
    wire = adapter.encode(messages, canonical_value["envelope"])
    empty_wire = adapter.encode((), canonical_value["envelope"])
    if hashlib.sha256(wire).hexdigest() != wire_digest:
        raise ValueError("Phase 6 wire representation digest does not match the canonical plan")
    envelope_overhead = adapter.measure(empty_wire)
    input_units = adapter.measure(wire) - envelope_overhead
    if budget["envelope_overhead"] != envelope_overhead or budget["input_units"] != input_units:
        raise ValueError("Phase 6 context budget units do not match the wire representation")
    counts = plan["input_counts"]
    if not isinstance(counts, dict) or set(counts) != {
        "history_turns_considered", "history_turns_included", "history_turns_excluded", "mandatory_sources",
    }:
        raise ValueError("Phase 6 context input counts are invalid")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("Phase 6 context input counts are invalid")
    history_count = sum(source["kind"] == "history" for source in sources)
    mandatory_count = len(sources) - history_count
    if (
        history_count != 2 * counts["history_turns_considered"]
        or counts["history_turns_included"] + counts["history_turns_excluded"] != counts["history_turns_considered"]
        or counts["mandatory_sources"] != mandatory_count
        or len(plan["excluded_sources"]) != 2 * counts["history_turns_excluded"]
    ):
        raise ValueError("Phase 6 context input counts do not match the source list")
    settings_revisions = snapshot["settings_revisions"]
    if (
        not isinstance(settings_revisions, dict)
        or set(settings_revisions) != {"application", "model", "chat"}
        or type(settings_revisions["application"]) is not int
        or settings_revisions["application"] < 1
        or any(
            value is not None and (type(value) is not int or value < 1)
            for value in (settings_revisions["model"], settings_revisions["chat"])
        )
    ):
        raise ValueError("Phase 6 settings revisions are invalid")
    if plan["parent_id"] is not None:
        _text(plan["parent_id"], "parent_id")
    if plan["lineage_id"] is not None:
        _text(plan["lineage_id"], "lineage_id")
    return snapshot
