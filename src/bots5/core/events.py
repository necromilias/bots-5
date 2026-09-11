from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from bots5.domain.clock import Clock
from bots5.domain.ids import IdFactory

from .errors import StateError


@dataclass(frozen=True, slots=True)
class CoreEvent:
    event_id: str
    sequence: int
    kind: str
    occurred_at: datetime
    payload: Mapping[str, Any]


class EventSubscription(AsyncIterator[CoreEvent]):
    def __init__(self, bus: EventBus, queue: asyncio.Queue[CoreEvent | None]):
        self._bus = bus
        self._queue = queue
        self._closed = False
        self._closed_event = asyncio.Event()

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> CoreEvent:
        if self._closed:
            raise StopAsyncIteration
        get_task = asyncio.create_task(self._queue.get())
        close_task = asyncio.create_task(self._closed_event.wait())
        try:
            done, _ = await asyncio.wait(
                (get_task, close_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if close_task in done:
                raise StopAsyncIteration
            event = get_task.result()
            if event is None:
                self._closed = True
                raise StopAsyncIteration
            return event
        finally:
            for task in (get_task, close_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, close_task, return_exceptions=True)

    async def _deliver(self, event: CoreEvent) -> None:
        self._bus._assert_delivery_effect()
        if self._closed:
            return
        put_task = asyncio.create_task(self._queue.put(event))
        close_task = asyncio.create_task(self._closed_event.wait())
        try:
            done, _ = await asyncio.wait(
                (put_task, close_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if close_task in done and not put_task.done():
                put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
        finally:
            if not put_task.done():
                put_task.cancel()
            if not close_task.done():
                close_task.cancel()
            await asyncio.gather(put_task, close_task, return_exceptions=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self._bus._remove(self._queue)


class EventBus:
    def __init__(self, clock: Clock, ids: IdFactory, *, queue_size: int = 128):
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._clock = clock
        self._ids = ids
        self._queue_size = queue_size
        self._subscriptions: set[EventSubscription] = set()
        self._sequence = 0
        self._publish_lock = asyncio.Lock()
        self._closed = False
        self._effect_scope_factory = None
        self._issued_effect_factory = None
        self._delivery_owner: ContextVar[asyncio.Task | None] = ContextVar(
            f"bots5_event_delivery_owner_{id(self)}", default=None
        )

    def bind_effect_authority(self, admission, issued_effect) -> None:
        """Bind one production authority contract; test-only buses may stay unbound."""
        if self._effect_scope_factory is not None:
            raise StateError("event bus effect authority is already bound")
        self._effect_scope_factory = admission
        self._issued_effect_factory = issued_effect

    def _effect_scope(self):
        factory = self._effect_scope_factory
        return nullcontext() if factory is None else factory()

    def _issued_effect(self):
        factory = self._issued_effect_factory
        if factory is None:
            return nullcontext()

        @contextmanager
        def owned_delivery():
            with factory():
                owner = asyncio.current_task()
                token = self._delivery_owner.set(owner)
                try:
                    yield
                finally:
                    self._delivery_owner.reset(token)

        return owned_delivery()

    def _assert_delivery_effect(self) -> None:
        if self._issued_effect_factory is None:
            return
        owner = asyncio.current_task()
        if owner is None or self._delivery_owner.get() is not owner:
            raise StateError("event delivery has no issued effect grant")

    def subscribe(self) -> EventSubscription:
        with self._effect_scope():
            if self._closed:
                raise StateError("event bus is closed")
            queue: asyncio.Queue[CoreEvent | None] = asyncio.Queue(maxsize=self._queue_size)
            subscription = EventSubscription(self, queue)
            self._subscriptions.add(subscription)
            return subscription

    async def publish(self, kind: str, **payload: Any) -> CoreEvent:
        with self._effect_scope():
            async with self._publish_lock:
                if self._closed:
                    raise StateError("event bus is closed")
                self._sequence += 1
                event = CoreEvent(
                    event_id=self._ids.new(),
                    sequence=self._sequence,
                    kind=kind,
                    occurred_at=self._clock.now(),
                    payload=MappingProxyType(dict(payload)),
                )
                for subscription in tuple(self._subscriptions):
                    with self._issued_effect():
                        await subscription._deliver(event)
                return event

    def _remove(self, queue: asyncio.Queue[CoreEvent | None]) -> None:
        self._subscriptions = {
            subscription
            for subscription in self._subscriptions
            if subscription._queue is not queue
        }

    def close(self) -> None:
        self._closed = True
        for subscription in tuple(self._subscriptions):
            subscription.close()
