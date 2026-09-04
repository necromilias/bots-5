from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class GenerationDelta:
    attempt_id: str
    text: str


@dataclass(frozen=True, slots=True)
class GenerationCompleted:
    attempt_id: str
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class GenerationFailed:
    attempt_id: str
    error_type: str
    error_message: str


GenerationEvent = GenerationDelta | GenerationCompleted | GenerationFailed


class GenerationBackend(Protocol):
    def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        ...
