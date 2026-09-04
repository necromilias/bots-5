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
