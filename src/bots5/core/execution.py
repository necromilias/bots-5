from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from .errors import StateError


class ExecutionManager:
    """Owns application tasks so shutdown has one explicit cancellation boundary."""

    def __init__(self) -> None:
        # One registry owns both work and the release obligations it creates.
        # Cleanup is drained, never cancelled by application shutdown.
        self._tasks: dict[asyncio.Task[Any], bool] = {}
        self._cleanup_failures: list[BaseException] = []
        self._closed = False
        self._draining = False

    def start(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
        cleanup: bool = False,
    ) -> asyncio.Task[Any]:
        # A shutting-down owned task may hand off its existing stream to an
        # owned release task. It may not admit new generation work.
        handoff = cleanup and self._draining and asyncio.current_task() in self._tasks
        if self._closed and not handoff:
            coroutine.close()
            raise StateError("execution manager is shut down")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks[task] = cleanup
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        cleanup = self._tasks.pop(task, False)
        if cleanup:
            try:
                task.result()
            except asyncio.CancelledError:
                self._cleanup_failures.append(StateError("generation stream cleanup was cancelled"))
            except BaseException as exc:
                self._cleanup_failures.append(exc)

    async def shutdown(self) -> None:
        self._closed = True
        failures: list[BaseException] = []
        self._draining = True
        try:
            # Generation cancellation can register cleanup while this gather
            # runs. Drain until all handoffs have settled, before store release.
            while self._tasks:
                tasks = tuple(self._tasks.items())
                for task, cleanup in tasks:
                    if not cleanup and not task.done() and not task.cancelling():
                        task.cancel()
                results = await asyncio.gather(
                    *(task for task, _ in tasks), return_exceptions=True
                )
                # Gathering already-finished tasks need not suspend. Retire
                # this snapshot directly, rather than waiting for callbacks
                # which cannot run while shutdown repeats that same snapshot.
                for task, _ in tasks:
                    self._finished(task)
                failures.extend(
                    result
                    for (_, cleanup), result in zip(tasks, results)
                    if not cleanup
                    and isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                )
        finally:
            self._draining = False
        failures.extend(self._cleanup_failures)
        if failures:
            raise failures[0]
