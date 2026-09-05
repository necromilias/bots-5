from __future__ import annotations

import asyncio
import os
import sqlite3
from types import SimpleNamespace
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from bots5.bootstrap.desktop import build_runtime
from bots5.core.application import BotsApplication
from bots5.core.errors import StateError
from bots5.core.events import EventBus
from bots5.core.generation import (
    GenerationCompleted,
    GenerationDelta,
    GenerationFailed,
    GenerationRequest,
)
from bots5.desktop.profile import DesktopSessionInfo
from bots5.desktop.session import DesktopSessionController
from bots5.desktop.window import MainWindow
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import AttemptState, ChatActivity
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database


MIGRATIONS = Path(__file__).resolve().parents[1] / "src/bots5/infrastructure/persistence/migrations"


class ControlledBackend:
    def __init__(self, modes: dict[str, str] | None = None) -> None:
        self.modes = modes or {}
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    async def stream(self, request: GenerationRequest):
        self.started.setdefault(request.chat_id, asyncio.Event()).set()
        yield GenerationDelta(attempt_id=request.attempt_id, text=f"partial-{request.chat_id}")
        mode = self.modes.get(request.prompt, "complete")
        if mode == "block":
            await self.release.setdefault(request.chat_id, asyncio.Event()).wait()
        if mode == "failed":
            yield GenerationFailed(
                attempt_id=request.attempt_id,
                error_type="controlled_failure",
                error_message="deterministic failure",
            )
        else:
            yield GenerationCompleted(attempt_id=request.attempt_id)


def _application(tmp_path: Path, backend) -> BotsApplication:
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=64),
        backend,
        ids=ids,
        clock=clock,
    )


def _upgrade_to(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, revision)


async def _wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


async def _terminal(subscription, attempt_id: str):
    terminal = {
        "generation_completed",
        "generation_incomplete",
        "generation_failed",
        "generation_aborted",
    }
    async for event in subscription:
        if event.kind in terminal and event.payload.get("attempt_id") == attempt_id:
            return event
    raise AssertionError(f"terminal event not observed: {attempt_id}")


async def _terminals(subscription, attempt_ids: set[str]) -> None:
    seen: set[str] = set()
    async for event in subscription:
        if event.kind.startswith("generation_") and event.kind in {
            "generation_completed",
            "generation_incomplete",
            "generation_failed",
            "generation_aborted",
        }:
            attempt_id = event.payload.get("attempt_id")
            if attempt_id in attempt_ids:
                seen.add(attempt_id)
                if seen == attempt_ids:
                    return
    raise AssertionError("not all terminal events were observed")


def test_phase4_cross_chat_concurrency_failure_and_same_chat_rejection(tmp_path):
    async def scenario():
        backend = ControlledBackend({"two": "failed"})
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat_a = await application.create_chat("A")
            chat_b = await application.create_chat("B")
            first = asyncio.create_task(application.send_message(chat_a.id, "one"))
            second = asyncio.create_task(application.send_message(chat_b.id, "two"))
            await asyncio.gather(
                backend.started.setdefault(chat_a.id, asyncio.Event()).wait(),
                backend.started.setdefault(chat_b.id, asyncio.Event()).wait(),
            )
            attempt_a, attempt_b = await asyncio.gather(first, second)
            with pytest.raises(StateError, match="active generation"):
                await application.send_message(chat_a.id, "same chat is rejected")
            await _terminals(subscription, {attempt_a.id, attempt_b.id})
            stored_a = await application.list_generation_attempts(chat_a.id)
            stored_b = await application.list_generation_attempts(chat_b.id)
            assert stored_a[0].state is AttemptState.COMPLETE
            assert stored_b[0].state is AttemptState.FAILED
            _, messages_a = await application.open_chat(chat_a.id)
            _, messages_b = await application.open_chat(chat_b.id)
            assert messages_a[-1].content.startswith("partial-")
            assert messages_b[-1].content.startswith("partial-")
        finally:
            subscription.close()
            await application.close()

    asyncio.run(scenario())


def test_phase4_terminal_generation_allows_later_same_chat_generation(tmp_path):
    async def scenario():
        application = _application(tmp_path, ControlledBackend())
        subscription = application.subscribe()
        try:
            chat = await application.create_chat("A")
            first = await application.send_message(chat.id, "first")
            await _terminal(subscription, first.id)
            second = await application.send_message(chat.id, "second")
            await _terminal(subscription, second.id)
            assert [
                attempt.state
                for attempt in await application.list_generation_attempts(chat.id)
            ] == [AttemptState.COMPLETE, AttemptState.COMPLETE]
        finally:
            subscription.close()
            await application.close()

    asyncio.run(scenario())


