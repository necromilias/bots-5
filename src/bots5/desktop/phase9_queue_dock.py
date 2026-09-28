"""ImportQueueDockWidget: dockable operator surface for the persistent import queue.

Rows render only from the sealed display projection via
:class:`~bots5.desktop.phase9_imports.ImportQueueViewModel` — no SQL, no
store reach-around (SLICE_E_DESIGN.md I1).  The row ``revision`` column is a
read-only CAS token echoed to cancel/remove; it is never editable domain state.

Refresh is the sealed pair (master §4.4, D-3):

* an **immediate reload** when the dock becomes visible and after every locally
  issued queue command (enqueue, cancel, remove, reorder, retry, clear-history);
* a **bounded poll** with a maximum interval of one second that runs only while
  the dock is visible **and** at least one row is non-terminal, so durable
  mid-preflight transitions (QUEUED → PREFLIGHTING → …) become visible within a
  stated bound.  Polling stops when the dock is hidden or all rows are terminal.

There is no auto-retry: a stale row revision or queue revision surfaces a
truthful "queue changed — refresh" prompt.  Reorder refusals carry the landed
store messages verbatim, and the reorder control is disabled while any row is
active rather than relying on the refusal alone (oracle R-6).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bots5.core.import_queue import ImportQueueState, QueueDisplayPage

from .phase9_imports import ImportQueueViewModel, QueueRowView


# Bounded refresh: the timer interval must never exceed POLL_MAX_INTERVAL_MS.
POLL_INTERVAL_MS = 250
POLL_MAX_INTERVAL_MS = 1000

_REFRESH_PROMPT = "Queue changed — refresh."


class ImportQueueDockWidget(QDockWidget):
    """Dockable queue rows plus the exact landed control set (workflow 4)."""

    refresh_requested = Signal()  # fallback route when no controller is attached
    cancel_requested = Signal(str, int)  # (queue_id, observed row revision)
    remove_requested = Signal(str, int)  # (queue_id, observed row revision)
    retry_requested = Signal(str)
    reorder_requested = Signal(int, tuple)  # (observed queue revision, ordered ids)
    clear_history_requested = Signal(tuple)

    _COLUMNS = ("Source", "State", "Revision", "Result", "Enqueued", "Started", "Finished")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Import Queue", parent)
        self.setObjectName("importQueueDock")
        self._controller = None
        self._view_model = ImportQueueViewModel()
        self._closed = False

        content = QWidget(self)
        content.setObjectName("importQueueContent")
        layout = QVBoxLayout(content)

        self.table = QTableWidget(content)
        self.table.setObjectName("importQueueTable")
        self.table.setColumnCount(len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._sync_controls)
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", content)
        self.refresh_button.setObjectName("importQueueRefreshButton")
        self.refresh_button.setToolTip("Reload the queue from the durable store now")
        self.refresh_button.clicked.connect(self.reload_now)
        controls.addWidget(self.refresh_button)
        self.cancel_button = QPushButton("Cancel", content)
        self.cancel_button.setObjectName("importQueueCancelButton")
        self.cancel_button.setToolTip(
            "Cancel the selected queued or preflighting import (pre-cutoff only)"
        )
        self.cancel_button.clicked.connect(self._on_cancel)
        controls.addWidget(self.cancel_button)
        self.remove_button = QPushButton("Remove waiting", content)
        self.remove_button.setObjectName("importQueueRemoveButton")
        self.remove_button.setToolTip("Remove the selected still-waiting import")
        self.remove_button.clicked.connect(self._on_remove)
        controls.addWidget(self.remove_button)
        self.retry_button = QPushButton("Retry", content)
        self.retry_button.setObjectName("importQueueRetryButton")
        self.retry_button.setToolTip("Explicitly retry a failed import as a fresh row")
        self.retry_button.clicked.connect(self._on_retry)
        controls.addWidget(self.retry_button)
        self.move_up_button = QPushButton("Move up", content)
        self.move_up_button.setObjectName("importQueueMoveUpButton")
        self.move_up_button.setToolTip("Move the selected waiting import earlier")
        self.move_up_button.clicked.connect(lambda: self._on_move(-1))
        controls.addWidget(self.move_up_button)
        self.move_down_button = QPushButton("Move down", content)
        self.move_down_button.setObjectName("importQueueMoveDownButton")
        self.move_down_button.setToolTip("Move the selected waiting import later")
        self.move_down_button.clicked.connect(lambda: self._on_move(1))
        controls.addWidget(self.move_down_button)
        self.clear_button = QPushButton("Clear history", content)
        self.clear_button.setObjectName("importQueueClearHistoryButton")
        self.clear_button.setToolTip("Remove all terminal rows from the queue history")
        self.clear_button.clicked.connect(self._on_clear_history)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.status_label = QLabel(
            "Queue rows follow the durable import store.", content
        )
        self.status_label.setObjectName("importQueueStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.setWidget(content)

        self._poll_timer = self._build_poll_timer()
        self.visibilityChanged.connect(self._on_visibility_changed)
        self._sync_controls()
        self._sync_polling()

    def _build_poll_timer(self):
        from PySide6.QtCore import QTimer

        timer = QTimer(self)
        timer.setInterval(POLL_INTERVAL_MS)
        timer.timeout.connect(self.reload_now)
        return timer

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    @property
    def view_model(self) -> ImportQueueViewModel:
        return self._view_model

    @property
    def poll_timer(self):
        return self._poll_timer

    def set_controller(self, controller) -> None:
        self._controller = controller

    def mark_closed(self) -> None:
        """Stop every timer/route when the owning window tears down."""

        if self._closed:
            return
        self._closed = True
        self._poll_timer.stop()

    # ------------------------------------------------------------------
    # Refresh (immediate reload + bounded poll)
    # ------------------------------------------------------------------

    def reload_now(self) -> None:
        """Immediate reload (D-3): on show and after locally issued commands."""

        if self._closed:
            return
        if self._controller is not None:
            self._controller.schedule_refresh()
        else:
            self.refresh_requested.emit()

    def _on_visibility_changed(self, visible: bool) -> None:
        if visible:
            self.reload_now()
        self._sync_polling()

    def _sync_polling(self) -> None:
        should_poll = (
            not self._closed
            and self.isVisible()
            and self._view_model.has_active
        )
        if should_poll and not self._poll_timer.isActive():
            self._poll_timer.start()
        elif not should_poll and self._poll_timer.isActive():
            self._poll_timer.stop()

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------

    def apply_page(self, page: QueueDisplayPage) -> None:
        if self._closed:
            return
        self._view_model.apply_page(page)
        rows = self._view_model.rows
        self.table.setRowCount(len(rows))
        for position, row in enumerate(rows):
            values = (
                row.source_label,
                row.state.value,
                str(row.revision),
                self._result_text(row),
                row.enqueued_at or "—",
                row.started_at or "—",
                row.finished_at or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.id)
                    item.setToolTip(
                        "Operator label only; the intake path is never shown by the queue UI"
                    )
                self.table.setItem(position, column, item)
        self._sync_controls()
        self._sync_polling()

    @staticmethod
    def _result_text(row: QueueRowView) -> str:
        if row.state is ImportQueueState.FAILED:
            return f"{row.failure_label} ({row.failure_code})"
        return ""

    def show_notice(self, message: str) -> None:
        """A truthful typed outcome, carried verbatim to the operator."""

        self.status_label.setText(message)

    def show_refresh_prompt(self, detail: str = "") -> None:
        """Stale row revision / queue revision: prompt, never a silent retry."""

        message = _REFRESH_PROMPT
        if detail:
            message += f" ({detail})"
        self.status_label.setText(message)

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def _selected_row(self) -> QueueRowView | None:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() is not None else []
        if not indexes:
            return None
        item = self.table.item(indexes[0].row(), 0)
        if item is None:
            return None
        queue_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(queue_id, str):
            return None
        return self._view_model.row(queue_id)

    def _sync_controls(self) -> None:
        row = self._selected_row()
        self.cancel_button.setEnabled(row is not None and row.is_cancellable)
        self.remove_button.setEnabled(row is not None and row.is_removable)
        self.retry_button.setEnabled(row is not None and row.is_retryable)
        self.clear_button.setEnabled(bool(self._view_model.terminal_ids()))
        # The landed store refuses reorder while an import is active
        # ("queue reorder is unavailable while an import is active"); the
        # control is disabled rather than relying on the refusal alone (R-6).
        # QUEUED rows are not active imports; a worker claim (PREFLIGHTING
        # through settlement) is.
        reorder_available = (
            self._view_model.has_queued and not self._view_model.has_running_import
        )
        movable = (
            row is not None
            and row.is_removable
            and reorder_available
        )
        self.move_up_button.setEnabled(
            movable
            and self._view_model.reordered_queued_ids(row.id, offset=-1) is not None
        )
        self.move_down_button.setEnabled(
            movable
            and self._view_model.reordered_queued_ids(row.id, offset=1) is not None
        )

    def _on_cancel(self) -> None:
        row = self._selected_row()
        if row is None or not row.is_cancellable:
            return
        # Echo the last-observed row CAS token as the command precondition.
        self.cancel_requested.emit(row.id, row.revision)

    def _on_remove(self) -> None:
        row = self._selected_row()
        if row is None or not row.is_removable:
            return
        self.remove_requested.emit(row.id, row.revision)

    def _on_retry(self) -> None:
        row = self._selected_row()
        if row is None or not row.is_retryable:
            return
        self.retry_requested.emit(row.id)

    def _on_move(self, offset: int) -> None:
        row = self._selected_row()
        queue_revision = self._view_model.queue_revision
        if row is None or queue_revision is None:
            return
        ordering = self._view_model.reordered_queued_ids(row.id, offset=offset)
        if ordering is None:
            return
        self.reorder_requested.emit(queue_revision, ordering)

    def _on_clear_history(self) -> None:
        ids = self._view_model.terminal_ids()
        if ids:
            self.clear_history_requested.emit(ids)
