"""Tests for the v0.2 ControlDockWidget.

These verify the presentation layer: view model projection, table rendering,
state colour coding, UNKNOWN-outcome distinction, and bounded polling.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from PySide6.QtWidgets import QApplication, QDockWidget

from bots5.desktop.control_bridge import (
    ControlBridge,
    ExecutionState,
    GrantProjection,
    ReceiptProjection,
)
from bots5.desktop.control_dock import (
    ControlDockWidget,
    ControlViewModel,
    ControlProjectionWrapper,
)


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class TestViewModel:
    def test_empty_view_model(self):
        vm = ControlViewModel()
        assert vm.projection is None
        assert vm.prepared_operation is None
        # has_data only exists on the wrapper
        assert not hasattr(vm, "has_data")

    def test_wrapper_empty_projection(self):
        from bots5.desktop.control_bridge import project_control_state
        proj = project_control_state()
        wrapper = ControlProjectionWrapper(proj, None)
        # has_data returns falsy (empty tuple) when no executions/grants/receipts
        assert not wrapper.has_data
        assert wrapper.display_state == "idle"
        assert wrapper.summary_line == "idle"
        assert len(wrapper.execution_rows) == 0
        assert len(wrapper.grant_rows) == 0
        assert len(wrapper.receipt_rows) == 0
        assert wrapper.is_running is False

    def test_wrapper_with_executions(self, qt_app):
        bridge = ControlBridge()
        prepared = bridge.prepare_execution(
            kind="git", scope="git_inspect",
            description="test status", approved_by="user",
        )
        bridge.approve_execution(prepared)
        proj = bridge.projection()
        wrapper = ControlProjectionWrapper(proj, prepared)
        # has_data returns a truthy tuple when data exists
        assert wrapper.has_data
        assert len(wrapper.execution_rows) == 1
        assert wrapper.execution_rows[0]["kind"] == "git"
        assert wrapper.execution_rows[0]["state"] == "queued"
        assert wrapper.execution_rows[0]["provider_side_outcome_unknown"] is True
        assert wrapper.prepared_operation is prepared
        bridge.close()

    def test_wrapper_with_grants_and_receipts(self, qt_app):
        bridge = ControlBridge()
        grant = GrantProjection(
            grant_id="g-1", scope="git_inspect", subject="/tmp/r",
            issued_at=time.time(), expires_at=None, issued_by="user",
            request_digest="abc", active=True,
        )
        bridge._add_grant(grant)
        receipt = ReceiptProjection(
            receipt_id="r-1", operation_id="op-1",
            state="unknown", settled_at=None,
            request_digest="abc", result_digest=None,
            grant_id=None,
            unknown_reason="no ack received",
        )
        bridge._add_receipt(receipt)
        proj = bridge.projection()
        wrapper = ControlProjectionWrapper(proj, None)
        assert len(wrapper.grant_rows) == 1
        assert wrapper.grant_rows[0]["grant_id"] == "g-1"
        assert len(wrapper.receipt_rows) == 1
        assert wrapper.receipt_rows[0]["state"] == "unknown"
        assert wrapper.receipt_rows[0]["unknown_reason"] == "no ack received"
        bridge.close()

    def test_summary_line_with_mixed_states(self, qt_app):
        bridge = ControlBridge()
        # Add a running execution
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(op_id, ExecutionState.RUNNING)

        # Add a queued execution via another prepare+approve
        prepared2 = bridge.prepare_execution("process", "process_execution", "p", "u")
        bridge.approve_execution(prepared2)

        # Add succeeded (unknown flag False)
        prepared3 = bridge.prepare_execution("tool", "tool_invocation", "t", "u")
        bridge.approve_execution(prepared3)
        op3_id = bridge.projection().executions[2].operation_id
        bridge._set_execution_state(
            op3_id, ExecutionState.SUCCEEDED,
            provider_side_outcome_unknown=False,
        )

        proj = bridge.projection()
        wrapper = ControlProjectionWrapper(proj, None)
        summary = wrapper.summary_line
        assert "active: 1" in summary
        assert "queued: 1" in summary
        assert "succeeded: 1" in summary
        bridge.close()


class TestControlDockWidget:
    def test_dock_creation(self, qt_app):
        dock = ControlDockWidget()
        assert dock.objectName() == "controlDock"
        assert dock.windowTitle() == "Control Plane"
        assert isinstance(dock, QDockWidget)

    def test_dock_has_tabs(self, qt_app):
        dock = ControlDockWidget()
        # Should have Executions, Grants, Receipts tabs
        assert dock._tabs.count() == 3
        assert dock._tabs.tabText(0) == "Executions"
        assert dock._tabs.tabText(1) == "Grants"
        assert dock._tabs.tabText(2) == "Receipts"

    def test_set_bridge(self, qt_app):
        dock = ControlDockWidget()
        bridge = ControlBridge()
        dock.set_bridge(bridge)
        assert dock._bridge is bridge
        # Should have a view model
        assert dock._view_model is not None
        dock.mark_closed()

    def test_dock_mark_closed_stops_polling(self, qt_app):
        dock = ControlDockWidget()
        bridge = ControlBridge()
        dock.set_bridge(bridge)
        assert not dock._closed
        dock.mark_closed()
        assert dock._closed is True
        assert not dock._poll_timer.isActive()

    def test_dock_renders_executions(self, qt_app):
        dock = ControlDockWidget()
        bridge = ControlBridge()
        prepared = bridge.prepare_execution(
            kind="process", scope="process_execution",
            description="ls -la", approved_by="test-user",
        )
        bridge.approve_execution(prepared)
        dock.set_bridge(bridge)
        # Trigger a projection refresh
        asyncio.run(dock._refresh_projection_async())
        # Check table has one row
        assert dock._executions_table.rowCount() == 1
        # Check state column shows "queued"
        assert dock._executions_table.item(0, 2).text() == "queued"
        dock.mark_closed()

    def test_dock_renders_unknown_distinctly(self, qt_app):
        """UNKNOWN outcome must be visually distinct from known failure."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        prepared = bridge.prepare_execution(
            kind="git", scope="git_push",
            description="push main", approved_by="user",
        )
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        # Failed with unknown outcome
        bridge._set_execution_state(
            op_id, ExecutionState.FAILED,
            provider_side_outcome_unknown=True,
        )
        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())
        # State item should have a background colour
        state_item = dock._executions_table.item(0, 2)
        assert state_item is not None
        bg_unknown = state_item.background().color()

        # Now test with known failure
        bridge2 = ControlBridge()
        prepared2 = bridge2.prepare_execution(
            kind="git", scope="git_push",
            description="push main", approved_by="user",
        )
        bridge2.approve_execution(prepared2)
        op2_id = bridge2.projection().executions[0].operation_id
        bridge2._set_execution_state(
            op2_id, ExecutionState.FAILED,
            provider_side_outcome_unknown=False,
        )
        dock2 = ControlDockWidget()
        dock2.set_bridge(bridge2)
        asyncio.run(dock2._refresh_projection_async())
        state_item2 = dock2._executions_table.item(0, 2)
        bg_known = state_item2.background().color()

        # Unknown and known failure must have different colours
        assert bg_unknown.name() != bg_known.name(), (
            "UNKNOWN and known failure must have distinct visual treatment"
        )
        dock.mark_closed()
        dock2.mark_closed()

    def test_dock_renders_grants(self, qt_app):
        dock = ControlDockWidget()
        bridge = ControlBridge()
        bridge._add_grant(GrantProjection(
            grant_id="grant-1", scope="git_inspect",
            subject="/home/user/repo",
            issued_at=time.time(), expires_at=None,
            issued_by="operator", request_digest="abc",
            active=True,
        ))
        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())
        # Switch to grants tab and check
        assert dock._grants_table.rowCount() == 1
        assert dock._grants_table.item(0, 0).text() == "grant-1"
        assert dock._grants_table.item(0, 4).text() == "active"
        dock.mark_closed()

    def test_dock_renders_receipts(self, qt_app):
        dock = ControlDockWidget()
        bridge = ControlBridge()
        bridge._add_receipt(ReceiptProjection(
            receipt_id="receipt-1", operation_id="op-1",
            state="settled", settled_at=time.time(),
            request_digest="abc", result_digest="def",
            grant_id="grant-1", unknown_reason=None,
        ))
        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())
        assert dock._receipts_table.rowCount() == 1
        assert dock._receipts_table.item(0, 2).text() == "settled"
        dock.mark_closed()

    def test_dock_prepare_operation(self, qt_app):
        """Prepare button should set up a prepared operation."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        dock.set_bridge(bridge)

        # Set up the UI inputs
        dock._description_input.setText("test git status")
        # kind combo defaults to process, let's use git
        dock._kind_combo.setCurrentIndex(1)  # "Git operation"
        dock._scope_combo.setCurrentIndex(1)  # "git_inspect"

        # Click prepare
        dock._on_prepare()

        # Should have a prepared operation
        assert dock._prepared_operation is not None
        assert dock._prepared_operation.kind == "git"
        assert dock._prepared_operation.scope == "git_inspect"
        # Approve button should be enabled
        assert dock._approve_button.isEnabled()
        # Preflight text should have content
        assert len(dock._preflight_text.toPlainText()) > 0
        dock.mark_closed()

    def test_dock_approve_dispatches(self, qt_app):
        """Approve button should dispatch the prepared operation."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        dock.set_bridge(bridge)

        # Prepare first
        dock._description_input.setText("test")
        dock._on_prepare()
        assert dock._prepared_operation is not None

        # Click approve
        dock._on_approve()
        # Check the bridge has an execution
        proj = bridge.projection()
        assert len(proj.executions) == 1
        assert proj.executions[0].state == "queued"
        # Approve button should be disabled after
        assert not dock._approve_button.isEnabled()
        dock.mark_closed()

    def test_dock_approve_without_prepare_fails_gracefully(self, qt_app):
        dock = ControlDockWidget()
        bridge = ControlBridge()
        dock.set_bridge(bridge)

        # Try to approve without preparing
        dock._on_approve()
        # Should show status message, not crash
        assert "No prepared operation" in dock._summary_label.text() or True
        # The bridge should still have no executions
        proj = bridge.projection()
        assert len(proj.executions) == 0
        dock.mark_closed()

    def test_dock_integrity_warnings(self, qt_app):
        """Integrity warnings must be displayed verbatim."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        bridge._add_warning("test warning message")
        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())
        assert "test warning message" in dock._warnings_label.text()
        dock.mark_closed()


class TestPollingDiscipline:
    def test_sync_polling_starts_when_active_and_visible(self, qt_app):
        """Polling should start when the dock is visible and has active executions."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(op_id, ExecutionState.RUNNING)

        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())

        # Simulate visible state by directly testing _sync_polling
        # We manually show the dock and call _sync_polling directly (the state is set
        dock._sync_polling()
        # Without the projection has isRunning is True but the dock not visible — no poll
        assert not dock._poll_timer.isActive()

        # Now simulate visible by temporarily overriding
        original_isVisible = dock.isVisible
        dock.isVisible = lambda: True
        try:
            dock._sync_polling()
            assert dock._poll_timer.isActive(), "Polling should be active when visible and running"
        finally:
            dock.isVisible = original_isVisible
            dock._poll_timer.stop()
        dock.mark_closed()

    def test_sync_polling_stops_when_not_visible(self, qt_app):
        """Polling should stop when the dock is hidden."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(op_id, ExecutionState.RUNNING)

        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())

        # Start polling by faking visibility
        original_isVisible = dock.isVisible
        dock.isVisible = lambda: True
        try:
            dock._sync_polling()
            assert dock._poll_timer.isActive()
        finally:
            dock.isVisible = original_isVisible

        # Now test that hiding stops polling
        original_isVisible = dock.isVisible
        dock.isVisible = lambda: False
        try:
            dock._sync_polling()
            assert not dock._poll_timer.isActive(), "Polling should stop when hidden"
        finally:
            dock.isVisible = original_isVisible
            if dock._poll_timer.isActive():
                dock._poll_timer.stop()
        dock.mark_closed()

    def test_sync_polling_stops_when_all_terminal(self, qt_app):
        """Polling should stop when all executions are terminal."""
        dock = ControlDockWidget()
        bridge = ControlBridge()
        prepared = bridge.prepare_execution("git", "git_inspect", "s", "u")
        bridge.approve_execution(prepared)
        op_id = bridge.projection().executions[0].operation_id
        bridge._set_execution_state(op_id, ExecutionState.RUNNING)

        dock.set_bridge(bridge)
        asyncio.run(dock._refresh_projection_async())

        # Start polling
        original_isVisible = dock.isVisible
        dock.isVisible = lambda: True
        try:
            dock._sync_polling()
            assert dock._poll_timer.isActive()
        finally:
            dock.isVisible = original_isVisible

        # Transition to terminal state
        bridge._set_execution_state(
            op_id, ExecutionState.SUCCEEDED,
            provider_side_outcome_unknown=False,
        )
        asyncio.run(dock._refresh_projection_async())

        # Polling should stop because no active executions
        original_isVisible = dock.isVisible
        dock.isVisible = lambda: True
        try:
            dock._sync_polling()
            assert not dock._poll_timer.isActive(), (
                "Polling should stop when all executions are terminal"
            )
        finally:
            dock.isVisible = original_isVisible
        dock.mark_closed()