def test_phase4_independent_cancel_and_duplicate_cancel_are_safe(tmp_path):
    async def scenario():
        backend = ControlledBackend({"one": "block", "two": "block"})
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat_a = await application.create_chat("A")
            chat_b = await application.create_chat("B")
            first, second = await asyncio.gather(
                application.send_message(chat_a.id, "one"),
                application.send_message(chat_b.id, "two"),
            )
            await asyncio.gather(
                backend.started[chat_a.id].wait(),
                backend.started[chat_b.id].wait(),
            )
            cancelled = await application.cancel_generation(first.id)
            assert cancelled.state is AttemptState.ABORTED
            duplicate = await application.cancel_generation(first.id)
            assert duplicate.state is AttemptState.ABORTED
            backend.release[chat_b.id].set()
            await _terminal(subscription, second.id)
            assert (await application.list_generation_attempts(chat_b.id))[0].state is AttemptState.COMPLETE
        finally:
            subscription.close()
            await application.close()

    asyncio.run(scenario())


def test_phase4_migrates_legacy_same_chat_active_attempts_before_index(tmp_path):
    database = tmp_path / "legacy-duplicate-running.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at) VALUES "
                    "('chat', 'Legacy', '2026-09-05T00:00:00.000Z', "
                    "'2026-09-05T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES "
                    "('u1', 'chat', NULL, 1, 'user', 'sent', 'one', "
                    "'2026-09-05T00:00:00.000Z'), "
                    "('a1', 'chat', 'u1', 2, 'assistant', 'streaming', 'partial-one', "
                    "'2026-09-05T00:00:00.000Z'), "
                    "('u2', 'chat', 'a1', 3, 'user', 'sent', 'two', "
                    "'2026-09-05T00:00:00.000Z'), "
                    "('a2', 'chat', 'u2', 4, 'assistant', 'streaming', 'partial-two', "
                    "'2026-09-05T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO generation_attempts "
                    "(id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
                    "state, request_snapshot, started_at) VALUES "
                    "('g1', 'chat', 'u1', 'a1', 'fake', 'fake-v0.1', 'running', '{}', "
                    "'2026-09-05T00:00:00.000Z'), "
                    "('g2', 'chat', 'u2', 'a2', 'fake', 'fake-v0.1', 'running', '{}', "
                    "'2026-09-05T00:00:01.000Z')"
                )
            )
    finally:
        engine.dispose()

    _upgrade_to(database, "0005_generation_outcomes")
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        attempts = store.list_generation_attempts("chat")
        messages = store.list_messages("chat")
        assert attempts[0].state is AttemptState.ABORTED
        assert attempts[0].remote_outcome_unknown is True
        assert attempts[0].error_type == "aborted"
        assert attempts[0].ended_at is not None
        assert messages[1].state.value == "aborted"
        assert messages[1].content == "partial-one"
        assert attempts[1].state is AttemptState.RUNNING
        with store.engine.connect() as connection:
            indexes = connection.exec_driver_sql(
                "PRAGMA index_list(generation_attempts)"
            ).fetchall()
            active = next(row for row in indexes if row[1] == "ux_generation_attempts_active_chat")
            assert active[2] == 1
            assert active[4] == 1
    finally:
        store.close()

    upgrade_database(database)
    reopened = SQLiteAppStateStore.open(database)
    try:
        assert [attempt.state for attempt in reopened.list_generation_attempts("chat")] == [
            AttemptState.ABORTED,
            AttemptState.RUNNING,
        ]
    finally:
        reopened.close()

    async def scenario():
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids, queue_size=8),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        try:
            assert all(
                attempt.state is AttemptState.ABORTED
                for attempt in await application.list_generation_attempts("chat")
            )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_phase4_stamped_schema_requires_workspace_table(tmp_path):
    database = tmp_path / "missing-workspace.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE workspace_windows")
    with pytest.raises(RuntimeError, match="workspace_windows"):
        SQLiteAppStateStore.open(database)


def test_phase4_stamped_schema_requires_partial_unique_active_chat_index(tmp_path):
    database = tmp_path / "malformed-active-index.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ux_generation_attempts_active_chat")
        connection.execute(
            "CREATE UNIQUE INDEX ux_generation_attempts_active_chat "
            "ON generation_attempts (chat_id)"
        )
    with pytest.raises(RuntimeError, match="unique and partial"):
        SQLiteAppStateStore.open(database)


