"""Owned chrome dispatch and historical identity invariants; native checks are separate."""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QPoint, QPointF, QEvent, Qt, QCoreApplication
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel

from bots5.desktop.icons import action_icon
from bots5.desktop.model_selector import ModelSelectorButton
from bots5.desktop.window_chrome import NativeWindowEdges, _NativeWindowInputDispatcher
from bots5.desktop.window import MainWindow
from bots5.desktop.widgets import TopBar, MessageRow
from bots5.desktop.theme import apply_draft1_theme
from bots5.desktop.profile import DesktopSessionInfo
from bots5.domain.models import Message, MessageRole, MessageState

_application = None

@pytest.fixture
def ui():
    global _application
    _application = QApplication.instance() or QApplication([])
    _application.setQuitOnLastWindowClosed(False)
    previous = set(_application.topLevelWidgets())
    yield _application
    for widget in _application.topLevelWidgets():
        if widget not in previous:
            widget.close()
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _application.processEvents()


def test_embedded_tune_and_model_clicks_dispatch_separate_intents(ui):
    selector = ModelSelectorButton()
    selector.resize(420, 80)
    selector.show()
    ui.processEvents()
    intents = []
    selector.open_requested.connect(lambda: intents.append("picker"))
    selector.tune_button.clicked.connect(lambda: intents.append("tune"))
    QTest.mouseClick(selector.tune_button, Qt.MouseButton.LeftButton)
    assert intents == ["tune"]
    QTest.mouseClick(selector, Qt.MouseButton.LeftButton, pos=QPoint(30, 35))
    QTest.keyClick(selector, Qt.Key.Key_Space)
    assert intents == ["tune", "picker", "picker"]
    selector.tune_button.setEnabled(False)
    QTest.mouseClick(selector.tune_button, Qt.MouseButton.LeftButton)
    assert intents == ["tune", "picker", "picker"]
    selector.close()


@pytest.mark.parametrize("position,expected", [
    ((0, 0), Qt.Edge.LeftEdge | Qt.Edge.TopEdge),
    ((399, 0), Qt.Edge.RightEdge | Qt.Edge.TopEdge),
    ((0, 299), Qt.Edge.LeftEdge | Qt.Edge.BottomEdge),
    ((399, 299), Qt.Edge.RightEdge | Qt.Edge.BottomEdge),
    ((0, 150), Qt.Edge.LeftEdge), ((399, 150), Qt.Edge.RightEdge),
    ((200, 0), Qt.Edge.TopEdge), ((200, 299), Qt.Edge.BottomEdge),
])
def test_edge_press_delegates_to_compositor_without_changing_geometry(ui, monkeypatch, position, expected):
    window = QMainWindow()
    window.resize(400, 300)
    helper = NativeWindowEdges(window)
    calls = []
    monkeypatch.setattr(window, "windowHandle", lambda: SimpleNamespace(startSystemResize=lambda edge: calls.append(edge) or True))
    before = window.geometry()
    point = QPoint(*position)
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(point), QPointF(window.mapToGlobal(point)), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert helper.eventFilter(window, event)
    assert calls == [expected]
    assert window.geometry() == before
    window.close()


def test_retained_windows_share_one_input_dispatch_and_route_only_the_target(ui, monkeypatch):
    # Qt caches Python virtual overrides at QObject creation. Instrument with
    # a subclass before installation rather than patching an existing override.
    class CountingDispatcher(_NativeWindowInputDispatcher):
        def __init__(self, parent):
            super().__init__(parent)
            self.calls = []
        def eventFilter(self, watched, event):
            self.calls.append((watched, event.type()))
            return super().eventFilter(watched, event)
    previous = getattr(ui, "_bots_native_window_input_dispatcher", None)
    if previous is not None:
        ui.removeEventFilter(previous)
    dispatcher = CountingDispatcher(ui)
    ui._bots_native_window_input_dispatcher = dispatcher
    ui.installEventFilter(dispatcher)
    try:
        _assert_shared_native_dispatch(ui, monkeypatch, dispatcher)
    finally:
        ui.removeEventFilter(dispatcher)
        dispatcher.deleteLater()
        if previous is not None:
            ui._bots_native_window_input_dispatcher = previous
            ui.installEventFilter(previous)
        else:
            del ui._bots_native_window_input_dispatcher


