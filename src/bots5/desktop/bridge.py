from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, Signal

from bots5.core.application import BotsApplication
from bots5.core.events import CoreEvent, EventSubscription


class CoreEventBridge(QObject):
    event_received = Signal(object)

    def __init__(self, application: BotsApplication):
        super().__init__()
        self._application = application
        self._subscription: EventSubscription | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._subscription = self._application.subscribe()
            self._task = asyncio.create_task(
                self._consume(self._subscription),
                name="bots5-qt-event-bridge",
            )

    async def _consume(self, subscription: EventSubscription) -> None:
        try:
            async for event in subscription:
                self.event_received.emit(event)
        except asyncio.CancelledError:
            raise
        finally:
            subscription.close()
            if self._subscription is subscription:
                self._subscription = None

    def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def stop_async(self) -> None:
        self.stop()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
