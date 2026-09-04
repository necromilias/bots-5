from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from bots5.core.urls import canonical_http_base_url

PHASE3_BACKEND_ID = "openai_compatible_http"
PHASE3_PROVIDER_IDS = frozenset({"local_openai", "openrouter"})
_PHASE3_REQUIRED_SNAPSHOT_KEYS = (
    "attempt_id",
    "chat_id",
    "user_message_id",
    "backend_id",
    "model",
    "prompt",
    "provider_id",
    "base_url",
)
_PHASE3_ALLOWED_SNAPSHOT_KEYS = frozenset(
    {*_PHASE3_REQUIRED_SNAPSHOT_KEYS, "api_key_env"}
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQLITE_INTEGER_MAX = 2**63 - 1
_FORBIDDEN_SNAPSHOT_KEYS = frozenset(
    {"api_key", "api_key_value", "authorization", "secret", "secret_value"}
)


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("generation request snapshot contains duplicate keys")
        result[key] = value
    return result


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in _FORBIDDEN_SNAPSHOT_KEYS or _contains_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _validate_base_url(value: object, error_type: type[Exception]) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "?" in value
        or "#" in value
    ):
        raise error_type("generation request snapshot base URL is not normalized")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        parsed = None
        hostname = None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise error_type("generation request snapshot base URL is invalid")


def _validate_canonical_base_url(value: object, error_type: type[Exception]) -> None:
    canonical = canonical_http_base_url(
        value,
        error_type=error_type,
        error_message="generation request snapshot base URL is invalid",
    )
    if value != canonical:
        raise error_type("generation request snapshot base URL is not normalized")


def is_phase3_record(
    *,
    backend_id: object,
    provider_id: object,
    snapshot: Mapping[str, object],
    include_backend_marker: bool = True,
) -> bool:
    return (
        provider_id is not None
        or snapshot.get("provider_id") in PHASE3_PROVIDER_IDS
        or snapshot.get("backend_id") == PHASE3_BACKEND_ID
        or (include_backend_marker and backend_id == PHASE3_BACKEND_ID)
    )


