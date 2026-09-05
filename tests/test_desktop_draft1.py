from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from qasync import QEventLoop

from bots5.bootstrap.desktop import build_runtime
from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.core.generation import (
    GenerationCompleted,
    GenerationDelta,
    GenerationFailed,
    GenerationRequest,
)
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import AttemptState, MessageRole, MessageState
from bots5.desktop.window import MainWindow
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database


def _run_qasync(qt_application: QApplication, operation: Awaitable[None]) -> None:
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for desktop state")
        await asyncio.sleep(0.005)


def _application(tmp_path: Path, backend) -> BotsApplication:
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


class BlockingBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, request: GenerationRequest):
        yield GenerationDelta(attempt_id=request.attempt_id, text="partial")
        self.started.set()
        await asyncio.Event().wait()


class LifecycleBackend:
    def __init__(self, *, blocking_calls=(), modes=None) -> None:
        self.blocking_calls = set(blocking_calls)
        self.modes = dict(modes or {})
        self.calls = 0
        self.started: dict[int, asyncio.Event] = {}
        self.released: dict[int, asyncio.Event] = {}

    async def wait_started(self, call: int) -> None:
        await self.started.setdefault(call, asyncio.Event()).wait()

    def release(self, call: int) -> None:
        self.released.setdefault(call, asyncio.Event()).set()

    async def stream(self, request: GenerationRequest):
        self.calls += 1
        call = self.calls
        self.started.setdefault(call, asyncio.Event()).set()
        yield GenerationDelta(attempt_id=request.attempt_id, text=f"response {call}")
        if call in self.blocking_calls:
            await self.released.setdefault(call, asyncio.Event()).wait()
        mode = self.modes.get(call, "complete")
        if mode == "failed":
            yield GenerationFailed(
                attempt_id=request.attempt_id,
                error_type="test_failure",
                error_message="deterministic test failure",
            )
        elif mode == "incomplete":
            yield GenerationCompleted(attempt_id=request.attempt_id, finish_reason="length")
        else:
            yield GenerationCompleted(attempt_id=request.attempt_id)