@pytest.mark.parametrize(
    "predicate",
    (
        "state = 'running' AND 0",
        "state = 'running' OR 1",
        "state = 'RUNNING'",
        "state = 'RuNnInG'",
    ),
)
def test_phase4_stamped_schema_rejects_malformed_active_chat_predicate(tmp_path, predicate):
    database = tmp_path / "malformed-active-predicate.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ux_generation_attempts_active_chat")
        connection.execute(
            "CREATE UNIQUE INDEX ux_generation_attempts_active_chat "
            "ON generation_attempts (chat_id) WHERE " + predicate
        )
    with pytest.raises(RuntimeError, match="filtered to running attempts"):
        SQLiteAppStateStore.open(database)


def test_phase4_stamped_schema_rejects_wrong_active_chat_index_column(tmp_path):
    database = tmp_path / "wrong-active-column.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ux_generation_attempts_active_chat")
        connection.execute(
            "CREATE UNIQUE INDEX ux_generation_attempts_active_chat "
            "ON generation_attempts (backend_id) WHERE state = 'running'"
        )
    with pytest.raises(RuntimeError, match="must cover only generation_attempts.chat_id"):
        SQLiteAppStateStore.open(database)


def test_phase4_stamped_schema_rejects_missing_active_chat_index(tmp_path):
    database = tmp_path / "missing-active-index.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ux_generation_attempts_active_chat")
    with pytest.raises(RuntimeError, match="missing the active-chat uniqueness index"):
        SQLiteAppStateStore.open(database)


def test_phase4_valid_stamped_schema_opens(tmp_path):
    database = tmp_path / "valid-phase4.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        assert store.list_workspace_windows() == ()
    finally:
        store.close()


def test_phase4_stamped_schema_accepts_quoted_case_variant_state_identifier(tmp_path):
    database = tmp_path / "quoted-state-index.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ux_generation_attempts_active_chat")
        connection.execute(
            "CREATE UNIQUE INDEX ux_generation_attempts_active_chat "
            "ON generation_attempts (\"chat_id\") WHERE [STATE] = 'running'"
        )
    store = SQLiteAppStateStore.open(database)
    store.close()


def test_phase4_workspace_state_and_malformed_rows_fall_back_safely(tmp_path):
    async def scenario():
        application = _application(tmp_path, ControlledBackend())
        try:
            chat = await application.create_chat("Restored")
            saved = await application.save_workspace_window(
                window_id="window-a",
                ordinal=0,
                geometry=(10, 20, 900, 700),
                selected_chat_id=chat.id,
                rail_collapsed=True,
            )
            assert saved.geometry == (10, 20, 900, 700)
            assert (await application.list_workspace_windows())[0].selected_chat_id == chat.id
            database = tmp_path / "state.sqlite3"
            with application._store.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE workspace_windows SET geometry_json = '{broken' WHERE window_id = 'window-a'"
                )
            assert await application.list_workspace_windows() == ()
        finally:
            await application.close()

    asyncio.run(scenario())


def test_phase4_session_attention_is_ephemeral_and_clears_when_viewed(tmp_path):
    async def scenario():
        backend = ControlledBackend({"fail": "failed"})
        application = _application(tmp_path, backend)
        workspace = DesktopSessionController(
            application,
            DesktopSessionInfo("fake", "fake-v0.1"),
        )
        window = SimpleNamespace()
        workspace.register_window(window, window_id="window-a", ordinal=0)
        try:
            chat_a = await application.create_chat("A")
            chat_b = await application.create_chat("B")
            workspace.set_selected_chat("window-a", chat_b.id)
            subscription = application.subscribe()
            attempt = await application.send_message(chat_a.id, "fail")
            await _terminal(subscription, attempt.id)
            subscription.close()
            await asyncio.sleep(0.03)
            activity = workspace.activity_for(chat_a.id)
            assert activity.needs_attention
            assert not activity.background_completion
            workspace.set_selected_chat("window-a", chat_a.id)
            assert workspace.activity_for(chat_a.id) == ChatActivity()
        finally:
            await workspace.unregister_window("window-a")
            await workspace.close()
            await application.close()

    asyncio.run(scenario())


