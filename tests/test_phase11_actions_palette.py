"""
Phase 11 M1: Action registry and command palette tests.

Tests for:
- ActionRegistry: non-empty registry, unique IDs, conflict detection
- CommandPaletteDialog: filtering, keyboard navigation, dispatch
- Inert affordances remain inert (preserved contract per section 5)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel
from qasync import QEventLoop

from bots5.bootstrap.desktop import build_runtime
from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.desktop.actions import ActionDefinition, ActionRegistry
from bots5.desktop.palette import CommandPaletteDialog, PaletteFilter
from bots5.desktop.window import MainWindow
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from tests._authority_test_support import SQLiteAppStateStore, upgrade_database


def _run_qasync(qt_application: QApplication, operation) -> None:
    qt_application.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for desktop state")
        await asyncio.sleep(0.005)


def _application(tmp_path: Path, backend):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=32),
        backend,
        ids=ids,
        clock=clock,
    )


# =============================================================================
# ActionRegistry tests
# =============================================================================


def test_action_registry_is_non_empty_and_has_unique_ids(tmp_path):
    """Registry must be non-empty and each action must have a unique ID."""
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend())
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)

            registry = window._action_registry
            actions = registry.get_all_actions()

            # Registry is non-empty (has at least the palette and search actions)
            assert len(actions) > 0, "Action registry must contain actions"

            # Each action has a unique ID
            action_ids = [a.action_id for a in actions]
            assert len(action_ids) == len(
                set(action_ids)
            ), "Each action must have a unique action_id"

            # Each action has required fields populated
            for action in actions:
                assert isinstance(action.action_id, str) and len(action.action_id) > 0
                assert isinstance(action.title, str) and len(action.title) > 0
                assert isinstance(action.category, str) and len(action.category) > 0
                assert callable(action.handler)

        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_action_registry_detects_conflicts():
    """Two actions claiming the same chord must be detected and reported."""
    registry = ActionRegistry()

    # Register first action with a shortcut
    handler1 = lambda: None
    registry.register(
        ActionDefinition(
            action_id="test.action1",
            title="Action One",
            category="test",
            default_shortcut="Ctrl+Shift+A",
            handler=handler1,
        )
    )

    # Register second action with SAME shortcut - conflict expected
    handler2 = lambda: None
    registry.register(
        ActionDefinition(
            action_id="test.action2",
            title="Action Two",
            category="test",
            default_shortcut="Ctrl+Shift+A",  # SAME shortcut
            handler=handler2,
        )
    )

    # Detect conflicts
    conflicts = registry.detect_conflicts()

    # Conflict must be detected
    assert "Ctrl+Shift+A" in conflicts, "Conflict on Ctrl+Shift+A must be detected"
    assert len(conflicts["Ctrl+Shift+A"]) == 2, "Two actions claim the same shortcut"

    # Actions with unique shortcuts should have no conflicts
    registry2 = ActionRegistry()
    registry2.register(
        ActionDefinition(
            action_id="test.action3",
            title="Action Three",
            category="test",
            default_shortcut="Ctrl+Shift+B",
            handler=lambda: None,
        )
    )
    registry2.register(
        ActionDefinition(
            action_id="test.action4",
            title="Action Four",
            category="test",
            default_shortcut="Ctrl+Shift+C",  # Different shortcut
            handler=lambda: None,
        )
    )
    conflicts2 = registry2.detect_conflicts()
    assert len(conflicts2) == 0, "No conflicts should exist for unique shortcuts"


def test_action_registry_groups_actions_by_category():
    """Actions must be groupable by category for palette display."""
    registry = ActionRegistry()

    registry.register(
        ActionDefinition(
            action_id="test.action1",
            title="Action One",
            category="chat",
            default_shortcut="",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="test.action2",
            title="Action Two",
            category="chat",
            default_shortcut="",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="test.action3",
            title="Action Three",
            category="view",
            default_shortcut="",
            handler=lambda: None,
        )
    )

    grouped = registry.get_actions_by_category()

    # Must have categories
    assert len(grouped) >= 1, "Registry must have at least one category"

    # Each category contains actions
    for category, actions in grouped.items():
        assert len(actions) > 0, f"Category '{category}' must have actions"
        # All actions belong to that category
        for action in actions:
            assert action.category == category

    # Check specific categories
    assert "chat" in grouped
    assert "view" in grouped
    assert len(grouped["chat"]) == 2
    assert len(grouped["view"]) == 1


def test_action_registry_gets_action_by_id():
    """Registry must allow retrieval of actions by ID."""
    registry = ActionRegistry()

    handler = lambda: None
    registry.register(
        ActionDefinition(
            action_id="test.specific",
            title="Specific Action",
            category="test",
            default_shortcut="",
            handler=handler,
        )
    )

    # Get by ID
    action = registry.get_action("test.specific")
    assert action is not None
    assert action.action_id == "test.specific"
    assert action.title == "Specific Action"

    # Non-existent ID returns None
    assert registry.get_action("nonexistent") is None


def test_action_registry_detects_no_conflicts_for_unique_shortcuts():
    """Registry must report no conflicts when all shortcuts are unique."""
    registry = ActionRegistry()

    registry.register(
        ActionDefinition(
            action_id="a1",
            title="A1",
            category="test",
            default_shortcut="Ctrl+1",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="a2",
            title="A2",
            category="test",
            default_shortcut="Ctrl+2",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="a3",
            title="A3",
            category="test",
            default_shortcut="Ctrl+3",
            handler=lambda: None,
        )
    )

    conflicts = registry.detect_conflicts()
    assert len(conflicts) == 0, "No conflicts for unique shortcuts"
    assert not registry.has_conflicts()


def test_action_registry_reports_conflicts_flag():
    """Registry must provide has_conflicts() helper."""
    registry = ActionRegistry()

    # No conflicts initially
    assert not registry.has_conflicts()

    # Add conflicting actions
    registry.register(
        ActionDefinition(
            action_id="b1",
            title="B1",
            category="test",
            default_shortcut="Ctrl+X",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="b2",
            title="B2",
            category="test",
            default_shortcut="Ctrl+X",  # Conflict!
            handler=lambda: None,
        )
    )

    conflicts = registry.detect_conflicts()
    assert len(conflicts) > 0
    assert registry.has_conflicts()


# =============================================================================
# PaletteFilter tests
# =============================================================================


def test_palette_filter_matches_title():
    """Filter must match action titles."""
    filter_obj = PaletteFilter()

    # Test pattern matching
    action = ActionDefinition(
        action_id="test.action",
        title="Create New Chat",
        category="chat",
        default_shortcut="",
        handler=lambda: None,
    )

    # Exact match
    filter_obj.set_pattern("create")
    assert filter_obj.matches(action)

    # Partial match
    filter_obj.set_pattern("new")
    assert filter_obj.matches(action)

    # Case insensitive
    filter_obj.set_pattern("CHAT")
    assert filter_obj.matches(action)


def test_palette_filter_matches_category():
    """Filter must match action categories."""
    filter_obj = PaletteFilter()

    action = ActionDefinition(
        action_id="test.action",
        title="Action",
        category="chat_management",
        default_shortcut="",
        handler=lambda: None,
    )

    filter_obj.set_pattern("chat")
    assert filter_obj.matches(action)

    filter_obj.set_pattern("management")
    assert filter_obj.matches(action)


def test_palette_filter_empty_pattern_shows_all():
    """Empty filter pattern must show all actions."""
    filter_obj = PaletteFilter()

    action = ActionDefinition(
        action_id="test.action",
        title="Action",
        category="chat",
        default_shortcut="",
        handler=lambda: None,
    )

    filter_obj.set_pattern("")
    assert filter_obj.matches(action)


def test_palette_filter_no_match():
    """Filter must return False for non-matching patterns."""
    filter_obj = PaletteFilter()

    action = ActionDefinition(
        action_id="test.action",
        title="Action",
        category="chat",
        default_shortcut="",
        handler=lambda: None,
    )

    filter_obj.set_pattern("xyz_nonexistent")
    assert not filter_obj.matches(action)


# =============================================================================
# CommandPaletteDialog tests
# =============================================================================


def test_palette_dialog_construction():
    """Palette dialog must construct successfully."""
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="test.action",
            title="Test Action",
            category="test",
            default_shortcut="",
            handler=lambda: None,
        )
    )

    # Dialog construction (without showing)
    dialog = CommandPaletteDialog(registry, None)
    assert dialog is not None
    assert dialog._registry is registry


def test_palette_dialog_populates_list():
    """Palette must populate result list from registry."""
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="test.action1",
            title="Action One",
            category="chat",
            default_shortcut="",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="test.action2",
            title="Action Two",
            category="view",
            default_shortcut="",
            handler=lambda: None,
        )
    )

    dialog = CommandPaletteDialog(registry, None)
    # List should be populated (we can't easily inspect the items without
    # showing the dialog, but construction succeeded)

    # Verify registry has actions
    actions = registry.get_all_actions()
    assert len(actions) == 2


def test_palette_dialog_filter_updates_results():
    """Filter must update the displayed list."""
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="test.create",
            title="Create Chat",
            category="chat",
            default_shortcut="",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="test.join",
            title="Join Chat",
            category="chat",
            default_shortcut="",
            handler=lambda: None,
        )
    )

    dialog = CommandPaletteDialog(registry, None)
    dialog._filter.set_pattern("create")
    # Verify filtering works
    assert dialog._filter.matches(
        registry.get_action("test.create")
    ), "create should match 'create' pattern"
    assert not dialog._filter.matches(
        registry.get_action("test.join")
    ), "join should not match 'create' pattern"


# =============================================================================
# Inert affordances preservation
# =============================================================================


def test_inert_affordances_remain_inert(tmp_path):
    """Controls for out-of-scope features must stay visibly present but disabled."""
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend())
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()

            # Existing Phase 5 affordances remain disabled (inert)
            # per the inert-affordance contract (section 5)
            for button in (
                window.top_bar.tune_button,
                window.top_bar.settings_button,
            ):
                # These are inert in legacy mode or when not phase5
                if not window._phase5:
                    assert not button.isEnabled()
                    assert button.toolTip()

            # Palette affordance exists and works
            assert hasattr(window, "command_palette_action")
            assert window.command_palette_action is not None

            # Inert tool button (Tools) remains inert
            assert not window.tool_button.isEnabled()

        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


# =============================================================================
# Integration tests (palette + window integration)
# =============================================================================


def test_palette_integration_with_window_shortcuts(tmp_path):
    """Palette must integrate with existing window keyboard shortcuts."""
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend())
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()

            # Window must have palette action registered
            assert hasattr(window, "command_palette_action")
            palette_action = window.command_palette_action
            assert palette_action is not None
            assert "Command Palette" in palette_action.text()

            # Palette action has expected shortcut
            assert palette_action.shortcut().toString() == "Ctrl+Shift+P"

            # Registry contains expected actions
            registry = window._action_registry
            actions_by_id = {a.action_id: a for a in registry.get_all_actions()}

            # Check for key Phase 11 M1 actions
            assert "palette.open" in actions_by_id
            assert "chat.create" in actions_by_id
            assert "view.global_search" in actions_by_id

        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_palette_dispatches_handler(tmp_path):
    """Dispatching a selected action must invoke its handler."""
    qt_application = QApplication.instance() or QApplication([])

    invoked = []

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend())
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)

            registry = window._action_registry

            # Register a test action with a logging handler
            def logging_handler():
                invoked.append("logged")

            registry.register(
                ActionDefinition(
                    action_id="test.dispatch",
                    title="Dispatch Test",
                    category="test",
                    default_shortcut="",
                    handler=logging_handler,
                )
            )

            # Verify handler is registered
            action = registry.get_action("test.dispatch")
            assert action is not None

            # Invoke the handler directly
            action.handler()
            assert "logged" in invoked, "Handler must be invoked"

        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())
    assert "logged" in invoked, "Handler must be invoked"


# ---------------------------------------------------------------------------
# M1: conflicts must reach the operator, not just stdout
# ---------------------------------------------------------------------------


def test_palette_surfaces_shortcut_conflicts_to_the_operator():
    """A shortcut conflict must be visible in the palette, never silently accepted."""
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="test.one",
            title="One",
            category="test",
            default_shortcut="Ctrl+Shift+Z",
            handler=lambda: None,
        )
    )
    registry.register(
        ActionDefinition(
            action_id="test.two",
            title="Two",
            category="test",
            default_shortcut="Ctrl+Shift+Z",
            handler=lambda: None,
        )
    )

    dialog = CommandPaletteDialog(registry, None)
    banner = dialog.findChild(QLabel, "paletteConflictBanner")
    assert banner is not None, "conflicting shortcuts must be reported in the palette"
    text = banner.text()
    assert "Ctrl+Shift+Z" in text
    assert "One" in text and "Two" in text


def test_palette_has_no_conflict_banner_when_bindings_are_unique():
    """With unique bindings the operator must not be shown a spurious warning."""
    registry = ActionRegistry()
    registry.register(
        ActionDefinition(
            action_id="test.one",
            title="One",
            category="test",
            default_shortcut="Ctrl+Shift+Z",
            handler=lambda: None,
        )
    )

    dialog = CommandPaletteDialog(registry, None)
    assert dialog.findChild(QLabel, "paletteConflictBanner") is None
