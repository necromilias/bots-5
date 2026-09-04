from __future__ import annotations

import asyncio

from bots5.core.generation import GenerationCompleted, GenerationDelta, GenerationRequest
from bots5.infrastructure.generation.fake import FakeStreamingBackend


def test_fake_backend_emits_deterministic_deltas_and_terminal_event():
    async def scenario():
        request = GenerationRequest(
            attempt_id="attempt",
            chat_id="chat",
            user_message_id="message",
            backend_id="fake",
            model="fake-v0.1",
            prompt="hello",
        )
        events = [event async for event in FakeStreamingBackend().stream(request)]
        assert [event.text for event in events if isinstance(event, GenerationDelta)] == [
            "fake",
            " response",
            " to: ",
            "hello",
        ]
        assert isinstance(events[-1], GenerationCompleted)

    asyncio.run(scenario())
