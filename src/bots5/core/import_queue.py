"""Pure, deterministic Archive-import queue state machine.

Persistence owns compare-and-set and journalling; this module owns the closed
state language so callers cannot turn a failed/committing import into an
implicit retry.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import asyncio
from collections.abc import Awaitable, Callable


class ImportQueueError(ValueError):
    pass


class ImportQueueState(StrEnum):
    QUEUED = "QUEUED"
    PREFLIGHTING = "PREFLIGHTING"
    STAGING = "STAGING"
    COMMITTING = "COMMITTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TERMINAL = frozenset({ImportQueueState.COMPLETED, ImportQueueState.FAILED, ImportQueueState.CANCELLED})
_ALLOWED = {
    ImportQueueState.QUEUED: frozenset({ImportQueueState.PREFLIGHTING, ImportQueueState.CANCELLED}),
    ImportQueueState.PREFLIGHTING: frozenset({ImportQueueState.QUEUED, ImportQueueState.STAGING, ImportQueueState.FAILED, ImportQueueState.CANCELLED}),
    ImportQueueState.STAGING: frozenset({ImportQueueState.COMMITTING, ImportQueueState.FAILED}),
    ImportQueueState.COMMITTING: frozenset({ImportQueueState.COMPLETED, ImportQueueState.FAILED}),
    ImportQueueState.COMPLETED: frozenset(),
    ImportQueueState.FAILED: frozenset(),
    ImportQueueState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class QueueItem:
    id: str
    revision: int
    ordinal: int
    state: ImportQueueState
    source_fingerprint: tuple[int, int, int, int, int]
    failure_code: str | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id or self.revision < 1 or self.ordinal < 0 or len(self.source_fingerprint) != 5:
            raise ImportQueueError("queue item is malformed")
        if self.state is ImportQueueState.FAILED:
            if not self.failure_code:
                raise ImportQueueError("failed queue item lacks a code")
        elif self.failure_code is not None:
            raise ImportQueueError("nonfailed queue item has a failure code")


@dataclass(frozen=True, slots=True)
class CutoffResult:
    """A durable cutoff result plus cancellation observed while it crossed."""

    value: object
    deferred_cancellations: int = 0


@dataclass(frozen=True, slots=True)
class QueuePage:
    items: tuple[QueueItem, ...]
    next_cursor: tuple[int, str] | None
    queue_revision: int

    def __post_init__(self) -> None:
        if type(self.queue_revision) is not int or self.queue_revision < 0:
            raise ImportQueueError("queue page revision is malformed")


def transition(item: QueueItem, expected_revision: int, target: ImportQueueState, *, failure_code: str | None = None, operation_id: str | None = None) -> QueueItem:
    """Apply one CAS transition; the caller must persist it atomically."""
    if item.revision != expected_revision:
        raise ImportQueueError("queue revision conflict")
    if target not in _ALLOWED[item.state]:
        raise ImportQueueError("queue transition is not allowed")
    if target is ImportQueueState.FAILED:
        if not failure_code:
            raise ImportQueueError("failed transition lacks a code")
    elif failure_code is not None:
        raise ImportQueueError("only failed transition has a code")
    if item.state is ImportQueueState.PREFLIGHTING and target is ImportQueueState.STAGING and not operation_id:
        raise ImportQueueError("cutoff transition lacks an operation identity")
    if operation_id is not None and item.operation_id is not None and operation_id != item.operation_id:
        raise ImportQueueError("queue operation identity is immutable")
    return replace(item, revision=item.revision + 1, state=target, failure_code=failure_code, operation_id=operation_id or item.operation_id)


def reorder(items: tuple[QueueItem, ...], ordered_ids: tuple[str, ...]) -> tuple[QueueItem, ...]:
    """Assign dense ordinals inside the singleton queue-control CAS."""
    by_id = {item.id: item for item in items}
    if len(by_id) != len(items) or set(ordered_ids) != set(by_id):
        raise ImportQueueError("queue reorder does not cover exactly the queue")
    if any(by_id[key].state is not ImportQueueState.QUEUED for key in ordered_ids):
        raise ImportQueueError("only waiting queue rows are reorderable")
    return tuple(replace(by_id[key], ordinal=index, revision=by_id[key].revision + 1) for index, key in enumerate(ordered_ids))


def can_cancel(item: QueueItem) -> bool:
    return item.state in {ImportQueueState.QUEUED, ImportQueueState.PREFLIGHTING}


class OwnedImportWorkers:
    """Keep post-cutoff settlement outside ordinary cancellable work.

    The application supplies a store whose independent command admission owns
    the authority grant.  Cancellation wins only before ``cutoff`` returns;
    afterward the registered settlement task remains visible to shutdown.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._tasks: set[asyncio.Task[object]] = set()
        self._preflight: set[asyncio.Task[object]] = set()
        self._by_queue_id: dict[str, asyncio.Task[object]] = {}
        self._closed = False

    def start(self, preflight: Callable[[], Awaitable[object]], cutoff: Callable[[object], Awaitable[object]], settle: Callable[[object], Awaitable[object]], *, queue_id: str | None = None) -> asyncio.Task[object]:
        if self._closed:
            raise ImportQueueError("import worker registry is closed")
        if queue_id is not None and queue_id in self._by_queue_id:
            raise ImportQueueError("archive import already has an owned worker")

        async def runner() -> object:
            with self._store.command_admission(independent=True):
                prepared = await preflight()
                cutoff_result = await cutoff(prepared)
                if isinstance(cutoff_result, CutoffResult):
                    committed = cutoff_result.value
                    deferred_cancellations = cutoff_result.deferred_cancellations
                else:
                    committed = cutoff_result
                    deferred_cancellations = 0
                # The original command grant remains live after the first
                # journal effect.  Shutdown can no longer cancel this runner;
                # it must drain its settlement under that same grant.
                current = asyncio.current_task()
                if current is not None:
                    self._preflight.discard(current)
                settlement = asyncio.create_task(settle(committed), name="archive-import-settlement")
                self._tasks.add(settlement)
                settlement.add_done_callback(self._tasks.discard)
                while True:
                    try:
                        settled = await asyncio.shield(settlement)
                        break
                    except asyncio.CancelledError:
                        # Do not let caller cancellation revoke the cutoff-
                        # owning grant or cancel its settlement.  Every
                        # cancellation after cutoff is reported only after the
                        # exact plan has been durably settled.
                        deferred_cancellations += 1
                        current = asyncio.current_task()
                        if current is not None:
                            current.uncancel()
                if deferred_cancellations:
                    raise asyncio.CancelledError
                return settled

        task: asyncio.Task[object] = asyncio.create_task(runner(), name="archive-import-worker")
        self._tasks.add(task)
        self._preflight.add(task)
        def forget(done: asyncio.Task[object]) -> None:
            self._tasks.discard(done)
            self._preflight.discard(done)
            if queue_id is not None and self._by_queue_id.get(queue_id) is done:
                self._by_queue_id.pop(queue_id, None)
        if queue_id is not None:
            self._by_queue_id[queue_id] = task
        task.add_done_callback(forget)
        return task

    def cancel_preflight(self, queue_id: str) -> bool:
        """Ask the still-cancellable owned worker to observe durable cancel."""
        task = self._by_queue_id.get(queue_id)
        if task is None or task not in self._preflight or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        self._closed = True
        # Only workers which have not crossed the durable cutoff are
        # cancellable.  Cutoff-past runners retain their command grant while
        # their shielded settlement is drained.
        preflight = tuple(task for task in self._preflight if not task.done())
        for task in preflight:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
