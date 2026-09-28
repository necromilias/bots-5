"""Import-queue view model: projects the public display page into operator rows.

Consumes only the sealed :class:`~bots5.core.import_queue.QueueDisplayPage`
projection.  There is no SQL, no store reach-around and no Qt here: this module
is pure presentation state so the dock widget stays a thin client (SLICE_E_DESIGN.md
I1) and the refresh strategy stays testable.  ``source_path``, ``resolver_roots``
and raw ``options`` are deliberately absent from the projection and are never
recreated.

Row flags derive from ``state`` only, using the landed pure facts: a row is
cancellable exactly when it is QUEUED or PREFLIGHTING (pre-cutoff), removable
only while QUEUED, and retryable only when FAILED (retry always creates a fresh
row; there is no auto-retry).
"""

from __future__ import annotations

from dataclasses import dataclass

from bots5.core.import_queue import ImportQueueState, QueueDisplayPage


_TERMINAL_STATES = frozenset(
    {
        ImportQueueState.COMPLETED,
        ImportQueueState.FAILED,
        ImportQueueState.CANCELLED,
    }
)
_CANCELLABLE_STATES = frozenset(
    {ImportQueueState.QUEUED, ImportQueueState.PREFLIGHTING}
)
# States during which the queue control holds a worker claim: the landed store
# refuses reorder with "queue reorder is unavailable while an import is active"
# exactly while a claim exists (sqlite.py reorder_archive_imports).
_RUNNING_IMPORT_STATES = frozenset(
    {
        ImportQueueState.PREFLIGHTING,
        ImportQueueState.STAGING,
        ImportQueueState.COMMITTING,
    }
)

# Operator-readable labels for the landed failure codes.  The raw landed code
# always remains truthfully available beside the label (IMPORT_QUEUE_AND_
# CONTINUATION_UX.md §2.5).
FAILURE_LABELS = {
    "SOURCE_UNAVAILABLE": "Source unavailable",
    "ARCHIVE_INVALID": "Archive invalid",
    "RESOLVER_UNAVAILABLE": "Resolver unavailable",
    "RESOURCE_LIMIT": "Resource limit",
}


def failure_label_for(code: str | None) -> str:
    if not code:
        return ""
    return FAILURE_LABELS.get(code, code)


@dataclass(frozen=True, slots=True)
class QueueRowView:
    """One operator row derived only from the sealed display projection."""

    id: str
    ordinal: int
    revision: int  # read-only row CAS token, never editable domain state
    state: ImportQueueState
    source_label: str
    failure_code: str | None
    enqueued_at: str | None
    started_at: str | None
    finished_at: str | None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    @property
    def is_cancellable(self) -> bool:
        return self.state in _CANCELLABLE_STATES

    @property
    def is_removable(self) -> bool:
        return self.state is ImportQueueState.QUEUED

    @property
    def is_retryable(self) -> bool:
        return self.state is ImportQueueState.FAILED

    @property
    def failure_label(self) -> str:
        return failure_label_for(self.failure_code)


class ImportQueueViewModel:
    """Tracks last-observed rows, row CAS revisions and the page queue revision.

    The view model records the exact row ``revision`` it last observed so the
    dock can echo that token to cancel/remove, and the page-level
    ``queue_revision`` so reorder submits the observed queue CAS token.  A
    stale token is surfaced upstream as a refresh prompt; it is never silently
    retried here.
    """

    def __init__(self) -> None:
        self._rows: tuple[QueueRowView, ...] = ()
        self._queue_revision: int | None = None

    def apply_page(self, page: QueueDisplayPage) -> None:
        self._rows = tuple(
            QueueRowView(
                id=item.id,
                ordinal=item.ordinal,
                revision=item.revision,
                state=item.state,
                source_label=item.source_label,
                failure_code=item.failure_code,
                enqueued_at=item.enqueued_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
            )
            for item in page.items
        )
        self._queue_revision = page.queue_revision

    @property
    def rows(self) -> tuple[QueueRowView, ...]:
        return self._rows

    @property
    def queue_revision(self) -> int | None:
        return self._queue_revision

    @property
    def has_active(self) -> bool:
        """True while at least one row is non-terminal (drives bounded polling)."""

        return any(not row.is_terminal for row in self._rows)

    @property
    def has_queued(self) -> bool:
        """True while at least one waiting (reorderable/removable) row shows."""

        return any(row.is_removable for row in self._rows)

    @property
    def has_running_import(self) -> bool:
        """True while a worker claim exists (an import is actively running)."""

        return any(row.state in _RUNNING_IMPORT_STATES for row in self._rows)

    def row(self, queue_id: str) -> QueueRowView | None:
        return next((row for row in self._rows if row.id == queue_id), None)

    def observed_row_revision(self, queue_id: str) -> int | None:
        row = self.row(queue_id)
        return None if row is None else row.revision

    def queued_ids(self) -> tuple[str, ...]:
        """Waiting rows in their displayed order (reorder candidates)."""

        return tuple(row.id for row in self._rows if row.is_removable)

    def terminal_ids(self) -> tuple[str, ...]:
        return tuple(row.id for row in self._rows if row.is_terminal)

    def reordered_queued_ids(self, queue_id: str, *, offset: int) -> tuple[str, ...] | None:
        """Move one waiting row by ``offset`` positions inside the displayed order.

        Returns ``None`` when the move is not expressible (unknown row, out of
        range, or no-op); the caller must then not issue a reorder command.
        """

        ids = list(self.queued_ids())
        if queue_id not in ids:
            return None
        index = ids.index(queue_id)
        target = index + offset
        if not 0 <= target < len(ids) or target == index:
            return None
        ids.insert(target, ids.pop(index))
        return tuple(ids)