def _assert_shared_native_dispatch(ui, monkeypatch, dispatcher):
    windows = [QMainWindow() for _ in range(10)]
    helpers = [NativeWindowEdges(window) for window in windows]
    targets = []
    for window in windows:
        window.resize(400, 300)
        target = QLabel("target", window)
        target.move(0, 0)
        targets.append(target)
        window.show()
    ui.processEvents()
    for window in windows[5:]:
        window.hide()
    ui.processEvents()
    dispatcher.calls.clear()
    helper_calls = []
    original_helper = NativeWindowEdges.eventFilter
    def hit_test(self, watched, event):
        helper_calls.append(self)
        return original_helper(self, watched, event)
    monkeypatch.setattr(NativeWindowEdges, "eventFilter", hit_test)
    QApplication.sendEvent(targets[0], QEvent(QEvent.Type.User))
    assert dispatcher.calls == [(targets[0], QEvent.Type.User)]
    assert helper_calls == []

    resize_calls = []
    monkeypatch.setattr(windows[0], "windowHandle", lambda: SimpleNamespace(startSystemResize=lambda edge: resize_calls.append(edge) or True))
    point = QPoint(0, 0)
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(point), QPointF(targets[0].mapToGlobal(point)), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(targets[0], event)
    assert helper_calls == [helpers[0]]
    assert resize_calls == [Qt.Edge.LeftEdge | Qt.Edge.TopEdge]
    assert ui._bots_native_window_input_dispatcher is dispatcher


def test_bundled_action_icons_are_available(ui):
    for name in ("clipboard", "pencil", "branch", "more", "attach", "tools", "send", "stop", "chat", "plus", "sidebar", "cog", "minimize", "maximize", "restore", "close"):
        assert not action_icon(name).pixmap(18, 18).isNull(), name


def test_disabled_icons_have_explicit_lower_contrast(ui):
    from PySide6.QtGui import QIcon
    def brightness(image):
        colors = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
        visible = [color.lightness() for color in colors if color.alpha() >= 200]
        assert visible
        return sum(visible) / len(visible)
    for name in ("pencil", "branch", "send", "stop"):
        icon = action_icon(name)
        enabled = icon.pixmap(18, 18, QIcon.Mode.Normal).toImage()
        disabled = icon.pixmap(18, 18, QIcon.Mode.Disabled).toImage()
        assert brightness(disabled) < brightness(enabled) - 25


@pytest.mark.parametrize("role", [MessageRole.USER, MessageRole.ASSISTANT])
def test_message_controls_stay_compact_and_state_belongs_to_role(ui, role):
    from datetime import datetime, UTC
    message = Message("message", "chat", role, MessageState.SENT if role is MessageRole.USER else MessageState.COMPLETE, "Body", 1, datetime.now(UTC))
    row = MessageRow(message)
    if role is MessageRole.ASSISTANT:
        row.model_identity_label.setText("historical/model-name")
        row.model_identity_label.setVisible(True)
    row.resize(1100, 150)
    row.show()
    ui.processEvents()
    assert row.actions_widget.width() <= 160
    role_label = row.findChild(QLabel, "messageRoleLabel")
    assert row.state_label.x() - role_label.geometry().right() <= 10
    positions = [button.x() for button in (row.copy_button, row.edit_button, row.branch_button, row.more_button)]
    assert max(b - a for a, b in zip(positions, positions[1:])) <= 40
    assert not row.branch_button.isEnabled()
    row.resize(420, 150)
    ui.processEvents()
    assert row.actions_widget.width() <= 160
    assert row.actions_widget.mapTo(row, row.actions_widget.rect().bottomRight()).x() < row.width()
    row.close()


def test_masthead_keeps_chrome_model_and_compact_command_toggle_reachable(ui):
    apply_draft1_theme(ui)
    bar = TopBar(DesktopSessionInfo(backend_id="fake", model="fake-v0.1"), phase5=True)
    bar.readiness_label.setText("Provider and model ready")
    bar.resize(1440, 140)
    bar.show()
    ui.processEvents()
    assert bar.status_context.isVisible()
    assert bar.rail_toggle.width() <= 40
    assert bar.rail_toggle.parentWidget() is bar.utilities
    for width in (1440, 900, 480):
        bar.resize(width, 140)
        ui.processEvents()
        for button in (bar.rail_toggle, bar.minimize_button, bar.maximize_button, bar.close_button, bar.tune_button):
            assert button.isVisible()
            rect = button.rect()
            assert button.mapTo(bar, rect.topLeft()).x() >= 0
            assert button.mapTo(bar, rect.bottomRight()).x() < bar.width()
        assert bar.model_selector.width() >= 180
    bar.close()


