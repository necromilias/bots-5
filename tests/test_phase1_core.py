from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.core.generation import GenerationCompleted, GenerationDelta, GenerationRequest
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database


def _application(tmp_path: Path, backend=None) -> BotsApplication:
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=16),
        backend or FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )


def test_event_bus_is_ordered_and_bounded():
    async def scenario():
        ids = Uuid7Factory()
        bus = EventBus(SystemClock(), ids, queue_size=1)
        subscription = bus.subscribe()
        first = await bus.publish("first")

        async def publish_second():
            return await bus.publish("second")

        task = asyncio.create_task(publish_second())
        await asyncio.sleep(0)
        assert not task.done()
        assert (await subscription.__anext__()).kind == "first"
        assert (await task).sequence == 2
        assert (await subscription.__anext__()).kind == "second"
        subscription.close()
        assert first.sequence == 1

    asyncio.run(scenario())


def test_closing_a_full_subscription_unblocks_publishing():
    async def scenario():
        ids = Uuid7Factory()
        bus = EventBus(SystemClock(), ids, queue_size=1)
        subscription = bus.subscribe()
        await bus.publish("first")

        publishing = asyncio.create_task(bus.publish("second"))
        await asyncio.sleep(0)
        assert not publishing.done()

        subscription.close()
        event = await asyncio.wait_for(publishing, timeout=1)
        assert event.sequence == 2
        with pytest.raises(StopAsyncIteration):
            await subscription.__anext__()

    asyncio.run(scenario())


class _BlockingBackend:
    def __init__(self):
        self.started = asyncio.Event()

    async def stream(self, request: GenerationRequest):
        self.started.set()
        yield GenerationDelta(request.attempt_id, "partial")
        await asyncio.Event().wait()


def test_application_close_closes_subscribers_before_cancelling_generation(tmp_path):
    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        backend = _BlockingBackend()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids, queue_size=2),
            backend,
            ids=ids,
            clock=clock,
        )
        subscription = application.subscribe()
        chat = await application.create_chat()
        assert (await subscription.__anext__()).kind == "chat_created"
        await application.send_message(chat.id, "hello")
        await asyncio.wait_for(backend.started.wait(), timeout=1)
        await asyncio.sleep(0.01)
        await asyncio.wait_for(application.close(), timeout=1)

        store = SQLiteAppStateStore.open(database)
        try:
            assert store.list_messages(chat.id)[-1].state.value == "aborted"
            assert store.list_generation_attempts(chat.id)[-1].state.value == "aborted"
        finally:
            store.close()

    asyncio.run(scenario())


def test_immediate_application_close_terminalizes_a_scheduled_generation(tmp_path):
    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids),
            _BlockingBackend(),
            ids=ids,
            clock=clock,
        )
        chat = await application.create_chat()
        await application.send_message(chat.id, "hello")
        await asyncio.wait_for(application.close(), timeout=1)

        store = SQLiteAppStateStore.open(database)
        try:
            assistant = store.list_messages(chat.id)[-1]
            attempt = store.list_generation_attempts(chat.id)[-1]
            assert assistant.state.value == "aborted"
            assert attempt.state.value == "aborted"
            assert attempt.ended_at is not None
        finally:
            store.close()

    asyncio.run(scenario())


class _NonStopBackend:
    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationDelta | GenerationCompleted]:
        yield GenerationDelta(request.attempt_id, "partial")
        yield GenerationCompleted(request.attempt_id, finish_reason="length")


def test_non_stop_finish_is_not_marked_complete(tmp_path):
    async def scenario():
        application = _application(tmp_path, backend=_NonStopBackend())
        chat = await application.create_chat()
        await application.send_message(chat.id, "hello")
        await asyncio.sleep(0.01)
        _, messages = await application.open_chat(chat.id)
        assert messages[-1].state.value == "truncated"
        assert messages[-1].content == "partial"
        await application.close()

    asyncio.run(scenario())


def test_application_persists_a_streaming_turn(tmp_path):
    async def scenario():
        application = _application(tmp_path)
        subscription = application.subscribe()
        chat = await application.create_chat()
        await application.send_message(chat.id, "hello")
        kinds = [
            (await subscription.__anext__()).kind
            for _ in range(7)
        ]
        await asyncio.sleep(0.01)
        _, messages = await application.open_chat(chat.id)
        assert kinds[:3] == ["chat_created", "message_sent", "generation_started"]
        assert "message_delta" in kinds
        assert [(message.role.value, message.state.value, message.content) for message in messages] == [
            ("user", "sent", "hello"),
            ("assistant", "complete", "fake response to: hello"),
        ]
        await application.close()

    asyncio.run(scenario())
