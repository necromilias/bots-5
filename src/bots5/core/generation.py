from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class GenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str
    chat_id: str
    user_message_id: str
    backend_id: str
    model: str
    prompt: str
    provider_id: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    connection_id: str | None = None
    connection_name: str | None = None
    connection_revision: int | None = None
    model_entry_id: str | None = None
    catalogue_revision: int | None = None
    credential_source: str | None = None
    credential_reference: str | None = None
    credential_status: str | None = None
    provider_profile: str | None = None
    effective_settings: dict[str, object] | None = None
    settings_provenance: dict[str, str] | None = None
    capabilities: list[dict[str, object]] | None = None
    capability_provenance: dict[str, object] | None = None
    manual_overrides: dict[str, object] | None = None
    omitted_settings: dict[str, str] | None = None
    timeout_seconds: float | None = None
    # Phase 6: already-built adapter-owned request material.  Providers must
    # use this verbatim; the field is optional for Phase 1-5 compatibility.
    system_prompt: str | None = None
    wire_representation: bytes | None = None
    context_plan_digest: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationDelta:
    attempt_id: str
    text: str


@dataclass(frozen=True, slots=True)
class GenerationDispatched:
    attempt_id: str


@dataclass(frozen=True, slots=True)
class GenerationMetadata:
    attempt_id: str
    returned_model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class GenerationCompleted:
    attempt_id: str
    finish_reason: str = "stop"
    returned_model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None
    remote_outcome_unknown: bool = False


@dataclass(frozen=True, slots=True)
class GenerationFailed:
    attempt_id: str
    error_type: str
    error_message: str
    remote_outcome_unknown: bool | None = None


GenerationEvent = (
    GenerationDelta
    | GenerationDispatched
    | GenerationMetadata
    | GenerationCompleted
    | GenerationFailed
)


class GenerationBackend(Protocol):
    def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        ...