def validate_request_snapshot(
    *,
    attempt_id: object,
    chat_id: object,
    user_message_id: object,
    backend_id: object,
    model: object,
    provider_id: object,
    request_snapshot: object,
    user_message_content: object | None = None,
    phase3: bool | None = None,
    error_type: type[Exception] = ValueError,
) -> dict[str, object]:
    try:
        snapshot = json.loads(request_snapshot)
    except (TypeError, ValueError) as exc:
        raise error_type("generation request snapshot must be valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise error_type("generation request snapshot must be a JSON object")

    expected = {
        "attempt_id": attempt_id,
        "chat_id": chat_id,
        "user_message_id": user_message_id,
        "backend_id": backend_id,
        "model": model,
    }
    is_phase3 = (
        is_phase3_record(
            backend_id=backend_id,
            provider_id=provider_id,
            snapshot=snapshot,
        )
        if phase3 is None
        else phase3
    )
    if is_phase3:
        try:
            snapshot = json.loads(
                request_snapshot,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (TypeError, ValueError) as exc:
            raise error_type("generation request snapshot must be valid JSON") from exc
        if not isinstance(snapshot, dict):
            raise error_type("generation request snapshot must be a JSON object")
        if _contains_forbidden_key(snapshot):
            raise error_type("generation request snapshot must not contain secret values")
        if provider_id not in PHASE3_PROVIDER_IDS:
            raise error_type("generation attempt has an invalid Phase 3 provider ID")
        if backend_id != PHASE3_BACKEND_ID:
            raise error_type("generation attempt has an invalid Phase 3 backend ID")
        unexpected = set(snapshot) - _PHASE3_ALLOWED_SNAPSHOT_KEYS
        if unexpected:
            raise error_type("generation request snapshot contains unknown fields")
        expected["provider_id"] = provider_id
        for key in _PHASE3_REQUIRED_SNAPSHOT_KEYS:
            if key not in snapshot:
                raise error_type(
                    f"generation request snapshot is missing mandatory {key}"
                )
        for key, value in expected.items():
            if snapshot[key] != value:
                raise error_type(f"generation request snapshot contradicts {key}")
        if type(snapshot["model"]) is not str or not snapshot["model"]:
            raise error_type("generation request snapshot model is invalid")
        if type(snapshot["prompt"]) is not str:
            raise error_type("generation request snapshot prompt is invalid")
        if user_message_content is not None and snapshot["prompt"] != user_message_content:
            raise error_type("generation request snapshot prompt is not the current user message")
        _validate_canonical_base_url(snapshot["base_url"], error_type)
        api_key_env = snapshot.get("api_key_env")
        if api_key_env is not None and (
            type(api_key_env) is not str or _ENVIRONMENT_NAME.fullmatch(api_key_env) is None
        ):
            raise error_type("generation request snapshot authentication name is invalid")
        return snapshot

    for key, value in expected.items():
        if key in snapshot and snapshot[key] != value:
            raise error_type(f"generation request snapshot contradicts {key}")
    return snapshot


def valid_request_snapshot(
    request_snapshot: object,
    attempt_id: object,
    chat_id: object,
    user_message_id: object,
    backend_id: object,
    model: object,
    provider_id: object,
    user_message_content: object | None,
    phase3: object | None = None,
) -> int:
    """Return a SQLite-safe truth value for the canonical snapshot validator."""
    try:
        validate_request_snapshot(
            attempt_id=attempt_id,
            chat_id=chat_id,
            user_message_id=user_message_id,
            backend_id=backend_id,
            model=model,
            provider_id=provider_id,
            request_snapshot=request_snapshot,
            user_message_content=user_message_content,
            phase3=None if phase3 is None else bool(phase3),
            error_type=ValueError,
        )
    except Exception:
        return 0
    return 1


def validate_remote_outcome_transition(
    *,
    old_state: object,
    old_remote_outcome_unknown: object,
    new_state: object,
    new_remote_outcome_unknown: object,
    new_finish_reason: object,
    phase3: bool,
    error_type: type[Exception] = ValueError,
) -> None:
    """Keep dispatch uncertainty until a dispatched request has a known result."""
    if (
        not phase3
        or old_state != "running"
        or old_remote_outcome_unknown not in {True, 1}
    ):
        return
    if new_state in {"running", "aborted"} and new_remote_outcome_unknown not in {True, 1}:
        raise error_type(
            "Phase 3 dispatch uncertainty cannot be cleared while running or aborting"
        )
    if (
        new_state == "incomplete"
        and new_finish_reason is None
        and new_remote_outcome_unknown not in {True, 1}
    ):
        raise error_type(
            "Phase 3 dispatch uncertainty requires a known terminal finish reason"
        )


def valid_remote_outcome_transition(
    old_state: object,
    old_remote_outcome_unknown: object,
    new_state: object,
    new_remote_outcome_unknown: object,
    new_finish_reason: object,
    phase3: object,
) -> int:
    """Return a SQLite-safe truth value for the uncertainty transition validator."""
    try:
        validate_remote_outcome_transition(
            old_state=old_state,
            old_remote_outcome_unknown=old_remote_outcome_unknown,
            new_state=new_state,
            new_remote_outcome_unknown=new_remote_outcome_unknown,
            new_finish_reason=new_finish_reason,
            phase3=bool(phase3),
            error_type=ValueError,
        )
    except Exception:
        return 0
    return 1


def validate_outcome_fields(
    *,
    state: object,
    provider_id: object,
    finish_reason: object,
    prompt_tokens: object,
    completion_tokens: object,
    reasoning_tokens: object,
    total_tokens: object,
    known_cost_usd: object,
    remote_outcome_unknown: object,
    returned_model: object | None = None,
    request_id: object | None = None,
    outcome_error_type: object | None = None,
    outcome_error_message: object | None = None,
    phase3: bool | None = None,
    error_type: type[Exception] = ValueError,
) -> None:
    if provider_id is not None and provider_id not in PHASE3_PROVIDER_IDS:
        raise error_type("generation attempt has an invalid provider ID")
    for name, value in (("returned_model", returned_model), ("request_id", request_id)):
        if value is not None and (type(value) is not str or not value):
            raise error_type(f"generation attempt has invalid {name}")
    for name, value in (
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
        ("reasoning_tokens", reasoning_tokens),
        ("total_tokens", total_tokens),
    ):
        if value is not None and (
            type(value) is not int or value < 0 or value > _SQLITE_INTEGER_MAX
        ):
            raise error_type(f"generation attempt has invalid {name}")
    if remote_outcome_unknown is not None and (
        type(remote_outcome_unknown) not in {int, bool}
        or remote_outcome_unknown not in {0, 1, False, True}
    ):
        raise error_type("generation attempt has invalid remote_outcome_unknown")
    if known_cost_usd is not None:
        try:
            parsed = Decimal(str(known_cost_usd))
        except (InvalidOperation, ValueError):
            raise error_type("generation attempt has invalid cost") from None
        if not parsed.is_finite() or parsed < 0:
            raise error_type("generation attempt has invalid cost")
    if finish_reason is not None and (
        type(finish_reason) is not str or not finish_reason
    ):
        raise error_type("generation attempt has invalid finish_reason")
    if phase3 is None:
        phase3 = provider_id is not None
    if phase3:
        if remote_outcome_unknown is None:
            raise error_type(
                "Phase 3 generation requires explicit remote outcome truth"
            )
        if finish_reason is not None and remote_outcome_unknown in {True, 1}:
            raise error_type(
                "Phase 3 known finish reason cannot have an unknown outcome"
            )
        if state in {"running", "complete"} and (
            outcome_error_type is not None or outcome_error_message is not None
        ):
            raise error_type(
                f"Phase 3 {state} generation cannot have failure metadata"
            )
        if state == "complete":
            if finish_reason != "stop":
                raise error_type("Phase 3 complete generation requires finish_reason=stop")
            if remote_outcome_unknown not in {False, 0}:
                raise error_type("Phase 3 complete generation cannot have an unknown outcome")
        elif state == "incomplete" and finish_reason == "stop":
            raise error_type("Phase 3 incomplete generation cannot have finish_reason=stop")
        elif state == "running" and finish_reason is not None:
            raise error_type("Phase 3 running generation cannot have finish_reason")
        elif state in {"failed", "aborted"} and finish_reason is not None:
            raise error_type(
                f"Phase 3 {state} generation cannot have finish_reason"
            )


def valid_outcome_fields(
    state: object,
    provider_id: object,
    finish_reason: object,
    prompt_tokens: object,
    completion_tokens: object,
    reasoning_tokens: object,
    total_tokens: object,
    known_cost_usd: object,
    remote_outcome_unknown: object,
    returned_model: object,
    request_id: object,
    outcome_error_type: object,
    outcome_error_message: object,
    phase3: object,
) -> int:
    """Return a SQLite-safe truth value for the outcome validator."""
    try:
        validate_outcome_fields(
            state=state,
            provider_id=provider_id,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            known_cost_usd=known_cost_usd,
            remote_outcome_unknown=remote_outcome_unknown,
            returned_model=returned_model,
            request_id=request_id,
            outcome_error_type=outcome_error_type,
            outcome_error_message=outcome_error_message,
            phase3=bool(phase3),
            error_type=ValueError,
        )
    except Exception:
        return 0
    return 1
