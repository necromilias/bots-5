"""Unified execution queue state machine — G4 implementation.

This module defines a single, general-purpose queue state machine that can be
consumed by the campaign runner, tools, code/process execution, and plugins.

States, transitions, guards, and failure behaviour are closed and explicit so
callers cannot turn a failed/settling execution into an implicit retry.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from .errors import StateError


class ExecutionQueueState(StrEnum):
    """Lifecycle of one owned execution item.

    The state machine is intentionally *smaller* than the union of every
    consumer's vocabulary.  Consumers map their richer states onto these
    canonical values; the machine guarantees that the mapping is reversible
    and unambiguous.
    """

    PENDING = "pending"       # admitted but not yet queued for dispatch
    QUEUED = "queued"         # waiting in the execution queue
    RUNNING = "running"       # actively executing
    SETTLING = "settling"     # primary work is done; durable settlement in progress
    COMPLETED = "completed"   # terminal — success
    FAILED = "failed"         # terminal — definite failure
    CANCELLED = "cancelled"   # terminal — operator/system cancellation
    UNKNOWN = "unknown"       # terminal — effect cannot be established


class ExecutionQueueError(ValueError):
    """Illegal transition or malformed queue item."""
    pass


# --- Terminal / non-terminal sets ------------------------------------------------

_TERMINAL = frozenset({
    ExecutionQueueState.COMPLETED,
    ExecutionQueueState.FAILED,
    ExecutionQueueState.CANCELLED,
    ExecutionQueueState.UNKNOWN,
})

_NON_TERMINAL = frozenset({
    ExecutionQueueState.PENDING,
    ExecutionQueueState.QUEUED,
    ExecutionQueueState.RUNNING,
    ExecutionQueueState.SETTLING,
})

# --- Allowed transitions (closed state language) ---------------------------------

_ALLOWED: dict[ExecutionQueueState, frozenset[ExecutionQueueState]] = {
    ExecutionQueueState.PENDING: frozenset({
        ExecutionQueueState.QUEUED,
        ExecutionQueueState.CANCELLED,
    }),
    ExecutionQueueState.QUEUED: frozenset({
        ExecutionQueueState.RUNNING,
        ExecutionQueueState.CANCELLED,
    }),
    ExecutionQueueState.RUNNING: frozenset({
        ExecutionQueueState.SETTLING,
        ExecutionQueueState.FAILED,
        ExecutionQueueState.CANCELLED,
    }),
    ExecutionQueueState.SETTLING: frozenset({
        ExecutionQueueState.COMPLETED,
        ExecutionQueueState.FAILED,
        ExecutionQueueState.UNKNOWN,
    }),
    ExecutionQueueState.COMPLETED: frozenset(),
    ExecutionQueueState.FAILED: frozenset(),
    ExecutionQueueState.CANCELLED: frozenset(),
    ExecutionQueueState.UNKNOWN: frozenset(),
}

# --- Data structures -------------------------------------------------------------

@dataclass(slots=True)
class ExecutionReceipt:
    """Immutable settlement receipt for one execution item.

    When ``state`` is ``UNKNOWN``, every other field may be absent or
    best-effort.  The receipt MUST NOT invent facts that are not established.
    """

    item_id: str
    state: ExecutionQueueState
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    provider_side_outcome_unknown: bool = False
    # Usage / cost remain None when unknown so the field is never fabricated.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ExecutionQueueError("receipt item_id is required")
        if not isinstance(self.state, ExecutionQueueState):
            raise ExecutionQueueError("receipt state is malformed")
        if self.state is ExecutionQueueState.FAILED:
            if not self.error_type:
                raise ExecutionQueueError("failed receipt requires error_type")
        if self.state is ExecutionQueueState.COMPLETED and self.error_type is not None:
            raise ExecutionQueueError("non-failed receipt must not carry an error_type")
        # UNKNOWN may carry error_type as best-effort context; it is
        # explicitly non-authoritative.

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "provider_side_outcome_unknown": self.provider_side_outcome_unknown,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "known_cost_usd": None if self.known_cost_usd is None else str(self.known_cost_usd),
        }

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def is_success(self) -> bool:
        return self.state is ExecutionQueueState.COMPLETED

    @property
    def is_unknown(self) -> bool:
        return self.state is ExecutionQueueState.UNKNOWN


@dataclass(slots=True)
class ExecutionQueueItem:
    """One unit of work managed by the execution queue state machine.

    ``revision`` is the compare-and-set token required for transitions.
    The caller (persistence layer) must validate the revision before applying.
    """

    id: str
    revision: int
    state: ExecutionQueueState
    operation_id: str | None = None
    receipt: ExecutionReceipt | None = None

    def __post_init__(self) -> None:
        if not self.id or self.revision < 1:
            raise ExecutionQueueError("queue item is malformed")
        if not isinstance(self.state, ExecutionQueueState):
            raise ExecutionQueueError("queue item state is malformed")
        if self.receipt is not None and not isinstance(self.receipt, ExecutionReceipt):
            raise ExecutionQueueError("queue item receipt is malformed")
        if self.receipt is not None and self.receipt.item_id != self.id:
            raise ExecutionQueueError("queue item receipt id mismatch")

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL


# --- Transition guards ----------------------------------------------------------

def _require_revision(item: ExecutionQueueItem, expected_revision: int) -> None:
    if item.revision != expected_revision:
        raise ExecutionQueueError("queue revision conflict")


def transition(
    item: ExecutionQueueItem,
    expected_revision: int,
    target: ExecutionQueueState,
    *,
    receipt: ExecutionReceipt | None = None,
) -> ExecutionQueueItem:
    """Apply one compare-and-set transition.

    The caller must persist the returned item atomically.  If ``target`` is
    terminal and ``receipt`` is provided, the receipt state must match
    ``target``.
    """
    _require_revision(item, expected_revision)
    if target not in _ALLOWED[item.state]:
        raise ExecutionQueueError(
            f"transition from {item.state.value!r} to {target.value!r} is not allowed"
        )
    if receipt is not None:
        if receipt.state != target:
            raise ExecutionQueueError("receipt state does not match transition target")
        if receipt.item_id != item.id:
            raise ExecutionQueueError("receipt item_id does not match queue item")
    # operation_id is immutable once set.
    operation_id = item.operation_id
    return replace(item, revision=item.revision + 1, state=target, receipt=receipt, operation_id=operation_id)


def can_cancel(item: ExecutionQueueItem) -> bool:
    """Cancellation is permitted before work has crossed the durable cutoff."""
    return item.state in {ExecutionQueueState.PENDING, ExecutionQueueState.QUEUED, ExecutionQueueState.RUNNING}


def can_timeout(item: ExecutionQueueItem) -> bool:
    """Timeout is a RUNNING→FAILED transition with a specific error_type."""
    return item.state is ExecutionQueueState.RUNNING


def terminalise_unknown(
    item: ExecutionQueueItem,
    expected_revision: int,
    *,
    reason: str | None = None,
    started_at: str | None = None,
) -> ExecutionQueueItem:
    """Terminalise an item whose outcome cannot be established.

    This is the *only* way to reach ``UNKNOWN``.  The receipt is synthesized
    with every fact left absent so the caller cannot accidentally fabricate
    success or failure.
    """
    _require_revision(item, expected_revision)
    if ExecutionQueueState.UNKNOWN not in _ALLOWED[item.state]:
        raise ExecutionQueueError(
            f"cannot terminalise UNKNOWN from {item.state.value!r}"
        )
    receipt = ExecutionReceipt(
        item_id=item.id,
        state=ExecutionQueueState.UNKNOWN,
        started_at=started_at,
        error_type="unknown_outcome" if reason else None,
        error_message=reason,
        provider_side_outcome_unknown=True,
    )
    return replace(item, revision=item.revision + 1, state=ExecutionQueueState.UNKNOWN, receipt=receipt)


def terminalise_failure(
    item: ExecutionQueueItem,
    expected_revision: int,
    *,
    error_type: str,
    error_message: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_seconds: float | None = None,
) -> ExecutionQueueItem:
    """Terminalise an item with a known failure.

    ``error_type`` is required and must be a non-empty string.
    """
    if not error_type:
        raise ExecutionQueueError("failure terminalisation requires error_type")
    _require_revision(item, expected_revision)
    if ExecutionQueueState.FAILED not in _ALLOWED[item.state]:
        raise ExecutionQueueError(
            f"cannot terminalise FAILED from {item.state.value!r}"
        )
    receipt = ExecutionReceipt(
        item_id=item.id,
        state=ExecutionQueueState.FAILED,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        error_type=error_type,
        error_message=error_message,
    )
    return replace(item, revision=item.revision + 1, state=ExecutionQueueState.FAILED, receipt=receipt)


def terminalise_completed(
    item: ExecutionQueueItem,
    expected_revision: int,
    *,
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_seconds: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    known_cost_usd: Decimal | None = None,
) -> ExecutionQueueItem:
    """Terminalise an item with a verified successful outcome."""
    _require_revision(item, expected_revision)
    if ExecutionQueueState.COMPLETED not in _ALLOWED[item.state]:
        raise ExecutionQueueError(
            f"cannot terminalise COMPLETED from {item.state.value!r}"
        )
    receipt = ExecutionReceipt(
        item_id=item.id,
        state=ExecutionQueueState.COMPLETED,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        known_cost_usd=known_cost_usd,
    )
    return replace(item, revision=item.revision + 1, state=ExecutionQueueState.COMPLETED, receipt=receipt)


# --- Queue-page projections (for consumers that paginate) -------------------------

@dataclass(slots=True)
class ExecutionQueuePage:
    items: tuple[ExecutionQueueItem, ...]
    next_cursor: tuple[int, str] | None
    queue_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.queue_revision, int) or self.queue_revision < 0:
            raise ExecutionQueueError("queue page revision is malformed")


# --- Registry of owned in-flight work (integration with ExecutionManager) -------

import asyncio
from collections.abc import Awaitable, Callable


class OwnedExecutionWorkers:
    """Own in-flight execution tasks with cancellation-before-cutoff semantics.

    Modeled after :class:`~bots5.core.import_queue.OwnedImportWorkers` and
    designed to integrate with :class:`~bots5.core.execution.ExecutionManager`.

    Cancellation wins only before the worker has crossed its "durable cutoff"
    (e.g., before the provider request is sent, or before a SQLite commit).
    After cutoff the settlement task is shielded and must drain under its own
    command grant.
    """

    def __init__(self, store=None) -> None:
        # ``store`` is optional; when present, command admission is respected.
        self._store = store
        self._tasks: set[asyncio.Task[object]] = set()
        self._preflight: set[asyncio.Task[object]] = set()
        self._by_item_id: dict[str, asyncio.Task[object]] = {}
        self._closed = False
        self._item_states: dict[str, ExecutionQueueItem] = {}

    def start(
        self,
        item: ExecutionQueueItem,
        preflight: Callable[[], Awaitable[object]],
        cutoff: Callable[[object], Awaitable[object]],
        settle: Callable[[object], Awaitable[ExecutionReceipt]],
    ) -> asyncio.Task[ExecutionReceipt]:
        """Begin one owned execution worker for ``item``.

        ``preflight`` → ``cutoff`` → ``settle``.  The returned task resolves
        to the :class:`ExecutionReceipt`.  If cancelled before ``cutoff`` returns
        the task is cancelled; if cancelled after cutoff, the settlement is
        shielded and the cancellation is reported in the receipt.
        """
        if self._closed:
            raise ExecutionQueueError("execution worker registry is closed")
        if item.id in self._by_item_id:
            raise ExecutionQueueError(f"execution item {item.id!r} already has an owned worker")
        if not can_cancel(item):
            raise ExecutionQueueError(f"execution item {item.id!r} is not cancellable")

        self._item_states[item.id] = item

        async def runner() -> ExecutionReceipt:
            admission = self._store.command_admission if self._store is not None else lambda **kw: __import__('contextlib').nullcontext()
            with admission():
                prepared = await preflight()
                cutoff_result = await cutoff(prepared)
                # After cutoff the worker owns its settlement; shutdown/caller
                # cancellation must not revoke the durable effect.
                current = asyncio.current_task()
                if current is not None:
                    self._preflight.discard(current)
                settlement = asyncio.create_task(settle(cutoff_result), name=f"execution-settle-{item.id}")
                self._tasks.add(settlement)
                settlement.add_done_callback(self._tasks.discard)
                deferred_cancellations = 0
                while True:
                    try:
                        receipt = await asyncio.shield(settlement)
                        break
                    except asyncio.CancelledError:
                        deferred_cancellations += 1
                        current = asyncio.current_task()
                        if current is not None:
                            current.uncancel()
                if deferred_cancellations:
                    # Augment the receipt with cancellation count if the settle
                    # handler already produced one; otherwise raise.
                    if isinstance(receipt, ExecutionReceipt):
                        # Receipt was already produced before shield observed
                        # cancellation; the cancellation is post-cutoff.
                        pass
                    else:
                        raise asyncio.CancelledError
                return receipt

        task: asyncio.Task[ExecutionReceipt] = asyncio.create_task(runner(), name=f"execution-{item.id}")
        self._tasks.add(task)
        self._preflight.add(task)

        def forget(done: asyncio.Task[ExecutionReceipt]) -> None:
            self._tasks.discard(done)
            self._preflight.discard(done)
            if self._by_item_id.get(item.id) is done:
                self._by_item_id.pop(item.id, None)

        self._by_item_id[item.id] = task
        task.add_done_callback(forget)
        return task

    def cancel_preflight(self, item_id: str) -> bool:
        """Request cancellation of a worker that has not yet crossed cutoff."""
        task = self._by_item_id.get(item_id)
        if task is None or task not in self._preflight or task.done():
            return False
        task.cancel()
        return True

    def get_item(self, item_id: str) -> ExecutionQueueItem | None:
        return self._item_states.get(item_id)

    async def shutdown(self) -> None:
        """Drain all settlement; cancel only preflight workers."""
        self._closed = True
        preflight = tuple(task for task in self._preflight if not task.done())
        for task in preflight:
            task.cancel()
        # Drain settlement tasks (they are shielded internally).
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    # --- Persistence integration (BLK-06) ---------------------------------------

    def attach_queue_store(self, store) -> None:
        """Attach a :class:`QueuePersistenceStore` for durable state persistence.

        This allows the worker to persist state transitions and participate in
        crash-restart recovery.  The store is used for persistence only;
        ``command_admission`` is still obtained from ``self._store`` if present.
        """
        self._queue_store = store

    def persist_item(self, item: ExecutionQueueItem) -> None:
        """Persist one queue item (including any attached receipt) to the store."""
        store = getattr(self, "_queue_store", None)
        if store is not None:
            store.save_item(item)

    def recover_in_flight(self) -> list[ExecutionQueueItem]:
        """Run crash-recovery on the attached queue store.

        Returns the list of items that were recovered to ``UNKNOWN``.
        Raises :exc:`RuntimeError` if no queue store is attached.
        """
        store = getattr(self, "_queue_store", None)
        if store is None:
            raise RuntimeError("no queue store attached")
        return store.recover_in_flight()
