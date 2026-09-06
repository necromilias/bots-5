from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal


class BackendType(StrEnum):
    FAKE = "fake"
    OPENAI_COMPATIBLE_HTTP = "openai_compatible_http"


class ProviderProfile(StrEnum):
    GENERIC = "generic"
    OPENROUTER = "openrouter"


class CredentialSource(StrEnum):
    NONE = "none"
    ENVIRONMENT = "environment"
    SECRET_SERVICE = "secret_service"


class CredentialRequirement(StrEnum):
    NOT_USED = "not_used"
    OPTIONAL = "optional"
    REQUIRED = "required"


class DiscoveryKind(StrEnum):
    NONE = "none"
    FAKE = "fake"
    OPENAI_COMPATIBLE_HTTP = "openai_compatible_http"


@dataclass(frozen=True, slots=True)
class ConnectionDefinition:
    """Closed built-in description of one operator-selectable connection shape."""

    key: str
    display_name: str
    backend_type: BackendType
    profile: ProviderProfile
    endpoint_required: bool
    default_endpoint: str | None
    credential_requirement: CredentialRequirement
    credential_sources: tuple[CredentialSource, ...]
    discovery_kind: DiscoveryKind


BUILTIN_CONNECTION_DEFINITIONS = (
    ConnectionDefinition(
        key="fake",
        display_name="Deterministic fake",
        backend_type=BackendType.FAKE,
        profile=ProviderProfile.GENERIC,
        endpoint_required=False,
        default_endpoint=None,
        credential_requirement=CredentialRequirement.NOT_USED,
        credential_sources=(CredentialSource.NONE,),
        discovery_kind=DiscoveryKind.FAKE,
    ),
    ConnectionDefinition(
        key="local_ollama",
        display_name="Local Ollama / OpenAI-compatible",
        backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=ProviderProfile.GENERIC,
        endpoint_required=True,
        default_endpoint="http://localhost:11434/v1",
        credential_requirement=CredentialRequirement.OPTIONAL,
        credential_sources=(
            CredentialSource.NONE,
            CredentialSource.ENVIRONMENT,
            CredentialSource.SECRET_SERVICE,
        ),
        discovery_kind=DiscoveryKind.OPENAI_COMPATIBLE_HTTP,
    ),
    ConnectionDefinition(
        key="generic_openai_compatible",
        display_name="Generic OpenAI-compatible HTTP",
        backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=ProviderProfile.GENERIC,
        endpoint_required=True,
        default_endpoint=None,
        credential_requirement=CredentialRequirement.OPTIONAL,
        credential_sources=(
            CredentialSource.NONE,
            CredentialSource.ENVIRONMENT,
            CredentialSource.SECRET_SERVICE,
        ),
        discovery_kind=DiscoveryKind.OPENAI_COMPATIBLE_HTTP,
    ),
    ConnectionDefinition(
        key="openrouter",
        display_name="OpenRouter",
        backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=ProviderProfile.OPENROUTER,
        endpoint_required=True,
        default_endpoint="https://openrouter.ai/api/v1",
        credential_requirement=CredentialRequirement.REQUIRED,
        credential_sources=(CredentialSource.ENVIRONMENT, CredentialSource.SECRET_SERVICE),
        discovery_kind=DiscoveryKind.OPENAI_COMPATIBLE_HTTP,
    ),
)


def builtin_connection_definitions() -> tuple[ConnectionDefinition, ...]:
    """Return the immutable built-in choices exposed by the desktop form."""

    return BUILTIN_CONNECTION_DEFINITIONS


def connection_definition(key: str) -> ConnectionDefinition:
    for definition in BUILTIN_CONNECTION_DEFINITIONS:
        if definition.key == key:
            return definition
    raise ValueError(f"unknown built-in connection definition: {key}")


class CatalogueOrigin(StrEnum):
    MANUAL = "manual"
    DISCOVERED = "discovered"
    MANUAL_CONFIRMED = "manual_confirmed"


class CatalogueAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    DISCONNECTED = "disconnected"


class CatalogueRefreshStatus(StrEnum):
    NEVER = "never"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CatalogueRefreshFailureClass(StrEnum):
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    PROVIDER_HTTP = "provider_http"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