def test_phase4_restart_reconciles_several_interrupted_attempts(tmp_path):
    async def scenario():
        backend = ControlledBackend({"one": "block", "two": "block"})
        application = _application(tmp_path, backend)
        try:
            first_chat = await application.create_chat("A")
            second_chat = await application.create_chat("B")
            first, second = await asyncio.gather(
                application.send_message(first_chat.id, "one"),
                application.send_message(second_chat.id, "two"),
            )
            await asyncio.gather(
                backend.started[first_chat.id].wait(),
                backend.started[second_chat.id].wait(),
            )
            _, first_messages = await application.open_chat(first_chat.id)
            _, second_messages = await application.open_chat(second_chat.id)
            assert first_messages[-1].content.startswith("partial-")
            assert second_messages[-1].content.startswith("partial-")
            application._store.close()
            for task in tuple(application._generation_tasks.values()):
                task.cancel()
            await asyncio.gather(*tuple(application._generation_tasks.values()), return_exceptions=True)
        finally:
            if not application._store._closed:
                await application.close()
        restarted = _application(tmp_path, ControlledBackend())
        try:
            assert (await restarted.list_generation_attempts(first_chat.id))[0].state is AttemptState.ABORTED
            assert (await restarted.list_generation_attempts(second_chat.id))[0].state is AttemptState.ABORTED
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_phase4_multiple_windows_share_core_and_nonfinal_close_is_view_only(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        runtime = build_runtime(tmp_path / "data")
        try:
            first = await runtime.open_window()
            first_states = await runtime.application.list_workspace_windows()
            assert len(first_states) == 1
            assert first_states[0].window_id == first._window_id
            await runtime.application.create_chat("Second")
            second = await runtime.open_window()
            durable_states = await runtime.application.list_workspace_windows()
            assert {state.window_id for state in durable_states} == {
                first._window_id,
                second._window_id,
            }
            assert runtime.workspace.window_count == 2
            assert len(runtime.application._events._subscriptions) == 1
            assert first._application is second._application is runtime.application
            assert first._current_chat_id != second._current_chat_id
            assert first.new_window_action.text() == "New Window"
            first_chat_ids = tuple(first._chat_ids)
            first.new_window_action.trigger()
            await _wait_until(lambda: runtime.workspace.window_count == 3)
            assert len(runtime.application._events._subscriptions) == 1
            assert len(runtime.windows) == 3

            first.close()
            await _wait_until(lambda: runtime.workspace.window_count == 2)
            await _wait_until(lambda: first._workspace_attached is False)
            remaining_states = await runtime.application.list_workspace_windows()
            assert first._window_id is None
            assert first_states[0].window_id not in {state.window_id for state in remaining_states}
            assert {state.window_id for state in remaining_states} == {
                second._window_id,
                runtime.windows[1]._window_id,
            }
            created = await runtime.application.create_chat("After close")
            await asyncio.sleep(0.03)
            assert created.id not in first._chat_ids
            assert tuple(first._chat_ids) == first_chat_ids
            assert created.id in second._chat_ids
            assert not runtime.application.has_active_generations()
        finally:
            for window in tuple(runtime.windows):
                if window._window_id is not None:
                    await window.stop_bridge_async()
            await runtime.close()

    from qasync import QEventLoop

    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(scenario())


def test_phase4_failed_production_window_initialization_leaves_no_restore_entry(
    tmp_path, monkeypatch
):
    qt_application = QApplication.instance() or QApplication([])

    async def failing_initialize(self):
        await original_initialize(self)
        raise RuntimeError("simulated initialization failure")

    original_initialize = MainWindow.initialize
    monkeypatch.setattr(MainWindow, "initialize", failing_initialize)

    async def scenario():
        runtime = build_runtime(tmp_path / "data")
        try:
            with pytest.raises(RuntimeError, match="simulated initialization failure"):
                await runtime.open_window()
            assert await runtime.application.list_workspace_windows() == ()
            assert runtime.workspace.window_count == 0
        finally:
            await runtime.close()

    from qasync import QEventLoop

    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(scenario())


def test_phase4_immediate_restart_restores_two_production_windows(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        data_root = tmp_path / "data"
        runtime = build_runtime(data_root)
        try:
            await runtime.open_window()
            await runtime.open_window()
            states = await runtime.application.list_workspace_windows()
            assert len(states) == 2
            original_ids = tuple(state.window_id for state in states)
            assert all(
                UUID(window_id.removeprefix("window-")).version == 7
                for window_id in original_ids
            )
        finally:
            await runtime.close()

        restarted = build_runtime(data_root)
        try:
            restored_states = await restarted.workspace.load_workspace()
            assert len(restored_states) == 2
            assert tuple(state.window_id for state in restored_states) == original_ids
            for state in restored_states:
                await restarted.open_window(state)
            assert restarted.workspace.window_count == 2
        finally:
            await restarted.close()

    from qasync import QEventLoop

    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(scenario())


def test_phase4_final_window_close_preserves_restore_entry(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        runtime = build_runtime(tmp_path / "data")
        try:
            window = await runtime.open_window()
            window_id = window._window_id
            window.close()
            await _wait_until(lambda: runtime.workspace.window_count == 0)
            states = await runtime.application.list_workspace_windows()
            assert len(states) == 1
            assert states[0].window_id == window_id
            assert states[0].restore_open is True
        finally:
            await runtime.close()

    from qasync import QEventLoop

    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(scenario())
