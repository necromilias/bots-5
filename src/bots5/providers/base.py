from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Mapping, Protocol

import httpx

from bots5.domain.generation_settings_registry import (
    GenerationSettingsPayload,
    PayloadFamily,
    setting_definition,
)


ReasoningEffort = Literal["none"]


def transport_timeout(timeout_seconds: float | None) -> httpx.Timeout | None:
    """Map the caller-supplied completion deadline onto an httpx transport backstop.

    ``CompletionRequest.timeout_seconds`` is a B.O.T.S.-owned deadline plumbed
    from ``GenerationRequest.timeout_seconds`` (0.0 / unset means "no caller
    deadline").  Without this mapping the value was dead at the transport
    layer: every provider client constructed ``AsyncClient(timeout=None)``, so
    a hung connect/TLS or a stalled stream had no backstop below the
    application-level absolute deadline.  The mapping bounds each transport
    operation (connect/read/write/pool) by the declared deadline; it is a
    backstop, not a duplicate of the application deadline, which remains the
    sole enforcement of the total budget.  Requests without a caller deadline
    keep the previous unbounded transport behaviour.
    """
    if timeout_seconds is None or not (timeout_seconds > 0):
        return None
    return httpx.Timeout(timeout_seconds)


def serialize_generation_settings(
    settings: GenerationSettingsPayload | None,
    *,
    states: Mapping[str, str] | None,
    capabilities: Mapping[str, str] | None = None,
    omitted: Mapping[str, str] | None = None,
    profile: str | None,
) -> dict[str, Any]:
    """Translate capability-resolved settings into the endpoint payload.

    The translation is data-driven from the registry's serialization mappings
    for the OpenAI-compatible family: a setting reaches the payload only when
    a truthful mapping exists for the connection profile, and never as a
    free-form key/value pass-through.  Deterministic emission order keeps
    payloads stable.

    This is the FINAL provider boundary.  A setting may cross it only when
    the request also carries an explicit ``emitted`` capability state for it
    (``states``) and no other evidence plane contradicts that: an unsupported/
    unknown/contradictory setting is dropped here even if an upstream builder
    populated the payload, and a request with no state evidence at all emits
    nothing (fail closed).
    """
    if settings is None:
        return {}
    payload: dict[str, Any] = {}
    allowed = settings.emittable_keys(states, capabilities, omitted)
    for key in allowed:
        if profile == "openrouter" and key == "top_logprobs" and (
            "logprobs" not in allowed or settings.logprobs is not True
        ):
            continue
        definition = setting_definition(key)
        mapping = definition.serialization_for(PayloadFamily.OPENAI_COMPATIBLE, profile)
        if mapping is None:
            continue
        value = getattr(settings, key)
        if isinstance(value, tuple):
            value = list(value)
        if "." in mapping.payload_key:
            root, _, leaf = mapping.payload_key.partition(".")
            nested = payload.setdefault(root, {})
            nested[leaf] = value
        else:
            payload[mapping.payload_key] = value
    return payload


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    system: str
    user: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: float
    reasoning_effort: ReasoningEffort | None = None
    # Phase 11 scope amendment: typed, validated, capability-resolved payload
    # settings.  ``None`` fields are omitted from the wire payload; adapters
    # must never accept unchecked dictionaries here.  ``generation_setting_states``
    # is the capability evidence that authorises each populated setting to
    # cross the boundary; without it the payload emits nothing.
    generation_settings: GenerationSettingsPayload | None = None
    generation_setting_states: Mapping[str, str] | None = None
    # Additional independent evidence planes.  ``generation_setting_capabilities``
    # maps setting key -> capability state; ``generation_omitted_settings`` is
    # the request's omission map.  Either may contradict an ``emitted`` state,
    # and the boundary then drops the setting.
    generation_setting_capabilities: Mapping[str, str] | None = None
    generation_omitted_settings: Mapping[str, str] | None = None
    # Closed OpenRouter alias selected from frozen catalogue provenance.
    # Generic/legacy requests retain their existing max_tokens wire field.
    max_output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    context_messages: tuple[tuple[str, str], ...] | None = None
    accounting_mode: str | None = None



@dataclass(frozen=True)
class CompletionResult:
    output_text: str
    requested_model: str
    finish_reason: str | None = None
    returned_model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class CompletionStreamEvent:
    text: str = ""
    finish_reason: str | None = None
    returned_model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None


class Provider(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        ...


class StreamingProvider(Provider, Protocol):
    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionStreamEvent]:
        ...
