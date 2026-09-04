from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from .errors import StateError


class ExecutionManager:
    """Owns application tasks so shutdown has one explicit cancellation boundary."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def start(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        if self._closed:
            coroutine.close()
            raise StateError("execution manager is shut down")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        self._closed = True
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            task.cancel()
        failures: list[BaseException] = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures.extend(
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            )
        self._tasks.clear()
        if failures:
            raise failures[0]
