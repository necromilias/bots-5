"""ControlDockWidget: dockable operator surface for the v0.2 control plane.

The dock presents a sealed projection via :class:`ControlViewModel` built from
the immutable :class:`~bots5.desktop.control_bridge.ControlProjection`.
Every state read comes from the bridge projection; every operation goes
through the bridge. No run files, no provider construction, no engine logic
duplication.

Presentation rules:
  - Load from a bridge factory, then Validate/Prepare → Approve
  - Show active grants, queued/running executions, and receipts
  - Preflight summary with Approve as explicit action
  - Live progress: per-operation state, duration, scope
  - Receipts: show settlement state, digest, and UNKNOWN explicitly
  - Integrity warnings: display every warning verbatim
  - Polling: QTimer at 250 ms cadence awaiting projection_async
  - Uncertainty: render unknown-outcome operations distinctly
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QDialog,
    QDialogButtonBox,
    QSizePolicy,
    QComboBox,
    QTabWidget,
)

from .control_bridge import (
    ControlBridge,
    ControlProjection,
    ExecutionState,
    POLL_INTERVAL_MS,
    POLL_MAX_INTERVAL_MS,
    PreparedControlOperation,
)

from .dialog_primitives import (
    ChamferedPanel, WorkPanel, normalize_dialog, scrollable, SectionHeader,
)
from .theme import (
    PANEL_INSET,
    ROW_GAP,
    SURFACE_PANEL,
    SURFACE_RAISED,
    STATUS_SUCCESS,
    STATUS_WARNING,
    STATUS_ERROR,
    ACCENT_STRUCTURE,
    TEXT_MUTED,
    TEXT_PRIMARY,
    BORDER_SUBTLE,
)


class ControlViewModel:
    """Immutable view model derived from a ControlProjection.

    Zero business logic: only field projection and derived presentation text.
    All state comes from the projection; nothing is fabricated.
    """

    def __init__(self, projection: ControlProjection | None = None) -> None:
        self._projection = projection
        self._prepared_operation: PreparedControlOperation | None = None

    @property
    def projection(self) -> ControlProjection | None:
        return self._projection

    @property
    def prepared_operation(self) -> PreparedControlOperation | None:
        return self._prepared_operation

    def apply_projection(self, projection: ControlProjection) -> "ControlViewModel":
        return ControlProjectionWrapper(projection, self._prepared_operation)

    def apply_prepared_operation(self, op: PreparedControlOperation) -> "ControlViewModel":
        return ControlProjectionWrapper(self._projection, op)


class ControlProjectionWrapper(ControlViewModel):
    """Wrapper that forwards projection access while adding derived fields."""

    def __init__(
        self,
        projection: ControlProjection | None,
        prepared_operation: PreparedControlOperation | None,
    ) -> None:
        super().__init__(projection)
        self._projection = projection
        self._prepared_operation = prepared_operation

    @property
    def has_data(self) -> bool:
        return self._projection is not None and (
            self._projection.executions
            or self._projection.grants
            or self._projection.receipts
        )

    @property
    def display_state(self) -> str:
        if self._projection is None:
            return "idle"
        return self._projection.display_state

    @property
    def execution_rows(self) -> list[dict[str, object]]:
        """Per-execution row data for the executions table."""
        if self._projection is None:
            return []
        rows = []
        for exec_proj in self._projection.executions:
            rows.append({
                "operation_id": exec_proj.operation_id,
                "kind": exec_proj.kind,
                "state": exec_proj.state,
                "scope": exec_proj.scope or "",
                "description": exec_proj.description,
                "submitted_at": exec_proj.submitted_at,
                "started_at": exec_proj.started_at,
                "ended_at": exec_proj.ended_at,
                "duration_seconds": exec_proj.duration_seconds,
                "exit_code": exec_proj.exit_code,
                "error_type": exec_proj.error_type,
                "error_message": exec_proj.error_message,
                "provider_side_outcome_unknown": exec_proj.provider_side_outcome_unknown,
                "grant_id": exec_proj.grant_id,
                "approval_id": exec_proj.approval_id,
                "output_path": exec_proj.output_path,
            })
        return rows

    @property
    def grant_rows(self) -> list[dict[str, object]]:
        """Per-grant row data for the grants table."""
        if self._projection is None:
            return []
        rows = []
        for grant in self._projection.grants:
            rows.append({
                "grant_id": grant.grant_id,
                "scope": grant.scope,
                "subject": grant.subject,
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
                "issued_by": grant.issued_by,
                "request_digest": grant.request_digest,
                "active": grant.active,
                "revoked_at": grant.revoked_at,
                "revoke_reason": grant.revoke_reason,
            })
        return rows

    @property
    def receipt_rows(self) -> list[dict[str, object]]:
        """Per-receipt row data for the receipts table."""
        if self._projection is None:
            return []
        rows = []
        for receipt in self._projection.receipts:
            rows.append({
                "receipt_id": receipt.receipt_id,
                "operation_id": receipt.operation_id,
                "state": receipt.state,
                "settled_at": receipt.settled_at,
                "request_digest": receipt.request_digest,
                "result_digest": receipt.result_digest,
                "grant_id": receipt.grant_id,
                "unknown_reason": receipt.unknown_reason,
            })
        return rows

    @property
    def summary_line(self) -> str:
        """Summary line: active/queued/succeeded/failed/unknown counts."""
        if self._projection is None:
            return "no data"
        parts = []
        if self._projection.active_count:
            parts.append(f"active: {self._projection.active_count}")
        if self._projection.queued_count:
            parts.append(f"queued: {self._projection.queued_count}")
        if self._projection.succeeded_count:
            parts.append(f"succeeded: {self._projection.succeeded_count}")
        if self._projection.failed_count:
            parts.append(f"failed: {self._projection.failed_count}")
        if self._projection.unknown_count:
            parts.append(f"unknown: {self._projection.unknown_count}")
        return " | ".join(parts) if parts else "idle"

    @property
    def integrity_warnings(self) -> tuple[str, ...]:
        if self._projection is None:
            return ()
        return self._projection.integrity_warnings

    @property
    def is_running(self) -> bool:
        if self._projection is None:
            return False
        return self._projection.is_running


class ControlDockWidget(QDockWidget):
    """Dockable v0.2 control-plane operator surface."""

    refresh_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        bridge_factory: Callable[[Path | None], ControlBridge] | None = None,
    ) -> None:
        super().__init__("Control Plane", parent)
        self.setObjectName("controlDock")
        self._bridge_factory = bridge_factory
        self._bridge: ControlBridge | None = None
        self._view_model = ControlViewModel()
        self._closed = False
        self._projection: ControlProjection | None = None
        self._prepared_operation: PreparedControlOperation | None = None
        # Retained reference to the in-flight projection refresh task. Holding a
        # strong reference prevents the scheduled coroutine being garbage
        # collected before it runs (RuntimeWarning: coroutine ... never awaited).
        self._refresh_task: asyncio.Task[None] | None = None

        content = QWidget(self)
        content.setObjectName("controlContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(PANEL_INSET, PANEL_INSET, PANEL_INSET, PANEL_INSET)
        layout.setSpacing(ROW_GAP)

        # Masthead
        masthead = ChamferedPanel(content, chamfer=8, header=True)
        head_layout = QVBoxLayout(masthead)
        head_layout.setContentsMargins(12, 8, 12, 8)
        head_layout.addWidget(SectionHeader(
            "Control Plane",
            "v0.2 authority grants, execution state, approvals and receipts.",
            masthead,
        ))
        layout.addWidget(masthead)

        # Operation prep panel
        prep = WorkPanel("Operation / preflight", content)
        prep_section = self._build_prep_section(content)
        prep.body_layout.addLayout(prep_section)
        layout.addWidget(prep)

        # Tabs: Executions, Grants, Receipts
        self._tabs = QTabWidget(content)
        self._tabs.setObjectName("controlTabs")

        # Executions tab
        exec_widget = QWidget()
        exec_layout = QVBoxLayout(exec_widget)
        exec_layout.setContentsMargins(0, 0, 0, 0)
        exec_layout.setSpacing(ROW_GAP)
        exec_section = self._build_executions_section(exec_widget)
        exec_layout.addLayout(exec_section)
        self._tabs.addTab(exec_widget, "Executions")

        # Grants tab
        grants_widget = QWidget()
        grants_layout = QVBoxLayout(grants_widget)
        grants_layout.setContentsMargins(0, 0, 0, 0)
        grants_layout.setSpacing(ROW_GAP)
        grants_section = self._build_grants_section(grants_widget)
        grants_layout.addLayout(grants_section)
        self._tabs.addTab(grants_widget, "Grants")

        # Receipts tab
        receipts_widget = QWidget()
        receipts_layout = QVBoxLayout(receipts_widget)
        receipts_layout.setContentsMargins(0, 0, 0, 0)
        receipts_layout.setSpacing(ROW_GAP)
        receipts_section = self._build_receipts_section(receipts_widget)
        receipts_layout.addLayout(receipts_section)
        self._tabs.addTab(receipts_widget, "Receipts")

        layout.addWidget(self._tabs, 1)

        # Summary and warnings
        summary_section = self._build_summary_section(content)
        layout.addLayout(summary_section)

        # Footer controls
        footer = QFrame(content)
        footer.setObjectName("botsDialogFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 8, 0, 0)
        controls_section = self._build_controls_section(content)
        footer_layout.addLayout(controls_section)
        layout.addWidget(footer)

        for button in self.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        self.setWidget(content)
        self.resize(900, 600)

        # Polling timer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll_timeout)

        self.visibilityChanged.connect(self._on_visibility_changed)
        self._sync_polling()

    # ------------------------------------------------------------------
    # UI construction helpers
    # ------------------------------------------------------------------

    def _build_prep_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("controlPrepSection")

        # Operation kind selector
        kind_layout = QHBoxLayout()
        kind_label = QLabel("Kind:", parent)
        kind_label.setObjectName("botsFieldLabel")
        self._kind_combo = QComboBox(parent)
        self._kind_combo.setObjectName("controlKindCombo")
        self._kind_combo.addItem("Process execution", "process")
        self._kind_combo.addItem("Git operation", "git")
        self._kind_combo.addItem("Tool invocation", "tool")
        self._kind_combo.addItem("Plugin operation", "plugin")
        kind_layout.addWidget(kind_label)
        kind_layout.addWidget(self._kind_combo)
        kind_layout.addStretch(1)
        section.addLayout(kind_layout)

        # Description input
        desc_label = QLabel("Description:", parent)
        desc_label.setObjectName("botsFieldLabel")
        section.addWidget(desc_label)
        self._description_input = QLineEdit(parent)
        self._description_input.setObjectName("controlDescriptionInput")
        self._description_input.setPlaceholderText("Brief description of the operation")
        section.addWidget(self._description_input)

        # Scope selector
        scope_layout = QHBoxLayout()
        scope_label = QLabel("Scope:", parent)
        scope_label.setObjectName("botsFieldLabel")
        self._scope_combo = QComboBox(parent)
        self._scope_combo.setObjectName("controlScopeCombo")
        self._scope_combo.addItem("Process execution", "process_execution")
        self._scope_combo.addItem("Git inspect", "git_inspect")
        self._scope_combo.addItem("Git edit", "git_edit")
        self._scope_combo.addItem("Git stage", "git_stage")
        self._scope_combo.addItem("Git commit", "git_commit")
        self._scope_combo.addItem("Git push", "git_push")
        self._scope_combo.addItem("Tool invocation", "tool_invocation")
        self._scope_combo.addItem("Network egress", "network_egress")
        self._scope_combo.addItem("Plugin load", "plugin_load")
        scope_layout.addWidget(scope_label)
        scope_layout.addWidget(self._scope_combo)
        scope_layout.addStretch(1)
        section.addLayout(scope_layout)

        # Preflight summary
        self._preflight_text = QTextEdit(parent)
        self._preflight_text.setObjectName("controlPreflightSummary")
        self._preflight_text.setReadOnly(True)
        self._preflight_text.setFixedHeight(100)
        self._preflight_text.setPlaceholderText("Prepare an operation to see the preflight summary")
        section.addWidget(QLabel("Preflight summary", parent))
        section.addWidget(self._preflight_text)

        # Prep controls
        prep_controls = QHBoxLayout()
        self._prepare_button = QPushButton("Prepare", parent)
        self._prepare_button.setObjectName("controlPrepareButton")
        self._prepare_button.setToolTip("Prepare the operation for approval (zero spend)")
        self._prepare_button.clicked.connect(self._on_prepare)
        prep_controls.addWidget(self._prepare_button)

        self._approve_button = QPushButton("Approve", parent)
        self._approve_button.setObjectName("controlApproveButton")
        self._approve_button.setToolTip("Approve and dispatch the prepared operation")
        self._approve_button.clicked.connect(self._on_approve)
        self._approve_button.setEnabled(False)
        prep_controls.addWidget(self._approve_button)

        prep_controls.addStretch(1)
        section.addLayout(prep_controls)

        return section

    def _build_executions_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("controlExecutionsSection")

        self._executions_table = QTableWidget(parent)
        self._executions_table.setObjectName("controlExecutionsTable")
        self._executions_table.setColumnCount(6)
        self._executions_table.setHorizontalHeaderLabels((
            "ID", "Kind", "State", "Scope", "Description", "Duration"
        ))
        self._executions_table.verticalHeader().setVisible(False)
        self._executions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._executions_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._executions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._executions_table.horizontalHeader()
        self._executions_table.setAlternatingRowColors(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 120)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._executions_table.itemSelectionChanged.connect(self._sync_execution_controls)
        section.addWidget(self._executions_table, 1)

        # Execution detail
        self._execution_detail = QTextEdit(parent)
        self._execution_detail.setObjectName("controlExecutionDetail")
        self._execution_detail.setReadOnly(True)
        self._execution_detail.setMaximumHeight(120)
        self._execution_detail.setPlaceholderText("Select an execution to see details")
        section.addWidget(self._execution_detail)

        # Execution controls
        exec_controls = QHBoxLayout()
        self._cancel_button = QPushButton("Cancel", parent)
        self._cancel_button.setObjectName("controlCancelButton")
        self._cancel_button.setToolTip("Cancel the selected execution")
        self._cancel_button.clicked.connect(self._on_cancel_execution)
        self._cancel_button.setEnabled(False)
        exec_controls.addWidget(self._cancel_button)

        self._refresh_exec_button = QPushButton("Refresh", parent)
        self._refresh_exec_button.setObjectName("controlRefreshExecButton")
        self._refresh_exec_button.setToolTip("Refresh execution state")
        self._refresh_exec_button.clicked.connect(self._on_refresh)
        exec_controls.addWidget(self._refresh_exec_button)

        exec_controls.addStretch(1)
        section.addLayout(exec_controls)

        return section

    def _build_grants_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("controlGrantsSection")

        self._grants_table = QTableWidget(parent)
        self._grants_table.setObjectName("controlGrantsTable")
        self._grants_table.setColumnCount(5)
        self._grants_table.setHorizontalHeaderLabels((
            "Grant ID", "Scope", "Subject", "Issued by", "Status"
        ))
        self._grants_table.verticalHeader().setVisible(False)
        self._grants_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._grants_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._grants_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._grants_table.horizontalHeader()
        self._grants_table.setAlternatingRowColors(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 120)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        section.addWidget(self._grants_table, 1)

        return section

    def _build_receipts_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("controlReceiptsSection")

        self._receipts_table = QTableWidget(parent)
        self._receipts_table.setObjectName("controlReceiptsTable")
        self._receipts_table.setColumnCount(4)
        self._receipts_table.setHorizontalHeaderLabels((
            "Receipt ID", "Operation", "State", "Settled"
        ))
        self._receipts_table.verticalHeader().setVisible(False)
        self._receipts_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._receipts_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._receipts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._receipts_table.horizontalHeader()
        self._receipts_table.setAlternatingRowColors(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 120)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 120)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._receipts_table.itemSelectionChanged.connect(self._sync_receipt_detail)
        section.addWidget(self._receipts_table, 1)

        # Receipt detail
        self._receipt_detail = QTextEdit(parent)
        self._receipt_detail.setObjectName("controlReceiptDetail")
        self._receipt_detail.setReadOnly(True)
        self._receipt_detail.setMaximumHeight(100)
        self._receipt_detail.setPlaceholderText("Select a receipt to see details")
        section.addWidget(self._receipt_detail)

        return section

    def _build_summary_section(self, parent: QWidget) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setObjectName("controlSummarySection")

        self._summary_label = QLabel("idle", parent)
        self._summary_label.setObjectName("controlSummaryLabel")
        self._summary_label.setWordWrap(True)
        section.addWidget(self._summary_label)

        self._warnings_label = QLabel("", parent)
        self._warnings_label.setObjectName("controlWarningsLabel")
        self._warnings_label.setWordWrap(True)
        self._warnings_label.setStyleSheet(f"color: {STATUS_WARNING};")
        section.addWidget(self._warnings_label)

        return section

    def _build_controls_section(self, parent: QWidget) -> QHBoxLayout:
        section = QHBoxLayout()
        section.addStretch(1)
        section.setObjectName("controlControlsSection")
        # Cancel is in executions tab; this is for global controls
        return section

    # ------------------------------------------------------------------
    # Operation preparation and approval
    # ------------------------------------------------------------------

    def set_bridge(self, bridge: ControlBridge) -> None:
        """Set the control bridge for this dock."""
        if self._bridge is not None:
            try:
                if asyncio.get_running_loop().is_running():
                    asyncio.create_task(self._bridge.close_async())
                else:
                    self._bridge.close()
            except RuntimeError:
                # No running event loop - synchronous close
                self._bridge.close()
            except Exception:
                pass
        self._bridge = bridge
        self._view_model = ControlViewModel()
        # Initial refresh: only schedule if we're inside a running event loop.
        # Otherwise, the first visibility change or poll will trigger it.
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self._refresh_projection_async())
        except RuntimeError:
            pass

    def _on_prepare(self) -> None:
        """Prepare the configured operation for approval (zero spend)."""
        if self._bridge is None:
            self._show_status("Bridge not available")
            return

        kind = self._kind_combo.currentData()
        scope = self._scope_combo.currentData()
        description = self._description_input.text().strip()
        if not description:
            self._show_status("Description is required")
            return

        try:
            prepared = self._bridge.prepare_execution(
                kind=kind,
                scope=scope,
                description=description,
                approved_by="desktop-user",
            )
            self._prepared_operation = prepared
            self._view_model = self._view_model.apply_prepared_operation(prepared)
            self._preflight_text.setPlainText(prepared.summary)
            self._approve_button.setEnabled(True)
            self._show_status("Operation prepared — Approve to dispatch")
        except Exception as exc:
            self._approve_button.setEnabled(False)
            self._show_status(f"Prepare failed: {exc}")

    def _on_approve(self) -> None:
        """Approve and dispatch the prepared operation."""
        if self._bridge is None or self._prepared_operation is None:
            self._show_status("No prepared operation")
            return

        try:
            self._bridge.approve_execution(self._prepared_operation)
            self._show_status("Operation dispatched")
            self._approve_button.setEnabled(False)
            self._prepared_operation = None
        except Exception as exc:
            self._show_status(f"Approval failed: {exc}")
        else:
            # Refresh only after the approval has been committed. Scheduling is
            # guarded so the coroutine is never created without a running loop,
            # and the task reference is retained so it cannot be collected
            # before it runs.
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Schedule a projection refresh if a loop is running.

        Fire-and-forget tasks are only safe when the event loop will run them;
        outside a running loop the coroutine is never created at all, so no
        un-awaited coroutine can leak. The task is retained so it cannot be
        garbage-collected before executing.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.create_task(self._refresh_projection_async())
        self._refresh_task = task
        task.add_done_callback(self._on_refresh_done)

    def _on_refresh_done(self, task: asyncio.Task[None]) -> None:
        """Release the retained refresh task and surface failures."""
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._show_status(f"Projection refresh failed: {error}")

    # ------------------------------------------------------------------
    # Execution controls
    # ------------------------------------------------------------------

    def _on_cancel_execution(self) -> None:
        """Cancel the selected execution."""
        if self._bridge is None:
            return
        selected = self._selected_execution()
        if selected is None:
            return

        operation_id = selected["operation_id"]

        # Prepare cancellation (zero-spend)
        try:
            prepared = self._bridge.prepare_cancellation(operation_id, "desktop-user")
        except Exception as exc:
            self._show_status(f"Cancel prepare failed: {exc}")
            return

        # Show confirmation dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Cancel Operation")
        dialog.setObjectName("controlCancelDialog")
        dialog_layout = QVBoxLayout(dialog)

        msg = QLabel(f"Cancel operation {operation_id}?\n\n"
                     f"Outcome will remain UNKNOWN until settlement confirms the cancel.\n"
                     f"This requires explicit approval.", dialog)
        msg.setWordWrap(True)
        dialog_layout.addWidget(msg)

        summary = QTextEdit(dialog)
        summary.setReadOnly(True)
        summary.setPlainText(prepared.summary)
        summary.setMaximumHeight(100)
        dialog_layout.addWidget(summary)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        dialog_actions = QHBoxLayout()
        dialog_actions.addStretch(1)
        dialog_actions.addWidget(button_box)
        dialog_layout.addLayout(dialog_actions)
        button_box.button(QDialogButtonBox.StandardButton.Ok).setProperty("role", "primary")
        normalize_dialog(dialog, size=(520, 340),
                         description=f"Approve cancellation of {operation_id}.")

        if dialog.exec() != 1:  # QDialog.Accepted
            return

        try:
            self._bridge.approve_cancellation(prepared)
            self._show_status(f"Cancel dispatched for {operation_id}")
            asyncio.create_task(self._refresh_projection_async())
        except Exception as exc:
            self._show_status(f"Cancel approval failed: {exc}")

    def _on_refresh(self) -> None:
        """Manually trigger a projection refresh."""
        asyncio.create_task(self._refresh_projection_async())

    def _selected_execution(self) -> dict[str, object] | None:
        """Get the currently selected execution row data."""
        indexes = self._executions_table.selectionModel().selectedRows() if self._executions_table.selectionModel() else []
        if not indexes:
            return None
        row = indexes[0].row()
        if row < 0 or row >= self._executions_table.rowCount():
            return None

        return {
            "operation_id": self._executions_table.item(row, 0).text(),
            "kind": self._executions_table.item(row, 1).text(),
            "state": self._executions_table.item(row, 2).text(),
            "scope": self._executions_table.item(row, 3).text(),
            "description": self._executions_table.item(row, 4).text(),
            "duration": self._executions_table.item(row, 5).text(),
        }

    def _sync_execution_controls(self) -> None:
        """Sync execution controls based on selection."""
        selected = self._selected_execution()
        if selected is None:
            self._cancel_button.setEnabled(False)
            self._execution_detail.setPlainText("")
            return

        # Cancel only for non-terminal states
        state = selected["state"]
        terminal = state in (
            ExecutionState.SUCCEEDED.value,
            ExecutionState.FAILED.value,
            ExecutionState.CANCELLED.value,
            ExecutionState.TIMED_OUT.value,
            ExecutionState.UNKNOWN.value,
        )
        self._cancel_button.setEnabled(not terminal)

        # Show details
        rows = self._view_model.execution_rows
        for r in rows:
            if r["operation_id"] == selected["operation_id"]:
                detail_lines = [
                    f"Operation: {r['operation_id']}",
                    f"Kind: {r['kind']}",
                    f"State: {r['state']}",
                    f"Scope: {r['scope']}",
                    f"Description: {r['description']}",
                ]
                if r["error_message"]:
                    detail_lines.append(f"Error: {r['error_message']}")
                if r.get("provider_side_outcome_unknown"):
                    detail_lines.append("⚠ Outcome: UNKNOWN (provider-side outcome not confirmed)")
                if r["approval_id"]:
                    detail_lines.append(f"Approval: {r['approval_id']}")
                if r["grant_id"]:
                    detail_lines.append(f"Grant: {r['grant_id']}")
                self._execution_detail.setPlainText("\n".join(detail_lines))
                break

    def _sync_receipt_detail(self) -> None:
        """Sync receipt detail based on selection."""
        indexes = self._receipts_table.selectionModel().selectedRows() if self._receipts_table.selectionModel() else []
        if not indexes:
            self._receipt_detail.setPlainText("")
            return
        row = indexes[0].row()
        receipt_id = self._receipts_table.item(row, 0).text()

        rows = self._view_model.receipt_rows
        for r in rows:
            if r["receipt_id"] == receipt_id:
                detail_lines = [
                    f"Receipt: {r['receipt_id']}",
                    f"Operation: {r['operation_id']}",
                    f"State: {r['state']}",
                ]
                if r["request_digest"]:
                    detail_lines.append(f"Request digest: {r['request_digest']}")
                if r["result_digest"]:
                    detail_lines.append(f"Result digest: {r['result_digest']}")
                if r["unknown_reason"]:
                    detail_lines.append(f"⚠ UNKNOWN reason: {r['unknown_reason']}")
                self._receipt_detail.setPlainText("\n".join(detail_lines))
                break

    # ------------------------------------------------------------------
    # Polling and projection
    # ------------------------------------------------------------------

    def _on_poll_timeout(self) -> None:
        """Poll timeout handler: await projection_async."""
        asyncio.create_task(self._refresh_projection_async())

    async def _refresh_projection_async(self) -> None:
        """Refresh the projection from the bridge."""
        if self._closed or self._bridge is None:
            return

        try:
            projection = await self._bridge.projection_async()
            self._projection = projection
            self._view_model = self._view_model.apply_projection(projection)
            self._render_projection(projection)
        except Exception as exc:
            self._show_status(f"Projection refresh failed: {exc}")

    def _render_projection(self, projection: ControlProjection) -> None:
        """Render the projection into the UI."""
        # Summary line
        self._summary_label.setText(self._view_model.summary_line)

        # Warnings
        warnings = self._view_model.integrity_warnings
        if warnings:
            self._warnings_label.setText("Integrity warnings: " + "; ".join(warnings))
        else:
            self._warnings_label.setText("")

        # Executions table
        rows = self._view_model.execution_rows
        self._executions_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self._executions_table.setItem(i, 0, QTableWidgetItem(str(row["operation_id"])))
            self._executions_table.setItem(i, 1, QTableWidgetItem(str(row["kind"])))

            state_item = QTableWidgetItem(str(row["state"]))
            state_color = self._state_color(
                str(row["state"]),
                bool(row.get("provider_side_outcome_unknown", False)),
            )
            state_item.setBackground(state_color)
            self._executions_table.setItem(i, 2, state_item)

            self._executions_table.setItem(i, 3, QTableWidgetItem(str(row["scope"] or "")))
            self._executions_table.setItem(i, 4, QTableWidgetItem(str(row["description"])))

            duration = row.get("duration_seconds")
            duration_str = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "—"
            self._executions_table.setItem(i, 5, QTableWidgetItem(duration_str))

        # Grants table
        grant_rows = self._view_model.grant_rows
        self._grants_table.setRowCount(len(grant_rows))
        for i, row in enumerate(grant_rows):
            self._grants_table.setItem(i, 0, QTableWidgetItem(str(row["grant_id"])))
            self._grants_table.setItem(i, 1, QTableWidgetItem(str(row["scope"])))
            self._grants_table.setItem(i, 2, QTableWidgetItem(str(row["subject"])))
            self._grants_table.setItem(i, 3, QTableWidgetItem(str(row["issued_by"])))

            status = "active" if row.get("active") else "revoked"
            status_item = QTableWidgetItem(status)
            if row.get("revoked_at"):
                status_item.setForeground(QColor(STATUS_WARNING))
            self._grants_table.setItem(i, 4, status_item)

        # Receipts table
        receipt_rows = self._view_model.receipt_rows
        self._receipts_table.setRowCount(len(receipt_rows))
        for i, row in enumerate(receipt_rows):
            self._receipts_table.setItem(i, 0, QTableWidgetItem(str(row["receipt_id"])))
            self._receipts_table.setItem(i, 1, QTableWidgetItem(str(row["operation_id"])))

            state_item = QTableWidgetItem(str(row["state"]))
            if row["state"] == "unknown":
                state_item.setForeground(QColor(STATUS_WARNING))
            elif row["state"] == "rejected":
                state_item.setForeground(QColor(STATUS_ERROR))
            elif row["state"] == "settled":
                state_item.setForeground(QColor(STATUS_SUCCESS))
            self._receipts_table.setItem(i, 2, state_item)

            settled = row.get("settled_at")
            settled_str = str(settled) if settled else "—"
            self._receipts_table.setItem(i, 3, QTableWidgetItem(settled_str))

        # Sync controls
        self._sync_execution_controls()
        self._sync_polling()

    def _state_color(self, state: str, provider_unknown: bool) -> QColor:
        """Retain state tones with readable light text on graphite."""
        base = QColor(SURFACE_PANEL)
        tone = {
            "succeeded": STATUS_SUCCESS,
            "failed": STATUS_WARNING if provider_unknown else STATUS_ERROR,
            "running": ACCENT_STRUCTURE,
            "queued": SURFACE_RAISED,
            "pending": SURFACE_RAISED,
            "approved": ACCENT_STRUCTURE,
            "cancelled": SURFACE_RAISED,
            "timed_out": STATUS_WARNING,
            "unknown": STATUS_WARNING,
        }.get(state, SURFACE_RAISED)
        accent = QColor(tone)
        return QColor(
            round(base.red() * .8 + accent.red() * .2),
            round(base.green() * .8 + accent.green() * .2),
            round(base.blue() * .8 + accent.blue() * .2),
        )

    def _on_visibility_changed(self, visible: bool) -> None:
        """Handle visibility changes for polling control."""
        if visible:
            try:
                asyncio.get_running_loop()
                asyncio.create_task(self._refresh_projection_async())
            except RuntimeError:
                # No running event loop (e.g., test environment) — skip
                # the async refresh; the caller can trigger it manually.
                pass
        self._sync_polling()

    def _sync_polling(self) -> None:
        """Sync polling timer based on visibility and run state."""
        should_poll = (
            not self._closed
            and self.isVisible()
            and self._projection is not None
            and self._projection.is_running
        )
        if should_poll and not self._poll_timer.isActive():
            self._poll_timer.start()
        elif not should_poll and self._poll_timer.isActive():
            self._poll_timer.stop()

    # ------------------------------------------------------------------
    # Status and lifecycle
    # ------------------------------------------------------------------

    def _show_status(self, message: str) -> None:
        """Show a status message in the summary area."""
        self._summary_label.setText(message)

    def mark_closed(self) -> None:
        """Stop polling and mark closed."""
        if self._closed:
            return
        self._closed = True
        self._poll_timer.stop()
        if self._bridge is not None:
            try:
                self._bridge.close()
            except Exception:
                pass

    async def drain(self) -> None:
        """Await bridge.close_async() if a bridge exists."""
        if self._bridge is not None:
            await self._bridge.close_async()
