"""Snapshot v5 validates its provider-managed plan separately from exact v3."""
from bots5.core.provider_managed_context import ProviderManagedContextPlan, canonical_json, parse_json
from .phase5_validation import _SNAPSHOT_KEYS, validate_phase5_snapshot
from .phase11_validation import _validate_generation_settings_evidence


def validate_provider_managed_snapshot(request_snapshot, **identity):
    snapshot = parse_json(request_snapshot)
    fields = _SNAPSHOT_KEYS | {"accounting_mode", "provider_managed_plan", "settings_revisions", "serialization", "generation_authority"}
    if type(snapshot) is not dict or set(snapshot) not in (fields, fields | {"generation_settings"}):
        raise ValueError("provider-managed snapshot is not the closed schema")
    if type(snapshot["snapshot_version"]) is not int or snapshot["snapshot_version"] != 5 or snapshot["accounting_mode"] != "provider-managed":
        raise ValueError("provider-managed snapshot mode/version mismatch")
    if snapshot["backend_id"] != "openai_compatible_http" or snapshot["provider_profile"] != "openrouter":
        raise ValueError("provider-managed accounting is not registered for this provider")
    base = {key: snapshot[key] for key in _SNAPSHOT_KEYS}
    base["snapshot_version"] = 2
    validate_phase5_snapshot(canonical_json(base), allow_branch_settings_provenance=True, **identity)
    if "generation_settings" in snapshot:
        _validate_generation_settings_evidence(snapshot["generation_settings"])
    plan = ProviderManagedContextPlan.from_evidence(snapshot["provider_managed_plan"])
    current = [s for s in plan.sources if s.kind == "current_user"]
    if len(current) != 1 or current[0].source_id != snapshot["user_message_id"] or current[0].content != snapshot["prompt"]:
        raise ValueError("provider-managed current message mismatch")
    value = parse_json(plan.canonical_representation)
    capability = next((c for c in snapshot["capabilities"] if c["key"] == "limits.context_tokens"), None)
    recorded = value["context_capability"]
    if capability is None or capability["state"] != "supported" or (capability["value"], capability["source"], capability["source_revision"]) != (recorded["advertised_context_tokens"], recorded["source"], recorded["source_revision"]):
        raise ValueError("provider-managed context capability mismatch")
    if snapshot["capability_provenance"]["limits.context_tokens"].get("field") != recorded["field"] or value["policy"]["output_reserve"] != snapshot["effective_settings"]["max_output_tokens"]:
        raise ValueError("provider-managed budgeting provenance mismatch")
    revisions = snapshot["settings_revisions"]
    if type(revisions) is not dict or set(revisions) != {"application", "model", "chat"} or type(revisions["application"]) is not int or revisions["application"] < 1 or any(v is not None and (type(v) is not int or v < 1) for v in revisions.values()):
        raise ValueError("provider-managed settings revisions are invalid")
    serialization = snapshot["serialization"]
    if type(serialization) is not dict or set(serialization) != {"max_output_parameter", "catalogue_revision"} or serialization["max_output_parameter"] not in {"max_tokens", "max_completion_tokens"} or serialization["catalogue_revision"] != snapshot["catalogue_revision"]:
        raise ValueError("provider-managed serialization evidence is invalid")
    validate_generation_authority(snapshot["generation_authority"])
    return snapshot


def valid_plan_json(value):
    try:
        ProviderManagedContextPlan.from_evidence(parse_json(value))
        return 1
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return 0


def validate_generation_authority(value):
    from bots5.domain.generation_settings_registry import SETTING_CAPABILITY_KEYS
    if type(value) is not dict or set(value) != {"model_revision", "extra_revisions", "capability_overrides"} or type(value["model_revision"]) is not int or value["model_revision"] < 1:
        raise ValueError("provider-managed extended authority is invalid")
    revisions = value["extra_revisions"]
    if type(revisions) is not dict or set(revisions) != {"application","model","chat"} or type(revisions["application"]) is not int or revisions["application"] < 0 or any(v is not None and (type(v) is not int or v < 0) for v in revisions.values()):
        raise ValueError("provider-managed extended revisions are invalid")
    overrides = value["capability_overrides"]
    if type(overrides) is not dict or set(overrides) - SETTING_CAPABILITY_KEYS:
        raise ValueError("provider-managed extended capability identity is invalid")
    for record in overrides.values():
        if type(record) is not dict or set(record) != {"state","revision"} or record["state"] not in {"supported","unsupported","unknown"} or type(record["revision"]) is not int or record["revision"] < 1:
            raise ValueError("provider-managed extended override is invalid")
