"""Bounded OpenRouter catalogue evidence, without generic endpoint assumptions.

Provider advertisements establish parameter support. The documented streaming
contract establishes a profile capability, not observed endpoint behaviour.
Missing evidence produces no fact and therefore resolves through the normal
capability machinery as unknown.
"""

from __future__ import annotations

import re

from bots5.domain.provider import (
    BackendType,
    CapabilityFact,
    CapabilityKey,
    CapabilitySource,
    CapabilityState,
    CatalogueAvailability,
    CatalogueOrigin,
    ModelCatalogueEntry,
    ProviderConnection,
    ProviderProfile,
)


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_STREAMING_AUTHORITY = "https://openrouter.ai/docs/api_reference/streaming"
_PARAMETER_KEYS = (
    ("temperature", CapabilityKey.TEMPERATURE.value),
    ("top_p", "request.top_p"),
    ("top_k", "request.top_k"),
    ("min_p", "request.min_p"),
    ("frequency_penalty", "request.frequency_penalty"),
    ("presence_penalty", "request.presence_penalty"),
    ("repetition_penalty", "request.repetition_penalty"),
    ("seed", "request.seed"),
    ("stop", "request.stop_sequences"),
    ("logprobs", "request.logprobs"),
    ("top_logprobs", "request.top_logprobs"),
)


def _names(value: object, *, maximum: int) -> list[str] | None:
    if type(value) is not list or len(value) > maximum:
        return None
    if any(type(item) is not str or _NAME.fullmatch(item) is None for item in value):
        return None
    return sorted(set(value))


def _reasoning(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    result: dict[str, object] = {}
    if "supported_efforts" in value:
        efforts = value["supported_efforts"]
        if efforts is None:
            result["supported_efforts"] = None
        else:
            names = _names(efforts, maximum=32)
            if names is None:
                return None
            result["supported_efforts"] = names
    for key in ("mandatory", "supports_max_tokens", "default_enabled"):
        if key in value:
            if type(value[key]) is not bool:
                return None
            result[key] = value[key]
    if "default_effort" in value:
        effort = value["default_effort"]
        if effort is not None and (type(effort) is not str or _NAME.fullmatch(effort) is None):
            return None
        result["default_effort"] = effort
    return result or None


def sanitize_openrouter_metadata(metadata: dict) -> dict:
    """Keep only bounded, correctly shaped parameter and reasoning evidence.

    Unknown parameter names survive for catalogue provenance, but do not gain
    capabilities. A malformed list is ignored in full, rather than promoting
    its valid-looking prefix. Limits and other metadata belong to the caller.
    """
    if type(metadata) is not dict:
        return {}
    result: dict[str, object] = {}
    if "supported_parameters" in metadata:
        parameters = _names(metadata["supported_parameters"], maximum=128)
        if parameters is not None:
            result["supported_parameters"] = parameters
    if "reasoning" in metadata:
        reasoning = _reasoning(metadata["reasoning"])
        if reasoning is not None:
            result["reasoning"] = reasoning
    return result


def openrouter_capability_facts(
    connection: ProviderConnection,
    model: ModelCatalogueEntry,
) -> tuple[CapabilityFact, ...]:
    """Derive facts from one current, actually discovered OpenRouter entry.

    The caller may persist frozen legacy facts in their existing table and
    resolve extended facts from this durable metadata. Manual overrides still
    pass through the unchanged precedence rules; this function creates none.
    """
    revision = model.discovery_revision
    if (
        connection.backend_type is not BackendType.OPENAI_COMPATIBLE_HTTP
        or connection.profile is not ProviderProfile.OPENROUTER
        or not connection.available
        or model.connection_id != connection.id
        or model.availability is not CatalogueAvailability.AVAILABLE
        or model.origin not in {CatalogueOrigin.DISCOVERED, CatalogueOrigin.MANUAL_CONFIRMED}
        or type(revision) is not int
        or revision <= 0
        or revision != connection.catalogue_revision
    ):
        return ()

    metadata = sanitize_openrouter_metadata(model.metadata)
    parameters = set(metadata.get("supported_parameters", ()))
    facts = [CapabilityFact(
        model.id,
        CapabilityKey.STREAMING.value,
        CapabilityState.SUPPORTED,
        CapabilitySource.TRUSTED_REGISTRY,
        revision,
        provenance={
            "reason": f"OpenRouter chat completions streaming contract: {_STREAMING_AUTHORITY}",
            "catalogue_revision": revision,
        },
        observed_at=model.discovered_at,
    )]

    def advertised(key: str, field: str) -> None:
        facts.append(CapabilityFact(
            model.id,
            key,
            CapabilityState.SUPPORTED,
            CapabilitySource.PROVIDER_METADATA,
            revision,
            provenance={"field": field, "catalogue_revision": revision},
            observed_at=model.discovered_at,
        ))

    for parameter, key in _PARAMETER_KEYS:
        if parameter in parameters:
            advertised(key, f"supported_parameters.{parameter}")
    for parameter in ("max_tokens", "max_completion_tokens"):
        if parameter in parameters:
            advertised(CapabilityKey.MAX_OUTPUT_TOKENS.value, f"supported_parameters.{parameter}")
            break

    reasoning = metadata.get("reasoning")
    if isinstance(reasoning, dict) and parameters.intersection({"reasoning", "reasoning_effort"}):
        efforts = reasoning.get("supported_efforts", ())
        # The provider accepts every gateway effort when this field is null.
        # A single boolean capability cannot represent only part of the
        # registry's low/medium/high choice set, so require all three.
        if "supported_efforts" in reasoning and (
            efforts is None or {"low", "medium", "high"}.issubset(efforts)
        ):
            advertised("request.reasoning_effort.level", "reasoning.supported_efforts")
        if (
            isinstance(efforts, list)
            and "none" in efforts
            and reasoning.get("mandatory") is not True
        ):
            advertised(CapabilityKey.REASONING_NONE.value, "reasoning.supported_efforts")
        if "reasoning" in parameters and reasoning.get("supports_max_tokens") is True:
            advertised("request.reasoning.token_budget", "reasoning.supports_max_tokens")

    # OpenRouter's logit_bias uses tokenizer IDs; the normalized B.O.T.S.
    # setting uses token text. Advertising one cannot establish the other.
    return tuple(sorted(facts, key=lambda fact: fact.key))
