from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    system: str
    user: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class CompletionResult:
    output_text: str
    requested_model: str
    returned_model: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None
    duration_seconds: float = 0.0


class Provider(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        ...