CATALOGUE_REFRESH_FAILURE_MESSAGES = {
    CatalogueRefreshFailureClass.TRANSPORT: "provider is unreachable",
    CatalogueRefreshFailureClass.TIMEOUT: "provider discovery timed out",
    CatalogueRefreshFailureClass.PROVIDER_HTTP: "provider returned an HTTP error",
    CatalogueRefreshFailureClass.PROTOCOL: "provider returned an invalid model catalogue",
    CatalogueRefreshFailureClass.UNKNOWN: "provider discovery failed",
}


class CapabilityState(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilitySource(StrEnum):
    MANUAL = "manual"
    CONFIRMED_ENDPOINT = "confirmed_endpoint"
    PROVIDER_METADATA = "provider_metadata"
    TRUSTED_REGISTRY = "trusted_registry"
    HEURISTIC = "heuristic"
    UNKNOWN = "unknown"


class CapabilityKey(StrEnum):
    STREAMING = "generation.streaming"
    TEMPERATURE = "request.temperature"
    MAX_OUTPUT_TOKENS = "request.max_output_tokens"
    REASONING_NONE = "request.reasoning_effort.none"
    CONTEXT_TOKENS = "limits.context_tokens"
    OUTPUT_TOKENS = "limits.output_tokens"
    USAGE = "telemetry.usage"
    REASONING_TOKENS = "telemetry.reasoning_tokens"
    COST = "telemetry.cost"
    REQUEST_ID = "telemetry.request_id"
    RETURNED_MODEL = "telemetry.returned_model"


CAPABILITY_KEYS = frozenset(item.value for item in CapabilityKey)


def validate_capability_value(key: str, state: CapabilityState, value: object) -> None:
    """Validate the bounded value shape for a capability fact or override."""
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("capability value is malformed")
    if key not in {
        CapabilityKey.CONTEXT_TOKENS.value,
        CapabilityKey.OUTPUT_TOKENS.value,
        CapabilityKey.MAX_OUTPUT_TOKENS.value,
    }:
        if value is not None:
            raise ValueError("non-limit capability must not carry a value")
    elif state is not CapabilityState.SUPPORTED and value is not None:
        raise ValueError("unsupported or unknown capability must not carry a limit")

PHASE5_SNAPSHOT_VERSION = 2


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    id: str
    name: str
    backend_type: BackendType
    profile: ProviderProfile
    endpoint: str | None
    credential_source: CredentialSource = CredentialSource.NONE
    credential_reference: str | None = None
    enabled: bool = True
    retired: bool = False
    revision: int = 1
    catalogue_revision: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    catalogue_refresh_status: CatalogueRefreshStatus = CatalogueRefreshStatus.NEVER
    catalogue_refresh_revision: int = 0
    catalogue_refresh_at: datetime | None = None
    catalogue_refresh_failure_class: CatalogueRefreshFailureClass | None = None
    catalogue_refresh_failure_message: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and not self.retired


@dataclass(frozen=True, slots=True)
class ModelCatalogueEntry:
    id: str
    connection_id: str
    provider_model_id: str
    display_name: str
    origin: CatalogueOrigin
    availability: CatalogueAvailability
    discovery_revision: int | None = None
    discovered_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_manual(self) -> bool:
        return self.origin in {CatalogueOrigin.MANUAL, CatalogueOrigin.MANUAL_CONFIRMED}


@dataclass(frozen=True, slots=True)
class CapabilityFact:
    model_entry_id: str
    key: str
    state: CapabilityState
    source: CapabilitySource
    source_revision: int | None = None
    value: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CapabilityOverride:
    model_entry_id: str
    key: str
    state: CapabilityState
    value: int | None = None
    reason: str | None = None
    revision: int = 1
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCapability:
    key: str
    state: CapabilityState
    source: CapabilitySource
    source_revision: int | None = None
    value: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: Literal["none"] | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ResolvedGenerationSettings:
    temperature: float
    max_output_tokens: int
    reasoning_effort: Literal["none"] | None
    timeout_seconds: float | None
    provenance: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ModelSelection:
    chat_id: str
    model_entry_id: str | None
    selection_required: bool
    revision: int


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    source: CredentialSource
    reference: str | None
    status: str


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    request: Any
    attempt_connection_id: str
    model_entry_id: str
    connection_revision: int
    catalogue_revision: int
    capabilities: tuple[ResolvedCapability, ...]
    settings: ResolvedGenerationSettings