def test_message_identity_uses_only_unambiguous_persisted_attempt_and_stale_guard(ui):
    class Label:
        def __init__(self):
            self.text = ""
            self.visible = False
        def setText(self, value): self.text = value
        def setToolTip(self, value): self.tooltip = value
        def setVisible(self, value): self.visible = value
    def row(role):
        return SimpleNamespace(message=SimpleNamespace(role=role), model_identity_label=Label())
    rows = {"old": row(MessageRole.ASSISTANT), "ambiguous": row(MessageRole.ASSISTANT), "user": row(MessageRole.USER)}
    attempts = [SimpleNamespace(assistant_message_id="old", model="requested-old", returned_model="actual-old"), SimpleNamespace(assistant_message_id="ambiguous", model="one", returned_model=None), SimpleNamespace(assistant_message_id="ambiguous", model="two", returned_model=None), SimpleNamespace(assistant_message_id="user", model="bad", returned_model=None)]
    shell = SimpleNamespace(_current_chat_id="chat", _refresh_generation=1, transcript=SimpleNamespace(message_rows=rows))
    async def listed(_chat): return attempts
    shell._application = SimpleNamespace(list_generation_attempts=listed)
    asyncio.run(MainWindow._refresh_message_model_identities(shell, "chat", 1))
    assert rows["old"].model_identity_label.text == "actual-old"
    assert rows["old"].model_identity_label.visible
    assert not rows["ambiguous"].model_identity_label.visible
    assert not rows["user"].model_identity_label.visible
    rows["old"].model_identity_label.text = "sentinel"
    async def switched(_chat):
        shell._current_chat_id = "different"
        return attempts
    shell._application.list_generation_attempts = switched
    asyncio.run(MainWindow._refresh_message_model_identities(shell, "chat", 1))
    assert rows["old"].model_identity_label.text == "sentinel"


def test_hidden_text_strip_preserves_original_shortcuts_and_cog_workflow(ui, tmp_path):
    from tests.test_phase11_model_selector import _phase5_application, _run_qasync, _wait_until
    from tests.test_phase11_workspace_restoration import _dispose_window
    from PySide6.QtTest import QSignalSpy

    async def scenario():
        application, _store = _phase5_application(tmp_path)
        window = MainWindow(application)
        try:
            await window.initialize()
            window.show()
            await asyncio.sleep(0.05)
            assert window._console_menu_bar.isHidden()
            original = tuple(window._native_menu_bar.actions())
            assert tuple(window.top_bar.navigation_more.menu().actions()) == original
            for action in (window.new_window_action, window.global_search_action, window.in_chat_search_action, window.command_palette_action):
                assert action in window.actions()
            new_window = QSignalSpy(window.new_window_requested)
            window.activateWindow()
            window.setFocus()
            QTest.keyClick(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            assert new_window.count() == 1
            window.new_window_action.trigger()
            assert new_window.count() == 2
            QTest.keyClick(window, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
            await _wait_until(lambda: window.search_dock.isVisible())
            window.search_dock.hide()

            assert window.top_bar.tune_button.isEnabled()
            QTest.mouseClick(window.top_bar.tune_button, Qt.MouseButton.LeftButton)
            await _wait_until(lambda: window._tune_dialog is not None and window._tune_dialog.isVisible())
            assert window._model_selector_popup is None
            window._tune_dialog.close()
            QTest.mouseClick(window.top_bar.model_selector, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
            assert window._model_selector_popup is not None and window._model_selector_popup.isVisible()
            assert not window._tune_dialog.isVisible()
            window._model_selector_popup.close()

            QTest.mouseClick(window.top_bar.maximize_button, Qt.MouseButton.LeftButton)
            await asyncio.sleep(0.02)
            assert window.isMaximized()
            assert window.top_bar.maximize_button.accessibleName() == "Restore window"
            QTest.mouseClick(window.top_bar.maximize_button, Qt.MouseButton.LeftButton)
            await asyncio.sleep(0.02)
            assert not window.isMaximized()
            assert window.top_bar.maximize_button.accessibleName() == "Maximize window"
        finally:
            if window._tune_dialog is not None:
                window._tune_dialog.close()
            if window._model_selector_popup is not None:
                window._model_selector_popup.close()
            await _dispose_window(window)
            await application.close()
    _run_qasync(ui, scenario())
