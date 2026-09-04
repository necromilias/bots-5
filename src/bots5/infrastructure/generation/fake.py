from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from bots5.core.generation import GenerationBackend, GenerationCompleted, GenerationDelta, GenerationRequest


class FakeStreamingBackend(GenerationBackend):
    def __init__(self, *, delay_seconds: float = 0.0):
        self.delay_seconds = delay_seconds

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationDelta | GenerationCompleted]:
        chunks = ("fake", " response", " to: ", request.prompt)
        for chunk in chunks:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            else:
                await asyncio.sleep(0)
            yield GenerationDelta(attempt_id=request.attempt_id, text=chunk)
        yield GenerationCompleted(attempt_id=request.attempt_id)