def test_draft1_shell_uses_native_frame_and_approved_inert_affordances(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend(delay_seconds=0.01))
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()

            assert not bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
            assert len(window.findChildren(QLabel, "modelPill")) == 1
            assert window.top_bar.model_pill.text() == "fake-v0.1"
            for button in (
                window.top_bar.tune_button,
                window.top_bar.settings_button,
                window.attachment_button,
                window.tool_button,
            ):
                assert not button.isEnabled()
                assert button.toolTip()
            assert not window.inspector_dock.isVisible()

            window.top_bar.rail_toggle.click()
            assert window.rail.collapsed
            assert not window.chat_list.isVisible()
            window.top_bar.rail_toggle.click()
            assert not window.rail.collapsed
            assert window.chat_list.isVisible()

            window.composer.setPlainText("line one")
            window.composer.setFocus()
            QTest.keyClick(
                window.composer,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.ShiftModifier,
            )
            assert "line one" in window.composer.toPlainText()
            assert "\n" in window.composer.toPlainText()
            assert len(window.transcript.message_rows) == 0

            QTest.keyClick(window.composer, Qt.Key.Key_Return)
            await _wait_until(lambda: len(window._current_messages) == 2)
            await _wait_until(lambda: not window._generation_busy)
            assert [message.role for message in window._current_messages] == [
                MessageRole.USER,
                MessageRole.ASSISTANT,
            ]
            assert window._current_messages[-1].state.value == "complete"
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_draft1_collapsed_rail_stacks_controls_vertically_across_toggles(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend())
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()

            for collapsed in (True, False, True, False, True):
                window.rail.set_collapsed(collapsed)
                qt_application.processEvents()
                if collapsed:
                    assert window.rail.icon_row.direction().name == "TopToBottom"
                    first = window.rail.chat_button.geometry()
                    second = window.rail.new_chat_button.geometry()
                    assert second.top() > first.top()
                    assert abs(second.center().x() - first.center().x()) <= 1
                    assert window.rail.width() == 48
                else:
                    assert window.rail.icon_row.direction().name == "LeftToRight"
                    assert window.chat_list.isVisible()
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_draft1_qasync_boundary_preserves_durable_state_across_close_reopen(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        data_root = tmp_path / "data"
        runtime = build_runtime(data_root)
        window = MainWindow(runtime.application, runtime.session)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            window.composer.setPlainText("durable shell message")
            window.send_button.click()
            await asyncio.sleep(0.1)
            chat_id = window._current_chat_id
            assert chat_id is not None
            _, messages = await runtime.application.open_chat(chat_id)
            assert messages[-1].content == "fake response to: durable shell message"
            assert messages[-1].state.value == "complete"
        finally:
            await window.stop_bridge_async()
            window.close()
            await runtime.close()

        restarted = build_runtime(data_root)
        try:
            chats = await restarted.application.list_chats()
            assert len(chats) == 1
            _, messages = await restarted.application.open_chat(chats[0].id)
            assert [message.content for message in messages] == [
                "durable shell message",
                "fake response to: durable shell message",
            ]
        finally:
            await restarted.close()

    _run_qasync(qt_application, scenario())


def test_draft1_busy_state_blocks_navigation_and_stop_persists_abort(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        backend = BlockingBackend()
        application = _application(tmp_path, backend)
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()
            first_chat_id = window._current_chat_id
            assert first_chat_id is not None
            await application.create_chat("Second chat")
            window._replace_chat_list(await application.list_chats())
            window._select_chat_row(first_chat_id)

            window.composer.setPlainText("cancel me")
            window.send_button.click()
            await backend.started.wait()
            await _wait_until(lambda: window.cancel_button.isEnabled())
            await _wait_until(lambda: window.generation_indicator.isVisible())
            await _wait_until(
                lambda: any(row.activity_label.isVisible() for row in window.transcript.message_rows.values())
            )

            assert window._generation_busy
            assert window.generation_indicator.text() == "● Generating…"
            assert window.composer.isReadOnly()
            assert window.chat_list.isEnabled()
            assert window.new_chat_button.isEnabled()
            assert not window.send_button.isEnabled()
            window._on_chat_selected(1)
            await _wait_until(lambda: window._current_chat_id != first_chat_id)
            window._on_chat_selected(window._chat_ids.index(first_chat_id))
            await asyncio.sleep(0.02)
            assert window._current_chat_id == first_chat_id

            window.cancel_button.click()
            await _wait_until(lambda: not window._generation_busy)
            assert not window.generation_indicator.isVisible()
            assert not any(
                row.activity_label.isVisible() for row in window.transcript.message_rows.values()
            )
            attempts = await application.list_generation_attempts(first_chat_id)
            assert len(attempts) == 1
            assert attempts[0].state is AttemptState.ABORTED
            assert window._current_messages[-1].state.value == "aborted"
            assert window._current_messages[-1].content == "partial"
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_draft1_edit_and_regenerate_expose_active_generation_state(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        backend = LifecycleBackend(blocking_calls={2, 3})
        application = _application(tmp_path, backend)
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()
            window.composer.setPlainText("original")
            window.send_button.click()
            await _wait_until(lambda: backend.calls >= 1)
            await _wait_until(lambda: not window._generation_busy)

            user = next(
                message for message in window._current_messages if message.role is MessageRole.USER
            )
            window.transcript.message_rows[user.id].edit_button.click()
            window.composer.setPlainText("edited")
            window.send_button.click()
            await backend.wait_started(2)
            await _wait_until(lambda: window.generation_indicator.isVisible())
            await _wait_until(
                lambda: any(row.activity_label.isVisible() for row in window.transcript.message_rows.values())
            )
            assert window.cancel_button.isEnabled()
            backend.release(2)
            await _wait_until(lambda: not window._generation_busy)
            assert not window.generation_indicator.isVisible()
            assert not any(
                row.activity_label.isVisible() for row in window.transcript.message_rows.values()
            )

            assistant = next(
                message
                for message in reversed(window._current_messages)
                if message.role is MessageRole.ASSISTANT
            )
            window.transcript.message_rows[assistant.id].regenerate_action.trigger()
            await backend.wait_started(3)
            await _wait_until(lambda: window.generation_indicator.isVisible())
            await _wait_until(
                lambda: any(row.activity_label.isVisible() for row in window.transcript.message_rows.values())
            )
            assert window.cancel_button.isEnabled()
            backend.release(3)
            await _wait_until(lambda: not window._generation_busy)
            assert not window.generation_indicator.isVisible()
            assert not any(
                row.activity_label.isVisible() for row in window.transcript.message_rows.values()
            )
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


@pytest.mark.parametrize(
    ("mode", "message_state", "attempt_state"),
    (
        ("incomplete", MessageState.TRUNCATED, AttemptState.INCOMPLETE),
        ("failed", MessageState.FAILED, AttemptState.FAILED),
    ),
)
def test_draft1_terminal_states_clear_generation_activity(
    tmp_path,
    mode,
    message_state,
    attempt_state,
):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        backend = LifecycleBackend(blocking_calls={1}, modes={1: mode})
        application = _application(tmp_path, backend)
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()
            window.composer.setPlainText(f"trigger {mode}")
            window.send_button.click()
            await backend.wait_started(1)
            await _wait_until(lambda: window.generation_indicator.isVisible())
            backend.release(1)
            await _wait_until(
                lambda: window._current_messages
                and window._current_messages[-1].state is message_state
            )
            await _wait_until(lambda: not window._generation_busy)
            assert not window.generation_indicator.isVisible()
            assert not any(
                row.activity_label.isVisible() for row in window.transcript.message_rows.values()
            )
            attempts = await application.list_generation_attempts(window._current_chat_id)
            assert attempts[0].state is attempt_state
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_draft1_switching_chats_clears_edit_context_before_send(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend(delay_seconds=0.005))
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()
            chat_a_id = window._current_chat_id
            assert chat_a_id is not None

            window.composer.setPlainText("message from A")
            window.send_button.click()
            await _wait_until(
                lambda: len(window._current_messages) == 2
                and window._current_messages[-1].state.value == "complete"
            )
            await _wait_until(lambda: not window._generation_busy)
            user_a = next(
                message
                for message in window._current_messages
                if message.role is MessageRole.USER
            )
            user_row = window.transcript.message_rows[user_a.id]
            user_row.edit_button.click()
            assert window._editing_message_id == user_a.id
            assert window.editing_label.isVisible()

            chat_b = await application.create_chat("Chat B")
            window._replace_chat_list(await application.list_chats())
            window._select_chat_row(chat_b.id)
            await _wait_until(lambda: window._current_chat_id == chat_b.id)
            await _wait_until(lambda: window._editing_message_id is None)
            assert window._editing_chat_id is None
            assert not window.editing_label.isVisible()
            assert not window.cancel_edit_button.isVisible()
            assert window.composer.toPlainText() == "message from A"

            window.send_button.click()
            await _wait_until(
                lambda: len(window._current_messages) == 2
                and all(message.chat_id == chat_b.id for message in window._current_messages)
                and window._current_messages[-1].state.value == "complete"
            )
            history_b = await application.list_message_history(chat_b.id)
            assert [message.content for message in history_b] == [
                "message from A",
                "fake response to: message from A",
            ]
            assert len(await application.list_generation_attempts(chat_a_id)) == 1
            assert len(await application.list_generation_attempts(chat_b.id)) == 1
            assert window._editing_message_id is None
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_draft1_new_chat_creation_clears_active_edit_context(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend(delay_seconds=0.005))
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()
            window.composer.setPlainText("editable")
            window.send_button.click()
            await _wait_until(lambda: len(window._current_messages) == 2)
            await _wait_until(lambda: not window._generation_busy)
            user = next(
                message
                for message in window._current_messages
                if message.role is MessageRole.USER
            )
            window.transcript.message_rows[user.id].edit_button.click()
            assert window._editing_message_id == user.id

            window.new_chat_button.click()
            assert window._editing_message_id is None
            await _wait_until(
                lambda: window._current_chat_id is not None
                and window._current_chat_id != user.chat_id
            )
            assert window._editing_chat_id is None
            assert not window.editing_label.isVisible()
            assert not window.cancel_edit_button.isVisible()
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_draft1_message_actions_and_inspector_use_core_commands(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _application(tmp_path, FakeStreamingBackend(delay_seconds=0.005))
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)
            window.show()
            qt_application.processEvents()
            window.composer.setPlainText("original")
            window.send_button.click()
            await _wait_until(lambda: len(window.transcript.message_rows) == 2)
            await _wait_until(lambda: not window._generation_busy)

            user_row = next(
                row
                for row in window.transcript.message_rows.values()
                if row.message.role is MessageRole.USER
            )
            assistant_row = next(
                row
                for row in window.transcript.message_rows.values()
                if row.message.role is MessageRole.ASSISTANT
            )
            assert user_row.edit_button.isEnabled()
            assert not user_row.branch_button.isEnabled()
            assert assistant_row.regenerate_action.isEnabled()
            assert not assistant_row.branch_button.isEnabled()

            user_row.copy_button.click()
            assert qt_application.clipboard().text() == "original"

            user_row.edit_button.click()
            assert window.composer.toPlainText() == "original"
            window.composer.setPlainText("edited")
            window.send_button.click()
            await _wait_until(
                lambda: len(window._current_messages) >= 2
                and window._current_messages[-2].content == "edited"
            )
            await _wait_until(lambda: not window._generation_busy)

            assistant_row = next(
                row
                for row in window.transcript.message_rows.values()
                if row.message.role is MessageRole.ASSISTANT
            )
            assistant_row.regenerate_action.trigger()
            await _wait_until(lambda: window._generation_busy)
            await _wait_until(lambda: not window._generation_busy)
            history = await application.list_message_history(window._current_chat_id)
            assert len(history) == 5
            assert history[-1].content == "fake response to: edited"
            assert history[-1].state.value == "complete"

            assistant_row = next(
                row
                for row in window.transcript.message_rows.values()
                if row.message.id == history[-1].id
            )
            assistant_row.inspect_action.trigger()
            await _wait_until(lambda: window.inspector_dock.isVisible())
            await _wait_until(
                lambda: any(
                    label.text() == "complete"
                    for label in window.inspector.findChildren(QLabel, "inspectorValue")
                )
            )
            assert window.top_bar.details_button.isChecked()
            window.top_bar.details_button.click()
            await _wait_until(lambda: not window.inspector_dock.isVisible())
        finally:
            await window.stop_bridge_async()
            await application.close()

    _run_qasync(qt_application, scenario())
