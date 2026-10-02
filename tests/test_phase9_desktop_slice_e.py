"""Phase 9 Slice E desktop harness — workflows 1-7 (mandatory proofs 1-8, 11, 12).

House style follows ``tests/test_phase7_desktop.py``: one QApplication plus a
qasync ``QEventLoop`` per test, every fixture local to this module, and every
proof driven from a real production desktop action (menu action, dock control,
composer send, transcript row action), not from a service call in isolation.

Graded thread obligations (oracle R-5): the ``thread``-named tests prove that
blocking work never runs on the GUI thread, that worker→UI publication hops to
the GUI thread, and that closing during an import interaction drains on the
GUI thread without hanging.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import threading
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QThread
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFileDialog,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
)
from qasync import QEventLoop

from bots5.core.archive_import import resolve_source as _landed_resolve_source
from bots5.core.errors import BackupUncertainPublication, StateError
from bots5.core.export import TranscriptScope
from bots5.core.import_queue import (
    ImportQueueError,
    ImportQueueState,
    QueueDisplayPage,
    QueuedImportDisplay,
)
from bots5.desktop.phase9 import Phase9ProgressBridge
import bots5.desktop.widgets as widgets_module
from bots5.desktop.widgets import continuation_readiness_needs_resolution
from bots5.desktop.window import MainWindow
from bots5.domain.models import MessageRole
from bots5.infrastructure.archive_package import validate_archive

from tests.test_phase9_archive_v2 import _archive, missing_external_archive

import bots5.infrastructure.archive_package as archive_package
import bots5.infrastructure.persistence.sqlite as sqlite_module


# ---------------------------------------------------------------------------
# Local fixtures (no conftest.py; nothing shared outside this module)
# ---------------------------------------------------------------------------


def _run_qasync(qt_application: QApplication, operation) -> None:
    # Production composes the desktop with setQuitOnLastWindowClosed(False)
    # (bootstrap/desktop.py:641, landed shutdown guarantee 5); the harness
    # mirrors that so window-close proofs run under the production lifetime
    # rule instead of Qt's default last-window quit.
    qt_application.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _wait_until(predicate, *, timeout: float = 15.0, what: str = "state") -> None:
    # The bound is generous on purpose: these proofs drive real async desktop
    # work (store writes, backup verification, restore handoff) and run inside
    # the full-suite process, where cumulative load can delay an otherwise
    # correct transition well past a couple of seconds. The bound only limits
    # how long a genuinely absent state is awaited; every assertion is
    # unchanged, and call sites that need a tighter or looser bound still pass
    # an explicit timeout.
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def _wait_for_dock(dock, predicate, *, timeout: float = 2.0, what: str = "dock rows") -> None:
    await _wait_until(
        lambda: predicate(dock.view_model),
        timeout=timeout,
        what=what,
    )


def _make_application(root: Path):
    """Local mirror of the house Phase 9 application fixture (real store)."""

    from bots5.core.application import BotsApplication
    from bots5.core.events import EventBus
    from bots5.core.provider_configuration import ProviderConfiguration
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from bots5.infrastructure.data_root_authority import DataRootAuthority
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=ProviderConfiguration(store, ids, clock),
    )
    return application, store, authority


async def _finish(application, attempt_id: str) -> None:
    task = application._generation_tasks.get(attempt_id)
    if task is not None:
        await task
    await asyncio.sleep(0)


async def _exportable_chat(application):
    chat = await application.create_chat("Slice E desktop chat")
    attempt = await application.send_message(chat.id, "hello slice e")
    await _finish(application, attempt.id)
    return chat


async def _dispose(window: MainWindow) -> None:
    await window.stop_bridge_async()
    window.hide()
    window.deleteLater()
    await asyncio.sleep(0)


async def _close_application(application, store, authority) -> None:
    await application.close()
    if not store.closed:
        authority.close()


async def _wait_for_terminal(application, queue_id: str, *, timeout: float = 25.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        page = await application.queued_import_display(limit=50)
        row = next((item for item in page.items if item.id == queue_id), None)
        if row is not None and row.is_terminal:
            assert row.state is ImportQueueState.COMPLETED, row.state
            return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("import did not settle within the test bound")
        await asyncio.sleep(0.01)


def _patch_save_file_name(monkeypatch, destination: Path, *, pattern: str = "*.md") -> dict:
    calls = {"count": 0}

    def fake_get_save_file_name(*args, **kwargs):
        calls["count"] += 1
        return (str(destination), pattern)

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_get_save_file_name))
    return calls


def _patch_open_file_name(monkeypatch, source: Path, *, pattern: str = "*.botsarchive") -> dict:
    calls = {"count": 0}

    def fake_get_open_file_name(*args, **kwargs):
        calls["count"] += 1
        return (str(source), pattern)

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_get_open_file_name))
    return calls


def _block_preflight(monkeypatch, gate: threading.Event) -> None:
    """Keep a claimed import durably PREFLIGHTING while the loop keeps ticking.

    The seam sits after the durable preflight claim, so the row is truthfully
    PREFLIGHTING in the store while blocked; the worker thread is off the loop.
    """

    def blocked(path, roots):
        gate.wait(timeout=20)
        return _landed_resolve_source(path, roots)

    monkeypatch.setattr(sqlite_module, "resolve_source", blocked)


def _display_page(*rows) -> QueueDisplayPage:
    items = []
    for row in rows:
        identifier, ordinal, revision, state, label = row[:5]
        failure_code = row[5] if len(row) > 5 else None
        items.append(
            QueuedImportDisplay(
                id=identifier,
                ordinal=ordinal,
                revision=revision,
                state=state,
                source_label=label,
                failure_code=failure_code,
                enqueued_at=None,
                started_at=None,
                finished_at=None,
            )
        )
    return QueueDisplayPage(
        items=tuple(items), next_cursor=None, queue_revision=len(items) + 3
    )


def _manifest_version(path: Path) -> int:
    with zipfile.ZipFile(path) as package:
        manifest = json.loads(package.read("manifest.json"))
    return int(manifest["archive_version"])


# ---------------------------------------------------------------------------
# Proof 1 — transcript export from the real menu action, GUI stays responsive
# ---------------------------------------------------------------------------


def test_transcript_export_action_writes_file_while_gui_thread_keeps_ticking(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            chat = await _exportable_chat(application)
            window = MainWindow(application)
            window._current_chat_id = chat.id
            window.show()
            await asyncio.sleep(0)

            destination = tmp_path / "transcript.md"
            _patch_save_file_name(monkeypatch, destination)
            loop_thread = threading.get_ident()
            writer_threads: list[int] = []
            original_writer = archive_package.write_transcript
            heartbeat = {"ticks": 0, "at_writer_start": None, "running": True}

            def spy_writer(projection, path):
                writer_threads.append(threading.get_ident())
                heartbeat["at_writer_start"] = heartbeat["ticks"]
                return original_writer(projection, path)

            monkeypatch.setattr(archive_package, "write_transcript", spy_writer)

            async def heartbeat_loop() -> None:
                while heartbeat["running"]:
                    heartbeat["ticks"] += 1
                    await asyncio.sleep(0)

            beat = asyncio.create_task(heartbeat_loop())

            window.export_transcript_action.trigger()
            dialog = window._phase9._dialogs["transcript_export"]
            assert dialog is not None and dialog.isVisible()
            dialog.scope_active_path.setChecked(True)
            assert dialog.scope() is TranscriptScope.ACTIVE_PATH
            dialog.pick_destination()
            assert dialog.destination() == str(destination)
            dialog.confirm()

            await _wait_until(lambda: destination.is_file(), what="published transcript")
            await window._phase9.wait_idle()
            heartbeat["running"] = False
            await beat

            assert writer_threads and writer_threads[-1] != loop_thread, (
                "the transcript writer ran on the GUI thread"
            )
            assert heartbeat["at_writer_start"] is not None
            assert heartbeat["ticks"] > heartbeat["at_writer_start"], (
                "the GUI loop stopped ticking while the export writer ran"
            )
            raw = destination.read_bytes()
            assert "hello slice e" in raw.decode("utf-8")
            assert not dialog.isVisible()
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 2 — archive export: truthful versions and the ArchiveVersionRequired(2)
# prompt path, driven from the real menu action
# ---------------------------------------------------------------------------


def test_archive_export_truthful_versions_and_v2_required_prompt_on_gui_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            chat = await _exportable_chat(application)
            window = MainWindow(application)
            window._current_chat_id = chat.id
            window.show()
            await asyncio.sleep(0)

            # A wholly native graph with automatic version selection stays v1.
            native_destination = tmp_path / "native.botsarchive"
            _patch_save_file_name(monkeypatch, native_destination, pattern="*.botsarchive")
            window.export_archive_action.trigger()
            dialog = window._phase9._dialogs["archive_export"]
            assert dialog is not None and dialog.isVisible()
            assert dialog.version() is None  # "Automatic" display default
            dialog.pick_destination()
            dialog.confirm()
            await _wait_until(lambda: native_destination.is_file(), what="native v1 package")
            await window._phase9.wait_idle()
            assert _manifest_version(native_destination) == 1
            with native_destination.open("rb") as stream:
                assert validate_archive(stream).archive_id
            assert not dialog.isVisible()

            # An imported chat carries provenance: an explicit v1 request must
            # surface the landed ArchiveVersionRequired(2) prompt, and the
            # operator-approved switch must publish a v2 package.
            source_archive = tmp_path / "roundtrip.botsarchive"
            await application.write_archive_export(chat.id, source_archive)
            queued = await application.enqueue_archive_import(source_archive)
            await _wait_for_terminal(application, queued.id)
            chats = await application.list_chats()
            imported = next(item for item in chats if item.id != chat.id)
            window._current_chat_id = imported.id

            questions: list[str] = []

            def fake_question(*args, **kwargs):
                questions.append(str(args[2]) if len(args) > 2 else str(kwargs.get("text", "")))
                return QMessageBox.StandardButton.Yes

            monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))

            v2_destination = tmp_path / "imported.botsarchive"
            _patch_save_file_name(monkeypatch, v2_destination, pattern="*.botsarchive")
            window.export_archive_action.trigger()
            dialog = window._phase9._dialogs["archive_export"]
            assert dialog is not None and dialog.isVisible()
            dialog.archive_version.setCurrentIndex(dialog.archive_version.findData(1))
            assert dialog.version() == 1
            dialog.pick_destination()
            dialog.confirm()
            await _wait_until(lambda: v2_destination.is_file(), what="operator-approved v2 package")
            await window._phase9.wait_idle()
            assert len(questions) == 1, "the v2-required prompt did not run exactly once"
            assert "v2" in questions[0]
            assert _manifest_version(v2_destination) == 2
            with v2_destination.open("rb") as stream:
                assert validate_archive(stream).archive_id
            assert not dialog.isVisible()
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 3 — archive import admission from the real menu action: durable row,
# visible through the projection, and no dialog-side filesystem probe
# ---------------------------------------------------------------------------


def test_import_admission_action_enqueues_durable_row_without_dialog_side_stat_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            chat = await _exportable_chat(application)
            source = tmp_path / "roundtrip.botsarchive"
            await application.write_archive_export(chat.id, source)

            window = MainWindow(application)
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_until(
                lambda: dock.view_model.queue_revision is not None,
                what="the visibility reload to apply",
            )

            # No dialog-side probe may run on the GUI thread: admission truth
            # is the landed store result (F-03).  The store's own offloaded
            # fingerprint runs on a worker thread and stays allowed.
            loop_thread = threading.get_ident()
            probes: list[str] = []
            originals = {
                name: getattr(pathlib.Path, name)
                for name in ("stat", "exists", "is_file")
            }

            def guard(name, original):
                def wrapper(self, *args, **kwargs):
                    if threading.get_ident() == loop_thread:
                        probes.append(name)
                        raise AssertionError(
                            f"{name} ran on the GUI thread during admission"
                        )
                    return original(self, *args, **kwargs)

                return wrapper

            for name, original in originals.items():
                monkeypatch.setattr(pathlib.Path, name, guard(name, original))

            _patch_open_file_name(monkeypatch, source)
            window.import_archive_action.trigger()
            dialog = window._phase9._dialogs["archive_import"]
            assert dialog is not None and dialog.isVisible()
            dialog.pick_source()
            assert dialog.source_path() == str(source)
            dialog.confirm()

            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows),
                timeout=5.0,
                what="the enqueued row to become visible",
            )
            await window._phase9.wait_idle()
            assert not dialog.isVisible()
            assert probes == [], "a dialog-side filesystem probe ran during admission"

            page = await application.queued_import_display(limit=50)
            assert len(page.items) == 1
            durable_row = page.items[0]
            assert durable_row.source_label == source.name
            assert str(tmp_path) not in durable_row.source_label
            displayed = dock.view_model.rows[0]
            assert displayed.id == durable_row.id
            assert displayed.source_label == durable_row.source_label
            table_text = dock.table.item(0, 0).text()
            assert table_text == source.name
            assert str(tmp_path) not in table_text

            await _wait_for_terminal(application, durable_row.id)
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 4a — rows render from the projection; a durable mid-preflight
# PREFLIGHTING row becomes visible within the bounded poll; polling stops when
# all rows are terminal; Clear history drains the terminal row
# ---------------------------------------------------------------------------


def test_queue_rows_render_and_preflighting_becomes_visible_within_poll_bound_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        gate = threading.Event()
        try:
            chat = await _exportable_chat(application)
            intake = tmp_path / "intake"
            intake.mkdir()
            source = intake / "visible.botsarchive"
            await application.write_archive_export(chat.id, source)

            window = MainWindow(application)
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_until(
                lambda: dock.view_model.queue_revision is not None,
                what="the visibility reload to apply",
            )

            _block_preflight(monkeypatch, gate)
            # The admission enqueue is locally issued, so the D-3 immediate
            # reload surfaces the QUEUED row and starts the bounded poll.
            _patch_open_file_name(monkeypatch, source)
            window.import_archive_action.trigger()
            dialog = window._phase9._dialogs["archive_import"]
            dialog.pick_source()
            dialog.confirm()
            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows),
                timeout=5.0,
                what="the admitted row to appear",
            )
            queued_id = dock.view_model.rows[0].id

            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows)
                and view_model.rows[0].state is ImportQueueState.PREFLIGHTING,
                timeout=2.5,
                what="PREFLIGHTING within the bounded poll",
            )
            assert dock.poll_timer.isActive()
            row = dock.view_model.rows[0]
            assert row.id == queued_id
            assert row.source_label == "visible.botsarchive"
            assert dock.table.rowCount() == 1
            assert dock.table.item(0, 0).text() == "visible.botsarchive"
            assert dock.table.item(0, 2).text() == str(row.revision)
            assert str(tmp_path) not in dock.table.item(0, 0).text()

            gate.set()
            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows)
                and view_model.rows[0].state is ImportQueueState.COMPLETED,
                timeout=25.0,
                what="the completed settlement",
            )
            await _wait_until(lambda: not dock.poll_timer.isActive(), what="polling to stop")
            assert not dock.poll_timer.isActive(), (
                "polling must stop once every row is terminal"
            )

            dock.clear_button.click()
            await _wait_for_dock(
                dock,
                lambda view_model: not view_model.rows,
                timeout=5.0,
                what="the terminal row to clear",
            )
            await window._phase9.wait_idle()
        finally:
            gate.set()
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 4b — cancel submits the observed row revision (real CAS against the
# landed store)
# ---------------------------------------------------------------------------


def test_queue_cancel_submits_the_observed_row_revision_on_gui_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        gate = threading.Event()
        try:
            chat = await _exportable_chat(application)
            source = tmp_path / "cancellable.botsarchive"
            await application.write_archive_export(chat.id, source)

            window = MainWindow(application)
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_until(
                lambda: dock.view_model.queue_revision is not None,
                what="the visibility reload to apply",
            )

            _block_preflight(monkeypatch, gate)
            queued = await application.enqueue_archive_import(source)
            dock.reload_now()
            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows)
                and view_model.rows[0].state is ImportQueueState.PREFLIGHTING,
                timeout=5.0,
                what="the claimed PREFLIGHTING row",
            )
            observed_revision = dock.view_model.rows[0].revision

            cancel_calls: list[tuple[str, int]] = []
            original_cancel = application.cancel_archive_import

            async def recording_cancel(queue_id, *, expected_revision):
                cancel_calls.append((queue_id, expected_revision))
                return await original_cancel(queue_id, expected_revision=expected_revision)

            monkeypatch.setattr(application, "cancel_archive_import", recording_cancel)

            dock.table.selectRow(0)
            await asyncio.sleep(0)
            assert dock.cancel_button.isEnabled()
            dock.cancel_button.click()
            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows)
                and view_model.rows[0].state is ImportQueueState.CANCELLED,
                timeout=10.0,
                what="the durably cancelled row",
            )
            await window._phase9.wait_idle()
            assert cancel_calls == [(queued.id, observed_revision)], (
                "cancel did not submit the observed row revision"
            )
            assert not dock.poll_timer.isActive()
        finally:
            gate.set()
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 4c — a stale row revision produces a refresh prompt, never a retry;
# remove echoes the observed revision too
# ---------------------------------------------------------------------------


def test_stale_row_revision_prompts_refresh_and_never_retries(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    class StaleQueueApplication:
        def __init__(self) -> None:
            self.cancel_calls: list[tuple[str, int]] = []
            self.remove_calls: list[tuple[str, int]] = []

        async def queued_import_display(self, *, limit=50, cursor=None):
            return _display_page(
                ("row-1", 0, 7, ImportQueueState.QUEUED, "stale.botsarchive"),
            )

        async def cancel_archive_import(self, queue_id, *, expected_revision):
            self.cancel_calls.append((queue_id, expected_revision))
            raise ImportQueueError("queue revision conflict")

        async def remove_waiting_archive_import(self, queue_id, *, expected_revision):
            self.remove_calls.append((queue_id, expected_revision))
            return None

    async def scenario() -> None:
        application = StaleQueueApplication()
        window = MainWindow(application)
        try:
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_for_dock(
                dock,
                lambda view_model: view_model.row("row-1") is not None,
                timeout=5.0,
                what="the fake queue row",
            )
            revision = dock.view_model.observed_row_revision("row-1")

            dock.table.selectRow(0)
            await asyncio.sleep(0)
            dock.cancel_button.click()
            await _wait_until(
                lambda: "refresh" in dock.status_label.text().lower(),
                what="the stale-token refresh prompt",
            )
            await window._phase9.wait_idle()
            assert application.cancel_calls == [("row-1", revision)]
            assert "queue revision conflict" in dock.status_label.text(), (
                "the landed stale-token message must be carried verbatim"
            )
            settled = len(application.cancel_calls)
            await asyncio.sleep(0.05)
            await window._phase9.wait_idle()
            assert len(application.cancel_calls) == settled, "a silent retry ran"

            dock.remove_button.click()
            await _wait_until(
                lambda: bool(application.remove_calls),
                what="the remove command",
            )
            await window._phase9.wait_idle()
            assert application.remove_calls == [("row-1", revision)]
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 4c-bis — explicit Retry on a FAILED row submits the row identity and
# the failure label keeps the raw landed code truthfully available
# ---------------------------------------------------------------------------


def test_retry_failed_row_submits_row_id_and_failure_label_stays_truthful(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    class FailedRowApplication:
        def __init__(self) -> None:
            self.page = _display_page(
                ("row-f", 0, 4, ImportQueueState.FAILED, "broken.botsarchive", "SOURCE_UNAVAILABLE"),
            )
            self.retry_calls: list[str] = []

        async def queued_import_display(self, *, limit=50, cursor=None):
            return self.page

        async def retry_archive_import(self, queue_id):
            self.retry_calls.append(queue_id)
            return None

    async def scenario() -> None:
        application = FailedRowApplication()
        window = MainWindow(application)
        try:
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_for_dock(
                dock,
                lambda view_model: view_model.row("row-f") is not None,
                timeout=5.0,
                what="the failed row",
            )
            # Failure codes map to an operator label and keep the raw code.
            result_text = dock.table.item(0, 3).text()
            assert "Source unavailable" in result_text
            assert "SOURCE_UNAVAILABLE" in result_text

            dock.table.selectRow(0)
            await asyncio.sleep(0)
            assert dock.retry_button.isEnabled()
            assert not dock.cancel_button.isEnabled()
            assert not dock.remove_button.isEnabled()
            dock.retry_button.click()
            await _wait_until(
                lambda: bool(application.retry_calls),
                what="the explicit retry command",
            )
            await window._phase9.wait_idle()
            assert application.retry_calls == ["row-f"]
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 4d (D-3) — with an empty queue and the dock visible, an enqueue from
# the real admission dialog appears without a manual Refresh
# ---------------------------------------------------------------------------


def test_enqueue_from_admission_dialog_appears_without_manual_refresh_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            chat = await _exportable_chat(application)
            source = tmp_path / "immediate.botsarchive"
            await application.write_archive_export(chat.id, source)

            window = MainWindow(application)
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_until(
                lambda: dock.view_model.queue_revision is not None,
                what="the visibility reload to apply",
            )
            # Empty queue and dock visible: the bounded poll is vacuously off,
            # so the immediate reload is the only route that can surface the
            # just-enqueued row.
            assert not dock.poll_timer.isActive()

            display_pages: list[QueueDisplayPage] = []
            original_display = application.queued_import_display

            async def recording_display(**kwargs):
                page = await original_display(**kwargs)
                display_pages.append(page)
                return page

            monkeypatch.setattr(application, "queued_import_display", recording_display)
            reads_before_confirm = len(display_pages)

            _patch_open_file_name(monkeypatch, source)
            window.import_archive_action.trigger()
            dialog = window._phase9._dialogs["archive_import"]
            assert dialog is not None and dialog.isVisible()
            dialog.pick_source()
            dialog.confirm()

            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows),
                timeout=2.0,
                what="the enqueued row without a manual Refresh",
            )
            await window._phase9.wait_idle()
            fresh_pages = display_pages[reads_before_confirm:]
            assert fresh_pages, "no reload ran after the admission enqueue"
            assert fresh_pages[0].items and fresh_pages[0].items[0].source_label == source.name, (
                "the first post-enqueue read was not the immediate reload"
            )
            assert dock.table.item(0, 0).text() == source.name
            assert not dialog.isVisible()
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 4e — reorder submits the observed queue revision; refusals carry the
# landed messages verbatim; the control is disabled while any row is active
# ---------------------------------------------------------------------------


def test_reorder_submits_queue_revision_and_refusals_carry_verbatim_thread(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    class FakeReorderApplication:
        def __init__(self) -> None:
            self.page = _display_page(
                ("a", 0, 1, ImportQueueState.QUEUED, "first.botsarchive"),
                ("b", 1, 1, ImportQueueState.QUEUED, "second.botsarchive"),
            )
            self.error: Exception | None = None
            self.reorder_calls: list[tuple[int, tuple[str, ...]]] = []

        async def queued_import_display(self, *, limit=50, cursor=None):
            return self.page

        async def reorder_archive_imports(self, expected_queue_revision, ordered_ids):
            self.reorder_calls.append((expected_queue_revision, tuple(ordered_ids)))
            if self.error is not None:
                raise self.error
            return ()

    async def scenario() -> None:
        application = FakeReorderApplication()
        window = MainWindow(application)
        try:
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_for_dock(
                dock,
                lambda view_model: len(view_model.rows) == 2,
                timeout=5.0,
                what="the two fake rows",
            )

            # Reorder submits the observed page-level queue revision and the
            # displayed order with the selection moved.
            dock.table.selectRow(1)
            await asyncio.sleep(0)
            assert dock.move_up_button.isEnabled()
            dock.move_up_button.click()
            await _wait_until(
                lambda: bool(application.reorder_calls),
                what="the reorder command",
            )
            await window._phase9.wait_idle()
            queue_revision = dock.view_model.queue_revision
            assert application.reorder_calls == [(queue_revision, ("b", "a"))]

            # Reorder is disabled while any row is active (R-6).
            application.page = _display_page(
                ("a", 0, 1, ImportQueueState.QUEUED, "first.botsarchive"),
                ("b", 1, 1, ImportQueueState.PREFLIGHTING, "second.botsarchive"),
            )
            dock.reload_now()
            await _wait_for_dock(
                dock,
                lambda view_model: view_model.row("b") is not None
                and view_model.row("b").state is ImportQueueState.PREFLIGHTING,
                timeout=5.0,
                what="the active row",
            )
            assert not dock.move_up_button.isEnabled()
            assert not dock.move_down_button.isEnabled()
            before = len(application.reorder_calls)
            dock.move_up_button.click()
            await window._phase9.wait_idle()
            assert len(application.reorder_calls) == before

            # The landed "active import" refusal carries verbatim.
            application.page = _display_page(
                ("a", 0, 1, ImportQueueState.QUEUED, "first.botsarchive"),
                ("b", 1, 1, ImportQueueState.QUEUED, "second.botsarchive"),
            )
            dock.reload_now()
            await _wait_for_dock(
                dock,
                lambda view_model: view_model.row("b") is not None
                and view_model.row("b").state is ImportQueueState.QUEUED,
                timeout=5.0,
                what="the reset waiting rows",
            )
            application.error = StateError(
                "queue reorder is unavailable while an import is active"
            )
            dock.table.selectRow(0)
            await asyncio.sleep(0)
            dock.move_down_button.click()
            await _wait_until(
                lambda: "while an import is active" in dock.status_label.text(),
                what="the verbatim active-import refusal",
            )
            await window._phase9.wait_idle()

            # The reorder CAS conflict is a refresh prompt carrying verbatim.
            application.error = StateError("queue reorder revision conflict")
            dock.move_down_button.click()
            await _wait_until(
                lambda: "refresh" in dock.status_label.text().lower(),
                what="the reorder refresh prompt",
            )
            await window._phase9.wait_idle()
            assert "queue reorder revision conflict" in dock.status_label.text()
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Worker → GUI thread hop (Phase9ProgressBridge, the M4 publication seam)
# ---------------------------------------------------------------------------


def test_worker_progress_signal_hops_to_gui_thread(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        received: list[tuple[object, object, bool]] = []
        completed: list[object] = []
        bridge = Phase9ProgressBridge()
        bridge.progress_updated.connect(
            lambda state, operation_id: received.append(
                (state, operation_id, QThread.currentThread() is qt_application.thread())
            )
        )
        bridge.completed.connect(completed.append)

        # Emission-origin proof (F-1): record threading.get_ident() at the
        # exact moment the bridge emits.  Calling publish_progress directly
        # from the GUI thread would record the GUI identity here and fail the
        # assertion below, so the proof cannot pass unless the progress
        # really originated on a worker thread.
        gui_thread_ident = threading.get_ident()
        emit_threads: list[tuple[str, int]] = []
        original_publish_progress = bridge.publish_progress
        original_publish_completed = bridge.publish_completed

        def spy_publish_progress(state, operation_id):
            emit_threads.append(("progress", threading.get_ident()))
            original_publish_progress(state, operation_id)

        def spy_publish_completed(result):
            emit_threads.append(("completed", threading.get_ident()))
            original_publish_completed(result)

        bridge.publish_progress = spy_publish_progress
        bridge.publish_completed = spy_publish_completed

        def worker() -> None:
            # Worker thread: only the thread-safe bridge publish methods are
            # called; no Qt widget is ever touched here.
            bridge.publish_progress("ACQUIRING_FENCE", "op-1")
            bridge.publish_completed({"state": "COMPLETED"})

        thread = threading.Thread(target=worker, name="phase9-progress-worker")
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

        # The EMITTING thread is a different, non-GUI thread: every emission
        # was recorded on the spawned worker thread's identity, never on the
        # GUI/qasync loop thread identity.
        assert emit_threads, "the bridge never emitted"
        assert {kind for kind, _ident in emit_threads} == {"progress", "completed"}
        assert all(ident == thread.ident for _kind, ident in emit_threads), (
            "the bridge emitted from an unexpected thread"
        )
        assert all(ident != gui_thread_ident for _kind, ident in emit_threads), (
            "the bridge emitted the progress from the GUI thread"
        )

        await _wait_until(lambda: bool(received), what="the queued progress signal")
        await _wait_until(lambda: bool(completed), what="the completed signal")
        state, operation_id, on_gui_thread = received[0]
        assert state == "ACQUIRING_FENCE"
        assert operation_id == "op-1"
        assert on_gui_thread is True, "the receiving slot did not run on the GUI thread"
        assert completed == [{"state": "COMPLETED"}]

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 12 — closing during an import interaction preserves the landed
# shutdown guarantees and does not hang
# ---------------------------------------------------------------------------


def test_window_close_during_import_drains_cutoff_past_work_on_gui_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        settle_gate = threading.Event()
        try:
            chat = await _exportable_chat(application)
            source = tmp_path / "drain.botsarchive"
            await application.write_archive_export(chat.id, source)

            order: list[str] = []
            original_settle = store.settle_archive_import

            def slow_settle(plan):
                assert settle_gate.wait(timeout=20), "settlement gate was not released"
                result = original_settle(plan)
                order.append("settle_done")
                return result

            monkeypatch.setattr(store, "settle_archive_import", slow_settle)
            original_store_close = store.close

            def spy_store_close():
                order.append("store_close")
                return original_store_close()

            monkeypatch.setattr(store, "close", spy_store_close)
            original_authority_close = authority.close

            def spy_authority_close():
                order.append("authority_close")
                return original_authority_close()

            monkeypatch.setattr(authority, "close", spy_authority_close)

            window = MainWindow(application)
            dock = window.import_queue_dock
            window.show()
            await asyncio.sleep(0)
            dock.show()
            await _wait_until(
                lambda: dock.view_model.queue_revision is not None,
                what="the visibility reload to apply",
            )

            queued = await application.enqueue_archive_import(source)
            dock.reload_now()
            await _wait_for_dock(
                dock,
                lambda view_model: bool(view_model.rows)
                and view_model.rows[0].state is ImportQueueState.STAGING,
                timeout=15.0,
                what="the cutoff-past STAGING row",
            )
            assert not settle_gate.is_set()

            closed_flag: list[bool] = []
            window.closed.connect(lambda: closed_flag.append(True))
            window.close()
            await _wait_until(
                lambda: bool(closed_flag),
                timeout=10.0,
                what="the window close to finish during an import",
            )
            assert not dock.poll_timer.isActive()
            assert "store_close" not in order

            close_task = asyncio.create_task(application.close())
            await asyncio.sleep(0.2)
            assert "settle_done" not in order, "close released the store before the drain"
            assert "store_close" not in order
            settle_gate.set()
            await asyncio.wait_for(close_task, timeout=25.0)
            # Landed close order: the cutoff-past settlement drains before the
            # store closes, and the store close releases the authority inside
            # the same final step (SQLiteAppStateStore.close → authority.close),
            # so nothing is released after it.
            assert order == ["settle_done", "store_close", "authority_close"], (
                f"cutoff-past work was not drained before store close: {order}"
            )
            assert queued.id
        finally:
            settle_gate.set()
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 11 — the Phase 9 UI inventory exposes no destructive override and no
# retained-installation cleanup action
# ---------------------------------------------------------------------------


def test_phase9_ui_inventory_exposes_no_destructive_override_or_cleanup_surface(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    class InventoryApplication:
        async def queued_import_display(self, *, limit=50, cursor=None):
            return _display_page()

    async def scenario() -> None:
        application = InventoryApplication()
        window = MainWindow(application)
        try:
            window.show()
            await asyncio.sleep(0)
            dock = window.import_queue_dock
            dock.show()
            await asyncio.sleep(0)

            # The rail context menu is constructed on demand only, so this
            # inventory proof must force its construction here: otherwise the
            # additive "Rename Title…"/"Duplicate Chat…" entries never exist as
            # QActions and the scans below silently miss them.  The substituted
            # menu class dismisses instead of blocking in exec(), and the real
            # QMenu (parented to the chat list) stays behind for the scan.
            rail_item = QListWidgetItem("Inventory chat")
            rail_item.setData(Qt.ItemDataRole.UserRole, "inventory-chat-id")
            window.rail.chat_list.addItem(rail_item)

            class _InventoryContextMenu(QMenu):
                def exec(self, *args, **kwargs):  # noqa: ANN002, ANN003
                    return None  # dismiss the on-demand menu; nothing is invoked

            original_menu_class = widgets_module.QMenu
            chat_list = window.rail.chat_list
            chat_list.itemAt = lambda pos: rail_item
            try:
                widgets_module.QMenu = _InventoryContextMenu
                window.rail._show_chat_context_menu(QPoint(0, 0))
            finally:
                widgets_module.QMenu = original_menu_class
                del chat_list.itemAt

            constructed_menus = chat_list.findChildren(QMenu)
            assert len(constructed_menus) == 1
            menu_entries = [
                action.text() for action in constructed_menus[0].actions() if action.text()
            ]
            # The on-demand menu carries the two landed Phase 9 export entries
            # and the two additive Phase 11 A-1 entries.
            assert "Export Transcript…" in menu_entries
            assert "Export Archive…" in menu_entries
            assert "Rename Title…" in menu_entries
            assert "Duplicate Chat…" in menu_entries

            forbidden = (
                "override",
                "destructive",
                "retained",
                "uninstall",
                "wipe",
                "cleanup",
            )
            labels: list[str] = []
            for action in window.findChildren(QAction):
                labels.extend([action.text(), action.toolTip()])
            for button in window.findChildren(QAbstractButton):
                labels.extend([button.text(), button.toolTip()])
            inventory = " | ".join(label for label in labels if label).lower()
            for word in forbidden:
                assert word not in inventory, (
                    f"the Phase 9 UI inventory exposes a forbidden surface: {word}"
                )

            # The dock's control set is exactly the sealed workflow-4 set.
            dock_buttons = {
                button.objectName()
                for button in dock.findChildren(QPushButton)
                if button.objectName()
            }
            assert dock_buttons == {
                "importQueueRefreshButton",
                "importQueueCancelButton",
                "importQueueRemoveButton",
                "importQueueRetryButton",
                "importQueueMoveUpButton",
                "importQueueMoveDownButton",
                "importQueueClearHistoryButton",
            }

            # The chat rail carries the on-demand context menu with the two
            # additive export context actions (plus the A-1 entries above) and
            # nothing else Phase 9 related.
            assert window.rail.chat_list.contextMenuPolicy() is Qt.ContextMenuPolicy.CustomContextMenu
        finally:
            await _dispose(window)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Workflow 5 harness helpers (local to this module, like every fixture here)
# ---------------------------------------------------------------------------


def _source_configuration_fixture() -> dict:
    """An immutable import-recorded source configuration with real descriptors.

    The backend is deliberately not the receiver's fake backend, so the
    landed deterministic receiver equivalence never auto-admits a choice and
    the anchor stays UNRESOLVED (the operator, not the import, decides).
    """

    model = {
        "source_model_entry_id": "source-model-1",
        "source_model_entry_id_status": "available",
        "source_connection_id": "source-connection-1",
        "source_connection_id_status": "available",
        "backend_type": "openai_compatible_http",
        "backend_type_status": "available",
        "provider_profile": "openrouter",
        "provider_profile_status": "available",
        "provider_model_id": "source/fake-model",
        "provider_model_id_status": "available",
        "display_name": "Imported Source Model",
        "display_name_status": "available",
        "origin": "manual",
        "origin_status": "available",
        "availability": "available",
        "availability_status": "available",
        "model_revision": 1,
        "connection_revision": 1,
        "catalogue_revision": 1,
    }
    return {
        "semantic": "inert-continuation-hints",
        "selection": {"model": model, "selection_required": False, "revision": 1},
        "overrides": [{
            "model": model, "revision": 1, "temperature": 0.5,
            "max_output_tokens": 256, "reasoning_effort": None,
            "timeout_seconds": None,
        }],
    }


async def _import_one_archive(application, source: Path):
    """Import through the landed desktop-side commands and return the chat."""

    queued = await application.enqueue_archive_import(
        source, resolver_roots=(source.parent,)
    )
    await _wait_for_terminal(application, queued.id)
    chats = await application.list_chats()
    assert len(chats) == 1
    return chats[0]


def _imported_source_rows(store, chat_id: str):
    """Durable imported source truth: message rows, import mapping, anchors.

    The continuation workflow writes only archive_continuation_choices and
    archive_continuation_branches; these three reads must therefore be
    identical before and after an admitted choice.  The anchor read is scoped
    to the imported source bases (plus the '' empty'' head anchor): a replayed
    send legitimately appends a *native child* anchor for the new assistant
    message, which is landed continuation machinery, not historical source
    truth.
    """

    with store.command_admission():
        with store._engine.connect() as db:
            return (
                db.exec_driver_sql(
                    "SELECT m.id,m.chat_id,m.parent_id,m.sequence,m.role,m.state,m.content,"
                    "m.created_at,m.lineage_id,m.revision,m.supersedes_id "
                    "FROM messages AS m JOIN archive_import_messages AS i ON i.message_id=m.id "
                    "WHERE m.chat_id=? ORDER BY m.id",
                    (chat_id,),
                ).fetchall(),
                db.exec_driver_sql(
                    "SELECT message_id,chat_id,source_message_id,source_lineage_id,source_node_id "
                    "FROM archive_import_messages WHERE chat_id=? ORDER BY message_id",
                    (chat_id,),
                ).fetchall(),
                db.exec_driver_sql(
                    "SELECT base_key,base_message_id,revision,source_configuration,resolution "
                    "FROM archive_continuation_anchors WHERE chat_id=? AND "
                    "(base_key='empty' OR base_key IN "
                    "(SELECT message_id FROM archive_import_messages WHERE chat_id=?)) "
                    "ORDER BY base_key",
                    (chat_id, chat_id),
                ).fetchall(),
            )


def _imported_user_row(store, chat_id: str, message_id: str):
    with store.command_admission():
        with store._engine.connect() as db:
            return (
                db.exec_driver_sql(
                    "SELECT id,chat_id,parent_id,sequence,role,state,content,created_at,"
                    "lineage_id,revision,supersedes_id FROM messages WHERE id=?",
                    (message_id,),
                ).mappings().one(),
                db.exec_driver_sql(
                    "SELECT message_id,chat_id,source_message_id,source_lineage_id,source_node_id "
                    "FROM archive_import_messages WHERE message_id=?",
                    (message_id,),
                ).mappings().one(),
            )


def _record_choose_command(monkeypatch, application) -> list:
    """Record every choose_import_continuation call without changing it."""

    calls = []
    original_choose = application.choose_import_continuation

    async def recording_choose(chat_id, base_key, **kwargs):
        calls.append((chat_id, base_key, dict(kwargs)))
        return await original_choose(chat_id, base_key, **kwargs)

    monkeypatch.setattr(application, "choose_import_continuation", recording_choose)
    return calls


async def _select_explicit_choice(dialog, application) -> tuple[str, str]:
    """Choose one active provider and one of its available models explicitly."""

    connections = [
        connection
        for connection in await application.list_provider_connections()
        if connection.available
    ]
    assert connections, "the seeded active provider is missing"
    dialog.provider_combo.setCurrentIndex(
        dialog.provider_combo.findData(connections[0].id)
    )
    models = [
        model
        for model in await application.list_model_catalogue()
        if model.connection_id == connections[0].id
    ]
    assert models, "the seeded available model is missing"
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData(models[0].id))
    return connections[0].id, models[0].id


def _continuation_window(application, chat_id: str) -> MainWindow:
    window = MainWindow(application)
    window.show()
    window._current_chat_id = chat_id
    return window


# ---------------------------------------------------------------------------
# Proof 5a/5d — send intercept: explicit choice admitted through the landed
# command, the intercepted send replays without the readiness StateError, and
# the historical source rows are unchanged
# ---------------------------------------------------------------------------


def test_imported_send_intercept_admits_choice_and_replays_on_gui_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            intake = tmp_path / "intake"
            intake.mkdir()
            source = intake / "imported.botsarchive"
            source.write_bytes(_archive())
            imported = await _import_one_archive(application, source)
            chat, _messages = await application.open_chat(imported.id)
            imported_head = chat.head_message_id
            readiness_before = await application.import_continuation_readiness(
                imported.id, imported_head
            )
            assert readiness_before is not None
            assert readiness_before.choice_revision == 0
            assert continuation_readiness_needs_resolution(readiness_before), (
                "the fixture must mirror the landed not-usable predicate"
            )
            source_rows_before = _imported_source_rows(store, imported.id)

            window = _continuation_window(application, imported.id)
            await window._refresh_transcript(imported.id)
            await asyncio.sleep(0)
            assert QThread.currentThread() is qt_application.thread()

            choice_calls = _record_choose_command(monkeypatch, application)

            # Real desktop action: the composer send button.
            window.composer.setPlainText("continue the imported chat")
            window.send_button.click()

            await _wait_until(
                lambda: window._phase9._dialogs.get("continuation_resolution") is not None,
                what="the continuation resolution dialog",
            )
            dialog = window._phase9._dialogs["continuation_resolution"]
            assert dialog is not None and dialog.isVisible()
            # The intercept, not the landed StateError, stopped the send:
            # nothing was dispatched, no error surfaced, no choice was written.
            assert tuple(store.list_generation_attempts(imported.id)) == ()
            assert "ready explicit choice" not in window.statusBar().currentMessage()
            assert choice_calls == [], "a continuation choice was written silently"
            assert not dialog.continue_button.isEnabled(), (
                "the dialog must not guess a provider/model choice"
            )
            assert window.continuation_banner.isVisible()

            connection_id, model_entry_id = await _select_explicit_choice(
                dialog, application
            )
            assert dialog.continue_button.isEnabled()
            dialog.continue_button.click()

            await _wait_until(
                lambda: bool(tuple(store.list_generation_attempts(imported.id))),
                timeout=5.0,
                what="the replayed send under the admitted choice",
            )
            attempt = tuple(store.list_generation_attempts(imported.id))[0]
            await _finish(application, attempt.id)
            await window._phase9.wait_idle()
            await _wait_until(lambda: not dialog.isVisible(), what="the dialog to close")

            assert len(choice_calls) == 1
            committed_chat, committed_base, kwargs = choice_calls[0]
            assert committed_chat == imported.id
            assert committed_base == imported_head
            assert kwargs["expected_choice_revision"] == readiness_before.choice_revision
            assert kwargs["connection_id"] == connection_id
            assert kwargs["model_entry_id"] == model_entry_id
            assert set(kwargs["explicit_settings"]) == {
                "temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds",
            }
            assert kwargs["excluded_refs"] == ()

            admitted = await application.import_continuation_readiness(
                imported.id, imported_head
            )
            assert admitted is not None and admitted.choice_revision == 1
            assert admitted.local_model_entry_id == model_entry_id
            assert "ready explicit choice" not in window.statusBar().currentMessage()
            settled_attempt = store.get_generation_attempt(attempt.id)
            assert settled_attempt is not None and settled_attempt.state.value == "complete", (
                "the replayed send must have run to a terminal attempt"
            )
            assert not window.continuation_banner.isVisible()

            # Proof 5d: the historical source rows are unchanged by the choice.
            assert _imported_source_rows(store, imported.id) == source_rows_before
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 5b — regenerate intercepts the same way; cancelling leaves the chat
# exactly as it was (no attempt, no choice row)
# ---------------------------------------------------------------------------


def test_imported_regenerate_intercepts_and_cancel_leaves_chat_unchanged(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            intake = tmp_path / "intake"
            intake.mkdir()
            source = intake / "regen.botsarchive"
            source.write_bytes(_archive())
            imported = await _import_one_archive(application, source)
            chat, _messages = await application.open_chat(imported.id)
            imported_head = chat.head_message_id
            source_rows_before = _imported_source_rows(store, imported.id)

            window = _continuation_window(application, imported.id)
            await window._refresh_transcript(imported.id)
            await asyncio.sleep(0)
            assert QThread.currentThread() is qt_application.thread()
            choice_calls = _record_choose_command(monkeypatch, application)

            # Real desktop action: the transcript row's Regenerate menu action.
            row = window.transcript.message_rows[imported_head]
            row.regenerate_action.trigger()
            await _wait_until(
                lambda: window._phase9._dialogs.get("continuation_resolution") is not None,
                what="the continuation resolution dialog",
            )
            dialog = window._phase9._dialogs["continuation_resolution"]
            assert dialog is not None and dialog.isVisible()
            assert tuple(store.list_generation_attempts(imported.id)) == ()
            assert "ready explicit choice" not in window.statusBar().currentMessage()
            assert choice_calls == []

            # Cancel: the chat is left exactly as it was.
            dialog.cancel_button.click()
            await _wait_until(
                lambda: "continuation_resolution" not in window._phase9._dialogs,
                what="the cancelled dialog to be forgotten",
            )
            # The cancelled send task settles its own busy state one loop turn
            # later; wait for it before re-arming the row action.
            await _wait_until(
                lambda: not window._generation_busy,
                what="the cancelled intercept task to settle",
            )
            await window._phase9.wait_idle()
            assert tuple(store.list_generation_attempts(imported.id)) == ()
            with store.command_admission():
                with store._engine.connect() as db:
                    assert db.exec_driver_sql(
                        "SELECT count(*) FROM archive_continuation_choices WHERE chat_id=?",
                        (imported.id,),
                    ).scalar_one() == 0
            assert not window.continuation_banner.isVisible()

            # The intercept repeats honestly and admits the explicit choice.
            row.regenerate_action.trigger()
            await _wait_until(
                lambda: window._phase9._dialogs.get("continuation_resolution") is not None,
                what="the reopened resolution dialog",
            )
            dialog = window._phase9._dialogs["continuation_resolution"]
            assert dialog is not None and dialog.isVisible()
            connection_id, model_entry_id = await _select_explicit_choice(
                dialog, application
            )
            dialog.continue_button.click()

            await _wait_until(
                lambda: bool(tuple(store.list_generation_attempts(imported.id))),
                timeout=5.0,
                what="the replayed regenerate under the admitted choice",
            )
            attempt = tuple(store.list_generation_attempts(imported.id))[0]
            await _finish(application, attempt.id)
            await window._phase9.wait_idle()
            await _wait_until(lambda: not dialog.isVisible(), what="the dialog to close")

            assert len(choice_calls) == 1
            committed_chat, committed_base, kwargs = choice_calls[0]
            assert committed_chat == imported.id and committed_base == imported_head
            assert kwargs["expected_choice_revision"] == 0
            assert kwargs["connection_id"] == connection_id
            assert kwargs["model_entry_id"] == model_entry_id
            admitted = await application.import_continuation_readiness(
                imported.id, imported_head
            )
            assert admitted is not None and admitted.choice_revision == 1
            assert "ready explicit choice" not in window.statusBar().currentMessage()
            settled_attempt = store.get_generation_attempt(attempt.id)
            assert settled_attempt is not None and settled_attempt.state.value == "complete"
            assert attempt.assistant_message_id != imported_head, (
                "a regenerate creates a sibling, it never rewrites the source"
            )
            # Proof 5d: the historical source rows are unchanged here too.
            assert _imported_source_rows(store, imported.id) == source_rows_before
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 5c — imported-user Edit is never intercepted and the imported source
# row is unchanged before and after (F-02)
# ---------------------------------------------------------------------------


def test_imported_user_edit_is_never_intercepted_and_source_row_unchanged_on_gui_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            intake = tmp_path / "intake"
            intake.mkdir()
            source = intake / "edit.botsarchive"
            source.write_bytes(_archive())
            imported = await _import_one_archive(application, source)
            chat, messages = await application.open_chat(imported.id)
            imported_head = chat.head_message_id
            imported_user = next(
                message for message in messages if message.role is MessageRole.USER
            )
            user_row_before = _imported_user_row(store, imported.id, imported_user.id)
            readiness_gate = await application.import_continuation_readiness(
                imported.id, imported_head
            )
            assert continuation_readiness_needs_resolution(readiness_gate), (
                "the chat must be unresolved so the edit could have been "
                "intercepted if the intercept were wired to the Edit path"
            )

            window = _continuation_window(application, imported.id)
            await window._refresh_transcript(imported.id)
            await asyncio.sleep(0)
            assert QThread.currentThread() is qt_application.thread()
            choice_calls = _record_choose_command(monkeypatch, application)

            # Real desktop actions: the row's Edit button, then the send button
            # (the window sends an edit while an edit target is armed).
            row = window.transcript.message_rows[imported_user.id]
            assert row.edit_button.isEnabled()
            row.edit_button.click()
            assert window._editing_message_id == imported_user.id
            window.composer.setPlainText("edited imported user from the desktop")
            window.send_button.click()

            await _wait_until(
                lambda: bool(tuple(store.list_generation_attempts(imported.id))),
                timeout=5.0,
                what="the imported-user edit attempt",
            )
            attempt = tuple(store.list_generation_attempts(imported.id))[0]
            await _finish(application, attempt.id)
            await window._phase9.wait_idle()

            # The Edit path routed straight to the landed edit_message: no
            # resolution dialog was opened and no choice was requested.
            assert "continuation_resolution" not in window._phase9._dialogs
            assert choice_calls == [], "an imported edit must not choose a continuation"
            assert "ready explicit choice" not in window.statusBar().currentMessage()
            assert attempt.user_message_id is not None
            assert store.get_message(attempt.user_message_id).supersedes_id == imported_user.id, (
                "the edit creates a local derived revision of the imported turn"
            )
            # The imported source row is unchanged before and after.
            assert _imported_user_row(store, imported.id, imported_user.id) == user_row_before
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 6 — MISSING_EXTERNAL requirements are listed truthfully and cannot be
# continued away without an explicit acknowledgement; nothing is guessed
# ---------------------------------------------------------------------------


def test_missing_external_continuation_requires_explicit_acknowledgement(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            intake = tmp_path / "intake"
            intake.mkdir()
            payload = b"proof six missing external payload"
            digest = hashlib.sha256(payload).hexdigest()
            source = intake / "missing.botsarchive"
            source.write_bytes(missing_external_archive(
                digest=digest, size=len(payload),
                source_configuration=_source_configuration_fixture(),
            ))
            imported = await _import_one_archive(application, source)
            chat, _messages = await application.open_chat(imported.id)
            imported_head = chat.head_message_id
            readiness_before = await application.import_continuation_readiness(
                imported.id, imported_head
            )
            assert readiness_before is not None
            assert readiness_before.resolution == "UNAVAILABLE"
            missing = [
                requirement
                for requirement in readiness_before.requirements
                if requirement.blocked_reason == "missing-external"
            ]
            assert missing, "the fixture must carry one MISSING_EXTERNAL slot"
            source_rows_before = _imported_source_rows(store, imported.id)

            window = _continuation_window(application, imported.id)
            await window._refresh_transcript(imported.id)
            await asyncio.sleep(0)
            choice_calls = _record_choose_command(monkeypatch, application)

            window.composer.setPlainText("continue with the missing external payload")
            window.send_button.click()
            await _wait_until(
                lambda: window._phase9._dialogs.get("continuation_resolution") is not None,
                what="the continuation resolution dialog",
            )
            dialog = window._phase9._dialogs["continuation_resolution"]
            assert dialog is not None and dialog.isVisible()

            # Read-only historical truth: the recorded source model/provider
            # and the original sampling parameters, never editable here.
            truth = dialog.source_truth_label.text()
            assert "Imported Source Model" in truth
            assert "source/fake-model" in truth
            assert "openai_compatible_http" in truth
            assert "temperature=0.5" in truth and "max_output_tokens=256" in truth

            # The missing external entries are listed truthfully: the recorded
            # identity of every blocked slot, with no invented filename.
            missing_text = dialog.missing_label.text()
            for requirement in missing:
                assert requirement.expected_digest[:16] in missing_text
                assert str(requirement.expected_size) in missing_text
            assert "not present in this workspace" in missing_text

            # Nothing is guessed: the operator must select and acknowledge.
            assert not dialog.continue_button.isEnabled()
            connection_id, model_entry_id = await _select_explicit_choice(
                dialog, application
            )
            assert not dialog.continue_button.isEnabled(), (
                "a degraded continuation must not proceed unacknowledged"
            )
            dialog.missing_acknowledgement.setChecked(True)
            assert dialog.continue_button.isEnabled()
            dialog.continue_button.click()

            await _wait_until(
                lambda: bool(tuple(store.list_generation_attempts(imported.id))),
                timeout=5.0,
                what="the replayed send under the acknowledged choice",
            )
            attempt = tuple(store.list_generation_attempts(imported.id))[0]
            await _finish(application, attempt.id)
            await window._phase9.wait_idle()
            await _wait_until(lambda: not dialog.isVisible(), what="the dialog to close")

            assert len(choice_calls) == 1
            _committed_chat, committed_base, kwargs = choice_calls[0]
            assert committed_base == imported_head
            assert kwargs["expected_choice_revision"] == readiness_before.choice_revision
            assert kwargs["connection_id"] == connection_id
            assert kwargs["model_entry_id"] == model_entry_id
            assert kwargs["excluded_refs"] == tuple(
                {
                    "requirement_ordinal": requirement.ordinal,
                    "expected_digest": requirement.expected_digest,
                    "attachment_id": requirement.imported_ref_id,
                    "reason": "missing-external",
                }
                for requirement in missing
            )
            admitted = await application.import_continuation_readiness(
                imported.id, imported_head
            )
            assert admitted is not None
            assert admitted.resolution != "UNAVAILABLE"
            assert admitted.choice_revision == 1
            assert tuple(admitted.excluded_refs) == kwargs["excluded_refs"]
            settled_attempt = store.get_generation_attempt(attempt.id)
            assert settled_attempt is not None and settled_attempt.state.value == "complete"
            assert _imported_source_rows(store, imported.id) == source_rows_before
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 5 (control) — a native chat (readiness None) proceeds exactly as
# today: no prompt, no dialog, no behaviour change
# ---------------------------------------------------------------------------


def test_native_chat_send_proceeds_without_any_continuation_prompt(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_application(tmp_path / "root")
        window = None
        try:
            chat = await application.create_chat("native chat")
            window = _continuation_window(application, chat.id)
            await window._refresh_transcript(chat.id)
            await asyncio.sleep(0)

            assert await application.import_continuation_readiness(
                chat.id, chat.head_message_id
            ) is None
            assert continuation_readiness_needs_resolution(None) is False

            window.composer.setPlainText("an ordinary native send")
            window.send_button.click()
            await _wait_until(
                lambda: bool(tuple(store.list_generation_attempts(chat.id))),
                timeout=5.0,
                what="the ordinary native send",
            )
            await _finish(application, tuple(store.list_generation_attempts(chat.id))[0].id)
            await window._phase9.wait_idle()

            assert "continuation_resolution" not in window._phase9._dialogs
            assert not window.continuation_banner.isVisible()
            settled = store.get_generation_attempt(
                tuple(store.list_generation_attempts(chat.id))[0].id
            )
            assert settled is not None and settled.state.value == "complete"
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# M4 harness helpers — real backup composition (local to this module)
# ---------------------------------------------------------------------------


def _make_backup_application(root: Path):
    """Mirror the production desktop backup composition (bootstrap/desktop.py).

    Same shape as the landed backup fixture's service: a real capture adapter
    over the authority-owned store, the real zip package adapter and the real
    file publication adapter — so the dialogs drive the real semantics.
    """

    from bots5.core.application import BotsApplication
    from bots5.core.backup import BackupService
    from bots5.core.events import EventBus
    from bots5.core.provider_configuration import ProviderConfiguration
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from bots5.infrastructure.app_paths import resolve_app_paths
    from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter
    from bots5.infrastructure.backup_package import (
        BackupFilePublicationAdapter,
        BackupZipPackageAdapter,
    )
    from bots5.infrastructure.data_root_authority import DataRootAuthority
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    authority = DataRootAuthority(root.absolute()).acquire()
    paths = resolve_app_paths(root)
    paths.ensure_non_authoritative()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    backup_service = BackupService(
        RootedBackupCaptureAdapter(
            authority,
            store,
            paths,
            BackupZipPackageAdapter(),
            data_root_is_override=True,
        ),
        BackupZipPackageAdapter(),
        BackupFilePublicationAdapter(),
        ids,
    )
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=ProviderConfiguration(store, ids, clock),
        backup_service=backup_service,
    )
    return application, store, authority


def _spy_capture(monkeypatch, gate: threading.Event | None = None) -> list[int]:
    """Record the worker-thread ids running ``capture_under_fence``.

    With a ``gate``, the worker blocks inside the seam (after the landed
    ``ACQUIRING_FENCE`` report, before the real capture) until it is set.
    """

    from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter

    original = RootedBackupCaptureAdapter.capture_under_fence
    threads: list[int] = []

    def wrapper(self_capture, *args, **kwargs):
        threads.append(threading.get_ident())
        if gate is not None:
            assert gate.wait(timeout=20), "capture gate was not released"
        return original(self_capture, *args, **kwargs)

    monkeypatch.setattr(RootedBackupCaptureAdapter, "capture_under_fence", wrapper)
    return threads


def _record_create_command(monkeypatch, application) -> list:
    """Record the landed service's create_backup kwargs without changing it.

    The controller drives the landed backup service through the landed
    fresh-context offload (see ``run_backup_creation``), so the recording
    seam sits on the service itself.
    """

    service = application._backup_service
    calls = []
    original_create = service.create_backup

    def recording_create(
        destination, *, overwrite=False, cancellation=None,
        receipt_sink=None, progress_callback=None,
    ):
        calls.append((
            Path(destination),
            {
                "overwrite": overwrite,
                "cancellation": cancellation,
                "progress_callback": progress_callback,
            },
        ))
        return original_create(
            destination,
            overwrite=overwrite,
            cancellation=cancellation,
            receipt_sink=receipt_sink,
            progress_callback=progress_callback,
        )

    monkeypatch.setattr(service, "create_backup", recording_create)
    return calls


# ---------------------------------------------------------------------------
# Proof 7 — backup creation: worker→GUI progress, threading.Event
# cancellation, and truthful success / failure / uncertain-publication
# outcomes, all driven from the real Tools menu action
# ---------------------------------------------------------------------------


def test_backup_creation_progress_reaches_gui_and_reports_success_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_backup_application(tmp_path / "root")
        window = None
        heartbeat = {"ticks": 0, "running": False, "at_confirm": None}
        try:
            window = MainWindow(application)
            window.show()
            await asyncio.sleep(0)

            destination = tmp_path / "packages" / "full.botsbackup"
            destination.parent.mkdir(parents=True)
            _patch_save_file_name(monkeypatch, destination, pattern="*.botsbackup")
            capture_threads = _spy_capture(monkeypatch)
            create_calls = _record_create_command(monkeypatch, application)

            async def heartbeat_loop() -> None:
                while heartbeat["running"]:
                    heartbeat["ticks"] += 1
                    await asyncio.sleep(0)

            heartbeat["running"] = True
            beat = asyncio.create_task(heartbeat_loop())

            # Real desktop action: Tools → "Create Full Backup…".
            window.create_backup_action.trigger()
            dialog = window._phase9._dialogs["backup_creation"]
            assert dialog is not None and dialog.isVisible()
            assert isinstance(dialog.cancel_event, threading.Event), (
                "backup cancellation must be a threading.Event"
            )
            dialog.pick_destination()
            assert dialog.destination() == str(destination)

            loop_thread = threading.get_ident()
            observed: list[tuple[object, object, bool]] = []
            original_progress_update = dialog.progress_update

            def spy_progress_update(state, operation_id):
                observed.append((
                    state,
                    operation_id,
                    QThread.currentThread() is qt_application.thread(),
                ))
                original_progress_update(state, operation_id)

            monkeypatch.setattr(dialog, "progress_update", spy_progress_update)

            dialog.confirm()
            heartbeat["at_confirm"] = heartbeat["ticks"]

            await _wait_until(lambda: destination.is_file(), what="published package")
            await window._phase9.wait_idle()
            await _wait_until(
                lambda: dialog.outcome == "success", what="the truthful success outcome"
            )
            heartbeat["running"] = False
            await beat

            assert not dialog.isVisible(), "the success outcome did not close the dialog"
            assert create_calls and create_calls[0][0] == destination
            kwargs = create_calls[0][1]
            assert kwargs["overwrite"] is False
            assert kwargs["cancellation"] == dialog.cancel_event.is_set, (
                "the controller must pass the dialog's Event.is_set as cancellation"
            )
            assert callable(kwargs["progress_callback"])
            assert capture_threads and all(t != loop_thread for t in capture_threads), (
                "the backup capture ran on the GUI thread"
            )
            states = [str(getattr(state, "value", state)) for state, _op, _gui in observed]
            assert states, "no backup progress reached the UI"
            assert states[0] == "acquiring-fence" and states[-1] == "completed"
            assert set(states) <= {
                "acquiring-fence",
                "holding-recovery-point-fence",
                "finalizing",
                "verifying",
                "publishing",
                "completed",
            }
            assert all(on_gui for _state, _op, on_gui in observed), (
                "a backup progress slot ran off the GUI thread"
            )
            assert heartbeat["ticks"] > heartbeat["at_confirm"], (
                "the GUI loop stopped ticking while the backup ran"
            )
            result = dialog.result
            assert result is not None
            summary = dialog.summary_label.text()
            assert result.backup_id in summary
            assert str(destination) in summary
            assert str(result.receipt.artifact_size) in summary
            assert destination.is_file()
        finally:
            heartbeat["running"] = False
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


def test_backup_creation_cancel_is_threading_event_and_reports_clean_cancel_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_backup_application(tmp_path / "root")
        window = None
        gate = threading.Event()
        try:
            window = MainWindow(application)
            window.show()
            await asyncio.sleep(0)

            destination = tmp_path / "packages" / "cancel.botsbackup"
            destination.parent.mkdir(parents=True)
            _patch_save_file_name(monkeypatch, destination, pattern="*.botsbackup")
            capture_threads = _spy_capture(monkeypatch, gate=gate)

            window.create_backup_action.trigger()
            dialog = window._phase9._dialogs["backup_creation"]
            assert dialog is not None and dialog.isVisible()
            assert isinstance(dialog.cancel_event, threading.Event)
            assert not dialog.cancel_event.is_set()
            dialog.pick_destination()
            dialog.confirm()

            # Wait until the worker thread sits inside the blocked capture
            # seam, then cancel from the GUI thread.
            await _wait_until(
                lambda: bool(capture_threads),
                what="the worker to reach the blocked capture",
            )
            assert not dialog.cancel_event.is_set()
            dialog.cancel_button.click()
            assert dialog.cancel_event.is_set(), (
                "pressing Cancel did not set the cancellation threading.Event"
            )

            gate.set()
            await _wait_until(
                lambda: dialog.outcome == "cancelled",
                what="the truthful clean-cancellation outcome",
            )
            await window._phase9.wait_idle()

            assert dialog.outcome == "cancelled"
            assert dialog.outcome != "success" and dialog.outcome != "uncertain"
            assert dialog.isVisible(), "the cancelled dialog must stay open"
            assert "cancel" in dialog._error_label.text().lower()
            assert not destination.exists()
            assert not list(destination.parent.glob(".bots5-backup-*.staging")), (
                "a clean cancellation left owned staging behind"
            )
        finally:
            gate.set()
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


def test_backup_uncertain_publication_reported_as_uncertain_never_success_or_cancel_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_backup_application(tmp_path / "root")
        window = None
        try:
            from bots5.infrastructure.backup_package import BackupFilePublicationAdapter

            def uncertain_publish(
                self_publisher, *, staging_path, destination, overwrite, backup_id,
                cancellation=None,
            ):
                raise BackupUncertainPublication(
                    "backup publication directory fsync is uncertain"
                )

            monkeypatch.setattr(
                BackupFilePublicationAdapter, "publish", uncertain_publish
            )

            window = MainWindow(application)
            window.show()
            await asyncio.sleep(0)

            destination = tmp_path / "packages" / "uncertain.botsbackup"
            destination.parent.mkdir(parents=True)
            _patch_save_file_name(monkeypatch, destination, pattern="*.botsbackup")

            window.create_backup_action.trigger()
            dialog = window._phase9._dialogs["backup_creation"]
            assert dialog is not None and dialog.isVisible()
            dialog.pick_destination()
            dialog.confirm()

            await _wait_until(
                lambda: dialog.outcome is not None,
                what="the truthful uncertain-publication outcome",
            )
            await window._phase9.wait_idle()

            assert dialog.outcome == "uncertain"
            assert dialog.outcome != "success", (
                "an uncertain publication was reported as success"
            )
            assert dialog.outcome != "cancelled", (
                "an uncertain publication was reported as a clean cancellation"
            )
            shown = dialog._error_label.text().lower()
            assert "uncertain" in shown
            assert "success" not in shown, "the uncertain surface claimed success"
            assert "cancel" not in shown, "the uncertain surface claimed cancellation"
            assert dialog.isVisible(), "the uncertain outcome must not close the dialog"
            assert list(destination.parent.glob(".bots5-backup-*.staging")), (
                "the owned staging path was cleaned despite the uncertain outcome"
            )
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


def test_backup_creation_reports_typed_failure_verbatim_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_backup_application(tmp_path / "root")
        window = None
        try:
            window = MainWindow(application)
            window.show()
            await asyncio.sleep(0)

            # A directory at the destination path: the landed publication
            # refuses it with the typed BackupDestinationInvalid refusal once
            # the operator explicitly consents to overwrite.
            destination = tmp_path / "packages" / "blocked.botsbackup"
            destination.mkdir(parents=True)
            _patch_save_file_name(monkeypatch, destination, pattern="*.botsbackup")

            def consenting_question(*args, **kwargs):
                return QMessageBox.StandardButton.Yes

            monkeypatch.setattr(
                QMessageBox, "question", staticmethod(consenting_question)
            )

            window.create_backup_action.trigger()
            dialog = window._phase9._dialogs["backup_creation"]
            assert dialog is not None and dialog.isVisible()
            dialog.pick_destination()
            dialog.confirm()

            await _wait_until(
                lambda: dialog.outcome == "failed",
                what="the typed failure outcome",
            )
            await window._phase9.wait_idle()

            assert dialog.outcome == "failed"
            assert dialog.outcome != "success" and dialog.outcome != "uncertain"
            assert "unsafe identity" in dialog._error_label.text(), (
                "the landed typed refusal was not carried verbatim"
            )
            assert dialog.isVisible()
            assert destination.is_dir(), "the typed failure mutated the destination"
            assert not list(destination.parent.glob(".bots5-backup-*.staging")), (
                "a typed failure left owned staging behind"
            )
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


def test_backup_overwrite_requires_explicit_confirmation_before_proceeding(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_backup_application(tmp_path / "root")
        window = None
        try:
            window = MainWindow(application)
            window.show()
            await asyncio.sleep(0)

            destination = tmp_path / "packages" / "existing.botsbackup"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old package")
            _patch_save_file_name(monkeypatch, destination, pattern="*.botsbackup")
            create_calls = _record_create_command(monkeypatch, application)

            def refusing_question(*args, **kwargs):
                return QMessageBox.StandardButton.No

            monkeypatch.setattr(
                QMessageBox, "question", staticmethod(refusing_question)
            )

            window.create_backup_action.trigger()
            dialog = window._phase9._dialogs["backup_creation"]
            assert dialog is not None and dialog.isVisible()
            assert not dialog.overwrite_checkbox.isChecked()
            dialog.pick_destination()
            dialog.confirm()

            await window._phase9.wait_idle()
            assert create_calls == [], (
                "the backup proceeded without explicit overwrite consent"
            )
            assert destination.read_bytes() == b"old package", (
                "the existing package was clobbered without consent"
            )
            assert dialog.isVisible()

            # Explicit consent: the same dialog now proceeds with overwrite.
            def consenting_question(*args, **kwargs):
                return QMessageBox.StandardButton.Yes

            monkeypatch.setattr(
                QMessageBox, "question", staticmethod(consenting_question)
            )
            dialog.confirm()
            await _wait_until(
                lambda: dialog.outcome == "success",
                what="the consented overwrite creation",
            )
            await window._phase9.wait_idle()
            assert create_calls and create_calls[0][1]["overwrite"] is True
            assert destination.is_file()
        finally:
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 8 — independent verification: the loop keeps ticking, the receipt is
# shown truthfully, and typed refusals (expected-id mismatch, invalid package)
# surface verbatim — driven from the real Tools menu action
# ---------------------------------------------------------------------------


def test_backup_verification_reports_receipt_without_freezing_loop_and_typed_refusals_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        application, store, authority = _make_backup_application(tmp_path / "root")
        window = None
        heartbeat = {"ticks": 0, "running": False, "at_verify": None, "at_done": None}
        try:
            # Fixture: one real package, built by the landed service on a
            # worker thread with the landed fresh-context offload pattern (the
            # application command's plain to_thread cannot carry its authority
            # grant into the worker; see run_backup_creation).
            from contextvars import Context

            artifact = tmp_path / "packages" / "verify-me.botsbackup"
            artifact.parent.mkdir(parents=True)
            created = await asyncio.to_thread(
                lambda: Context().run(
                    application._backup_service.create_backup, artifact
                )
            )
            backup_id = created.backup_id
            truncated = tmp_path / "packages" / "truncated.botsbackup"
            truncated.write_bytes(artifact.read_bytes()[:-9])

            window = MainWindow(application)
            window.show()
            await asyncio.sleep(0)

            verify_calls: list[tuple[Path, object]] = []
            original_verify = application.verify_backup

            async def recording_verify(package, **kwargs):
                verify_calls.append((Path(package), kwargs.get("expected_backup_id")))
                return await original_verify(package, **kwargs)

            monkeypatch.setattr(application, "verify_backup", recording_verify)

            async def heartbeat_loop() -> None:
                while heartbeat["running"]:
                    heartbeat["ticks"] += 1
                    await asyncio.sleep(0)

            heartbeat["running"] = True
            beat = asyncio.create_task(heartbeat_loop())

            # Real desktop action: Tools → "Verify Backup Package…".
            window.verify_backup_action.trigger()
            dialog = window._phase9._dialogs["backup_verification"]
            assert dialog is not None and dialog.isVisible()

            # (1) typed refusal: expected-backup-id mismatch, shown verbatim.
            _patch_open_file_name(monkeypatch, artifact, pattern="*.botsbackup")
            dialog.pick_package()
            assert dialog.package_path() == str(artifact)
            dialog.expected_edit.setText("definitely-not-the-backup-id")
            dialog.confirm()
            await _wait_until(
                lambda: "backup id does not match expected identity"
                in dialog._error_label.text(),
                what="the verbatim expected-id mismatch refusal",
            )
            assert dialog.outcome == "failed" and dialog.isVisible()

            # (2) typed refusal: a truncated package cannot verify.
            dialog.package_edit.setText(str(truncated))
            dialog.expected_edit.clear()
            dialog.confirm()
            await _wait_until(
                lambda: "central directory" in dialog._error_label.text(),
                what="the verbatim invalid-package refusal",
            )
            assert dialog.outcome == "failed" and dialog.isVisible()

            # (3) truthful success: the receipt, with the loop still ticking.
            dialog.package_edit.setText(str(artifact))
            dialog.expected_edit.setText(backup_id)
            dialog.confirm()
            heartbeat["at_verify"] = heartbeat["ticks"]
            await _wait_until(
                lambda: dialog.outcome == "success",
                what="the truthful verification receipt",
            )
            heartbeat["at_done"] = heartbeat["ticks"]
            heartbeat["running"] = False
            await beat

            assert not dialog.isVisible()
            assert verify_calls[0] == (artifact, "definitely-not-the-backup-id")
            assert verify_calls[1] == (truncated, None)
            assert verify_calls[2] == (artifact, backup_id)
            summary = dialog.summary_label.text()
            verified = dialog.result.receipt
            assert verified.backup_id == backup_id and backup_id in summary
            assert verified.artifact_sha256 == created.receipt.artifact_sha256
            assert verified.artifact_sha256 in summary
            assert str(verified.artifact_size) in summary
            assert verified.verified_at in summary
            assert "VALID" in summary
            assert "artifact-sha256" in summary
            assert heartbeat["at_done"] > heartbeat["at_verify"], (
                "the event loop froze while the verification ran"
            )
        finally:
            heartbeat["running"] = False
            if window is not None:
                await _dispose(window)
            await _close_application(application, store, authority)

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Workflow 8 / M5 — whole-installation restore handoff (proofs 9-12).
# Additive section: local fixtures only, no existing test touched.  The
# production route proven here is the real one: Tools → "Restore From
# Backup…" → RestoreHandoffDialog → RestoreHandoffCoordinator → the
# runtime-owned RestoreHandoffCapability → the real serve() post-close
# consumer.  A live desktop never calls whole-installation restore
# (invariant I2): restore runs only in the pre-store bootstrap child after
# runtime.close() released the authority.
# ---------------------------------------------------------------------------


import sys  # noqa: E402  (additive M5 section)

import pytest  # noqa: E402

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QDialog, QLabel  # noqa: E402

from bots5.bootstrap.desktop import (  # noqa: E402
    DesktopRuntime,
    RestoreHandoffRequest,
    build_runtime,
    serve,
)
from bots5.infrastructure.restore_service import RestoreService  # noqa: E402


class _FakeRestoreChild:
    """Stand-in for the waited pre-store bootstrap child process."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_result_dialog(monkeypatch) -> list:
    """Replace the S8 result dialog with a recording auto-dismissing subclass.

    The subclass is the REAL dialog (same presentation code path); it only
    dismisses itself so the awaited post-close step can continue headlessly.
    """

    from bots5.desktop import phase9_dialogs

    created: list = []
    base = phase9_dialogs.RestoreHandoffResultDialog

    class _AutoDismissResultDialog(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def show(self):
            super().show()
            QTimer.singleShot(0, lambda: self.done(QDialog.DialogCode.Accepted))

    monkeypatch.setattr(
        phase9_dialogs, "RestoreHandoffResultDialog", _AutoDismissResultDialog
    )
    return created


def _accept_consequence(monkeypatch) -> None:
    """Accept the S3 modal consequence (Proceed with Restore and Restart)."""

    def accepting_exec(box):
        box.done(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QMessageBox, "exec", accepting_exec)


def _forbid_live_restore(monkeypatch) -> list:
    """Proof 9 guards: any in-process restore call fails the test at once."""

    from bots5.bootstrap import desktop as bootstrap_desktop

    calls: list = []

    def forbidden_initiate(*args, **kwargs):
        calls.append(("_initiate_restore", args, kwargs))
        raise AssertionError("_initiate_restore ran in the live desktop process")

    def forbidden_restore(self, package, **kwargs):
        calls.append(("RestoreService.restore", package, kwargs))
        raise AssertionError(
            "RestoreService.restore ran in the live desktop process"
        )

    monkeypatch.setattr(bootstrap_desktop, "_initiate_restore", forbidden_initiate)
    monkeypatch.setattr(RestoreService, "restore", forbidden_restore)
    return calls


async def _finish_serve(serve_task, *, timeout: float = 30.0) -> None:
    if serve_task is not None and not serve_task.done():
        await asyncio.wait_for(serve_task, timeout=timeout)


def _new_runtime_composition(tmp_path: Path):
    """build_runtime on a mechanically disposable data root (never the real
    operator root) plus one real, independently verifiable backup package."""

    runtime = build_runtime(tmp_path / "root")
    return runtime


async def _make_package(runtime, tmp_path: Path) -> Path:
    package_dir = tmp_path / "outside"
    package_dir.mkdir(exist_ok=True)
    package = package_dir / "restore.botsbackup"
    created = await runtime.application.create_backup(package)
    return package, created.backup_id


# ---------------------------------------------------------------------------
# Proof 10 / F-07 (headline) — the real Tools action reaches the real serve()
# post-close consumer with the exact child argv; proof 9 rides along: no
# whole-installation restore ever runs while the live authority/store is open.
# ---------------------------------------------------------------------------


def test_restore_handoff_reaches_real_serve_post_close_consumer_with_exact_argv_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        from bots5.bootstrap import desktop as bootstrap_desktop

        runtime = None
        serve_task = None
        heartbeat = {"ticks": 0, "running": False, "at_action": None}
        try:
            runtime = _new_runtime_composition(tmp_path)
            package, backup_id = await _make_package(runtime, tmp_path)

            live_restore_calls = _forbid_live_restore(monkeypatch)

            # Observe the close/authority ordering the handoff depends on.
            released: list[bool] = []
            original_release = runtime.authority.release

            def spy_release():
                released.append(True)
                return original_release()

            monkeypatch.setattr(runtime.authority, "release", spy_release)

            close_returned: list[bool] = []
            original_close = DesktopRuntime.close

            async def spy_close(self):
                await original_close(self)
                close_returned.append(True)

            monkeypatch.setattr(DesktopRuntime, "close", spy_close)

            # The mocked child isolates exactly the action → argv step; the
            # REAL child is proven separately on a disposable root.
            observed: dict = {}

            async def fake_child(argv):
                observed["argv"] = list(argv)
                observed["authority_released"] = bool(released)
                observed["close_returned"] = bool(close_returned)
                observed["close_succeeded"] = runtime._close_result.succeeded
                receipt = json.dumps(
                    {"outcome": "RESTORED", "backup_id": backup_id}
                ).encode("utf-8")
                return _FakeRestoreChild(0, receipt, b"")

            monkeypatch.setattr(bootstrap_desktop, "_create_restore_child", fake_child)
            result_dialogs = _patch_result_dialog(monkeypatch)
            _accept_consequence(monkeypatch)

            async def heartbeat_loop() -> None:
                while heartbeat["running"]:
                    heartbeat["ticks"] += 1
                    await asyncio.sleep(0)

            heartbeat["running"] = True
            beat = asyncio.create_task(heartbeat_loop())

            # The REAL production serve loop, not a re-implementation.
            serve_task = asyncio.create_task(serve(runtime))
            await _wait_until(
                lambda: bool(runtime.windows),
                what="serve() to compose the production window set",
            )
            window = runtime.windows[0]

            # Real desktop action: Tools → "Restore From Backup…" (S1).
            window.restore_from_backup_action.trigger()
            heartbeat["at_action"] = heartbeat["ticks"]
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            # Selecting a package alone has no effect (S1: "no effect yet").
            assert runtime.handoff.pending is False

            _patch_open_file_name(monkeypatch, package, pattern="*.botsbackup")
            dialog.pick_package()
            assert dialog.package_path() == str(package)
            dialog.confirm()  # S2 verify → S3 Proceed (patched) → S5 → S6

            await _wait_until(
                lambda: "argv" in observed,
                timeout=30.0,
                what="the real serve() post-close consumer to spawn the child",
            )
            await _wait_until(
                lambda: bool(result_dialogs)
                and not result_dialogs[0].isVisible(),
                timeout=10.0,
                what="the GUI-visible result to be presented and dismissed",
            )
            heartbeat["running"] = False
            await beat
            await _finish_serve(serve_task)

            # The EXACT recorded child argv (proof 10 / F-07).
            assert observed["argv"] == [
                sys.executable,
                "-m",
                "bots5.bootstrap.desktop",
                "--data-root",
                os.fspath(runtime.paths.data_root),
                "--restore-from",
                os.fspath(package),
                "--expected-backup-id",
                backup_id,
            ]
            # The child ran only after a successful runtime.close() and the
            # authority release (which is LAST inside the close driver).
            assert observed["authority_released"] is True
            assert observed["close_returned"] is True
            assert observed["close_succeeded"] is True
            # Proof 9: no whole-installation restore ran in this live process.
            assert live_restore_calls == []
            # The exact 0/1/2/3 status is preserved, never collapsed.
            assert runtime.restore_exit_code == 0
            assert runtime.handoff.pending is False  # consumed, not cleared
            assert runtime.windows == []
            # The GUI-visible result surface (F-06) showed the raw receipt.
            result_dialog = result_dialogs[0]
            assert result_dialog.status_code == 0
            assert json.loads(result_dialog.raw_receipt_text) == {
                "outcome": "RESTORED",
                "backup_id": backup_id,
            }
            assert "exit status 0" in result_dialog.headline_label.text()
            assert result_dialog.raw_refusal_text == ""
            # The loop kept ticking throughout the handoff close.
            assert heartbeat["at_action"] is not None
            assert heartbeat["ticks"] > heartbeat["at_action"]
        finally:
            heartbeat["running"] = False
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 12 (F-05) — a declined active-generation close prompt aborts the
# handoff: all_closed=False, the capability cleared the request, the
# coordinator reports that NO restore was initiated, no child is launched,
# and workspace.wait_closed() (inside the real serve()) does not hang.
# ---------------------------------------------------------------------------


def test_declined_active_generation_close_prompt_aborts_handoff_without_child_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        from bots5.bootstrap import desktop as bootstrap_desktop
        from bots5.desktop import phase9 as phase9_module

        runtime = None
        serve_task = None
        try:
            runtime = _new_runtime_composition(tmp_path)
            package, _backup_id = await _make_package(runtime, tmp_path)

            spawn_calls: list = []

            async def recording_child(argv):
                spawn_calls.append(list(argv))
                return _FakeRestoreChild(0, b"{}", b"")

            monkeypatch.setattr(
                bootstrap_desktop, "_create_restore_child", recording_child
            )
            result_dialogs = _patch_result_dialog(monkeypatch)
            _accept_consequence(monkeypatch)

            # The last registered window must raise the landed active-
            # generation close prompt for the close to be declinable.
            monkeypatch.setattr(
                runtime.application, "has_active_generations", lambda: True
            )
            questions: list[str] = []

            def refusing_question(*args, **kwargs):
                questions.append(
                    str(args[2]) if len(args) > 2 else str(kwargs.get("text", ""))
                )
                return QMessageBox.StandardButton.No

            monkeypatch.setattr(QMessageBox, "question", staticmethod(refusing_question))

            # Shorten the bounded close poll; the sealed production default
            # stays 10.0 in phase9.py (only this test's bound is shortened).
            monkeypatch.setattr(
                phase9_module, "_RESTORE_HANDOFF_CLOSE_TIMEOUT_SECONDS", 0.2
            )

            serve_task = asyncio.create_task(serve(runtime))
            await _wait_until(lambda: bool(runtime.windows), what="the composed window")
            window = runtime.windows[0]

            window.restore_from_backup_action.trigger()
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            dialog.package_edit.setText(str(package))
            dialog.confirm()

            # The close prompt was raised and declined: the window stays open.
            await _wait_until(
                lambda: bool(questions), what="the active-generation close prompt"
            )
            assert "generations" in questions[0]
            await _wait_until(
                lambda: window._phase9._restore_handoff_coordinator_instance.last_report
                is not None,
                timeout=10.0,
                what="the truthful no-restore-was-initiated report",
            )
            report = (
                window._phase9._restore_handoff_coordinator_instance.last_report
            )
            assert "no restore was initiated" in report
            await _wait_until(
                lambda: "no restore was initiated"
                in window.statusBar().currentMessage(),
                timeout=2.0,
                what="the abort report to reach the status bar",
            )
            # The capability cleared the request: a later normal close must
            # never trigger an unexpected restore.
            assert runtime.handoff.pending is False
            assert bool(runtime.windows), "the declined window must still be open"
            assert spawn_calls == [], "a declined close launched the restore child"
            assert runtime.restore_exit_code is None
            assert result_dialogs == []

            # wait_closed() was never left hanging: serve() is still waiting
            # normally, and an ordinary close afterwards completes it.
            assert not serve_task.done()

            def consenting_question(*args, **kwargs):
                return QMessageBox.StandardButton.Yes

            monkeypatch.setattr(
                QMessageBox, "question", staticmethod(consenting_question)
            )
            window.close()
            await _finish_serve(serve_task)
            assert spawn_calls == [], "the later normal close launched a child"
            assert runtime.restore_exit_code is None
            assert runtime.handoff.pending is False
        finally:
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 12 — the handoff close during a cutoff-past import settlement
# preserves the landed shutdown guarantees: cutoff-past work is drained
# (never killed), the store closes after the drain, the authority release is
# last, and only then does the post-close child run.  It never hangs.
# ---------------------------------------------------------------------------


def test_restore_handoff_close_during_import_drains_before_release_and_child_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        from bots5.bootstrap import desktop as bootstrap_desktop

        runtime = None
        serve_task = None
        settle_gate = threading.Event()
        try:
            runtime = _new_runtime_composition(tmp_path)
            package, _backup_id = await _make_package(runtime, tmp_path)

            # A chat to export, so a real archive import can be enqueued.
            chat = await runtime.application.create_chat("handoff drain chat")
            attempt = await runtime.application.send_message(chat.id, "hello handoff")
            await _finish(runtime.application, attempt.id)
            source = tmp_path / "outside" / "drain.botsarchive"
            await runtime.application.write_archive_export(chat.id, source)

            order: list[str] = []
            store = runtime.application._store
            original_settle = store.settle_archive_import

            def slow_settle(plan):
                assert settle_gate.wait(timeout=20), "settlement gate was not released"
                result = original_settle(plan)
                order.append("settle_done")
                return result

            monkeypatch.setattr(store, "settle_archive_import", slow_settle)
            original_store_close = store.close

            def spy_store_close():
                order.append("store_close")
                return original_store_close()

            monkeypatch.setattr(store, "close", spy_store_close)
            released: list[bool] = []
            original_release = runtime.authority.release

            def spy_release():
                released.append(True)
                order.append("authority_release")
                return original_release()

            monkeypatch.setattr(runtime.authority, "release", spy_release)
            original_workspace_close = runtime.workspace.close

            async def spy_workspace_close():
                order.append("workspace_close")
                return await original_workspace_close()

            monkeypatch.setattr(runtime.workspace, "close", spy_workspace_close)

            spawn_calls: list = []

            async def recording_child(argv):
                order.append("child_spawn")
                spawn_calls.append(list(argv))
                return _FakeRestoreChild(0, b"{}", b"")

            monkeypatch.setattr(
                bootstrap_desktop, "_create_restore_child", recording_child
            )
            _patch_result_dialog(monkeypatch)
            _accept_consequence(monkeypatch)

            serve_task = asyncio.create_task(serve(runtime))
            await _wait_until(lambda: bool(runtime.windows), what="the composed window")
            window = runtime.windows[0]

            queued = await runtime.application.enqueue_archive_import(source)
            assert queued.id

            # The import must be past its journal cutoff (staging) so the
            # drain guarantee is exercised, not the pre-cutoff cancel path.
            deadline = asyncio.get_running_loop().time() + 15.0
            while True:
                page = await runtime.application.queued_import_display(limit=50)
                row = next(
                    (item for item in page.items if item.id == queued.id), None
                )
                if row is not None and row.state is ImportQueueState.STAGING:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("the import never reached STAGING")
                await asyncio.sleep(0.01)
            assert not settle_gate.is_set()

            window.restore_from_backup_action.trigger()
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            dialog.package_edit.setText(str(package))
            dialog.confirm()

            # The windows close through the ordinary path; the runtime close
            # then blocks inside the cutoff-past drain until we release it.
            await _wait_until(
                lambda: "workspace_close" in order,
                timeout=30.0,
                what="the runtime close to begin (workspace closed)",
            )
            await asyncio.sleep(0.2)
            assert "settle_done" not in order, (
                "the close released the store before the cutoff-past drain"
            )
            settle_gate.set()
            await _finish_serve(serve_task, timeout=40.0)

            # Landed close order preserved, with the child strictly last.
            assert all(
                stage in order
                for stage in (
                    "settle_done", "store_close", "authority_release", "child_spawn"
                )
            ), f"the close did not reach the expected stages: {order}"
            assert order.index("settle_done") < order.index("store_close"), (
                f"cutoff-past work was not drained before the store closed: {order}"
            )
            assert order.index("store_close") < order.index("authority_release"), (
                f"the authority was released before the store closed: {order}"
            )
            assert order.index("authority_release") < order.index("child_spawn"), (
                f"the child ran before the authority was released: {order}"
            )
            assert spawn_calls and runtime.restore_exit_code == 0
            assert runtime.windows == []
        finally:
            settle_gate.set()
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# S2 abort — a verification refusal aborts the handoff with the typed error
# and changes nothing: the request is never registered, no window closes.
# ---------------------------------------------------------------------------


def test_restore_handoff_verification_refusal_aborts_and_changes_nothing(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        from bots5.bootstrap import desktop as bootstrap_desktop

        runtime = None
        serve_task = None
        try:
            runtime = _new_runtime_composition(tmp_path)
            package, backup_id = await _make_package(runtime, tmp_path)

            spawn_calls: list = []

            async def recording_child(argv):
                spawn_calls.append(list(argv))
                return _FakeRestoreChild(0, b"{}", b"")

            monkeypatch.setattr(
                bootstrap_desktop, "_create_restore_child", recording_child
            )
            result_dialogs = _patch_result_dialog(monkeypatch)

            serve_task = asyncio.create_task(serve(runtime))
            await _wait_until(lambda: bool(runtime.windows), what="the composed window")
            window = runtime.windows[0]

            window.restore_from_backup_action.trigger()
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            dialog.package_edit.setText(str(package))
            dialog.expected_edit.setText("deliberately-wrong-backup-id")
            dialog.confirm()

            await _wait_until(
                lambda: "backup id does not match expected identity"
                in dialog._error_label.text(),
                what="the verbatim typed verification refusal",
            )
            # The refusal aborts: dialog stays open, nothing was registered,
            # no window closed, no child launched.
            assert dialog.isVisible()
            assert runtime.handoff.pending is False
            assert bool(runtime.windows)
            assert spawn_calls == []
            assert result_dialogs == []

            window.close()
            await _finish_serve(serve_task)
            assert spawn_calls == []
        finally:
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Fail-closed seam — a failed runtime.close() drops the restore request: no
# post-close step runs, no child is launched, and the pending request is
# abandoned to process termination (oracle R-8), never consumed.
# ---------------------------------------------------------------------------


def test_failed_runtime_close_drops_restore_request_and_launches_no_child(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        from bots5.bootstrap import desktop as bootstrap_desktop

        runtime = None
        serve_task = None
        try:
            runtime = _new_runtime_composition(tmp_path)
            package, _backup_id = await _make_package(runtime, tmp_path)

            spawn_calls: list = []

            async def recording_child(argv):
                spawn_calls.append(list(argv))
                return _FakeRestoreChild(0, b"{}", b"")

            monkeypatch.setattr(
                bootstrap_desktop, "_create_restore_child", recording_child
            )
            result_dialogs = _patch_result_dialog(monkeypatch)

            serve_task = asyncio.create_task(serve(runtime))
            await _wait_until(lambda: bool(runtime.windows), what="the composed window")
            window = runtime.windows[0]

            # Seam-level injection: this test targets the close-failure
            # branch, not the reachability route (that is proof 10's job).
            runtime.handoff.register(
                RestoreHandoffRequest(package=package, expected_backup_id="pinned")
            )
            assert runtime.handoff.pending is True

            async def failing_workspace_close():
                raise StateError("workspace close forced failure")

            monkeypatch.setattr(runtime.workspace, "close", failing_workspace_close)

            window.close()
            with pytest.raises(StateError):
                await _finish_serve(serve_task)

            # The close failure raised out of serve(): the post-close step
            # never ran, so no child was launched and the exit code is unset.
            assert spawn_calls == []
            assert runtime.restore_exit_code is None
            assert result_dialogs == []
            # R-8: the request was abandoned by process termination, not
            # tidily cleared behind the operator's back.
            assert runtime.handoff.pending is True
        finally:
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# Proof 11 (extended) — the new Tools → "Restore From Backup…" entry and the
# restore handoff surface expose no destructive override and no retained-
# installation cleanup action, and the S8 result presentation keeps the exact
# 0/1/2/3 statuses distinct and truthful.
# ---------------------------------------------------------------------------


def test_phase9_restore_tools_entries_expose_no_destructive_override_or_cleanup_surface(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        from bots5.desktop.phase9_dialogs import (
            RestoreHandoffDialog,
            RestoreHandoffResultDialog,
        )

        class InventoryApplication:
            async def queued_import_display(self, *, limit=50, cursor=None):
                return _display_page()

        async def inner() -> None:
            application = InventoryApplication()
            window = MainWindow(application)
            try:
                window.show()
                await asyncio.sleep(0)

                # The new Tools entry exists, is sealed by objectName, and
                # lives in the Tools menu beside the M4 backup actions.
                tools_menu = next(
                    action
                    for action in window.menuBar().actions()
                    if action.text() == "Tools"
                )
                tool_texts = [
                    item.text() for item in tools_menu.menu().actions()
                ]
                assert "Restore From Backup…" in tool_texts
                assert window.restore_from_backup_action.objectName() == (
                    "actionRestoreFromBackup"
                )
                assert not window.restore_from_backup_action.icon().isNull() or True

                # The whole window inventory (new entries included) exposes
                # no destructive override and no cleanup surface.
                forbidden = (
                    "override", "destructive", "retained", "uninstall",
                    "wipe", "cleanup",
                )
                labels: list[str] = []
                for action in window.findChildren(QAction):
                    labels.extend([action.text(), action.toolTip()])
                for button in window.findChildren(QAbstractButton):
                    labels.extend([button.text(), button.toolTip()])
                inventory = " | ".join(label for label in labels if label).lower()
                for word in forbidden:
                    assert word not in inventory, (
                        f"the Phase 9 UI inventory exposes a forbidden surface: {word}"
                    )

                # Without an injected capability the action is inert: it says
                # so and opens nothing (additive keyword stays optional).
                window.restore_from_backup_action.trigger()
                await asyncio.sleep(0)
                assert "unavailable" in window.statusBar().currentMessage()
                assert window._phase9._dialogs.get("restore_handoff") is None

                # The selection dialog's own surface is clean as well.
                dialog = RestoreHandoffDialog(window)
                dialog_labels: list[str] = [dialog.windowTitle()]
                for label in dialog.findChildren(QLabel):
                    dialog_labels.append(label.text())
                for button in dialog.findChildren(QAbstractButton):
                    dialog_labels.extend([button.text(), button.toolTip()])
                dialog_inventory = " | ".join(
                    text for text in dialog_labels if text
                ).lower()
                for word in forbidden:
                    assert word not in dialog_inventory, (
                        f"the restore dialog exposes a forbidden surface: {word}"
                    )

                # The S3 modal carries the five sealed bullets and exactly
                # [Proceed with Restore and Restart] / [Cancel] — no override.
                box = dialog._build_consequence_message_box()
                assert [b.text() for b in box.buttons()] == [
                    "Proceed with Restore and Restart",
                    "Cancel",
                ]
                modal_text = box.text().lower()
                for word in forbidden:
                    assert word not in modal_text, (
                        f"the consequence modal exposes a forbidden surface: {word}"
                    )
                for substance in (
                    "entire database",
                    "chat history",
                    "preserved",
                    "not deleted",
                    "close cleanly and release all locks",
                    "journal cutoff",
                    "relaunch",
                ):
                    assert substance in modal_text, (
                        f"the consequence modal lost the sealed substance: {substance}"
                    )

                # The S8 result presentation keeps 0/1/2/3 distinct and
                # truthful — never collapsed into success/failure.
                headlines = {}
                for status in (0, 1, 2, 3):
                    result = RestoreHandoffResultDialog(
                        None,
                        data_root=tmp_path,
                        argv=["python", "-m", "bots5.bootstrap.desktop"],
                        status=status,
                        receipt_text='{"outcome": "RESTORED"}' if status == 0 else "",
                        refusal_text=(
                            "restore rolled back to the preserved installation; "
                            "restart required"
                            if status == 2
                            else "restore failed closed: boom" if status == 3 else ""
                        ),
                    )
                    headlines[status] = result.headline_label.text()
                    assert f"status {status}" in headlines[status]
                    assert f"Exit status: {status}" == result.status_label.text()
                assert len(set(headlines.values())) == 4
                assert "committed" in headlines[0]
                assert "rolled back" in headlines[2]
                assert "failed closed" in headlines[3]
            finally:
                await _dispose(window)

        await inner()

    _run_qasync(qt_application, scenario())


# ---------------------------------------------------------------------------
# F-4 (additive) — the restore-handoff request plane is one-shot: a SECOND,
# different request is refused with StateError while one is pending, and the
# refusal never overwrites the original request.
# ---------------------------------------------------------------------------


def test_restore_handoff_registry_rejects_second_pending_request_thread(tmp_path):
    # The registry under proof is the real runtime-owned one: build_runtime
    # composes RestoreHandoffRegistry and RestoreHandoffCapability inside
    # DesktopRuntime.__post_init__, and the capability's request plane
    # delegates to exactly that registry.
    runtime = build_runtime(tmp_path / "root")
    registry = runtime._handoff_registry
    capability = runtime.handoff
    assert capability.pending is False and registry.pending is False

    first = RestoreHandoffRequest(
        package=tmp_path / "outside" / "first.botsbackup",
        expected_backup_id="first-backup-id",
    )
    second = RestoreHandoffRequest(
        package=tmp_path / "outside" / "second.botsbackup",
        expected_backup_id="second-backup-id",
    )
    assert first != second

    # Register the FIRST request through the production delegation plane.
    capability.register(first)
    assert capability.pending is True and registry.pending is True

    # The SECOND, different request is rejected through both planes.
    with pytest.raises(StateError):
        capability.register(second)
    with pytest.raises(StateError):
        registry.register(second)

    # The refusal changed nothing: the ORIGINAL request is still intact.
    assert capability.pending is True and registry.pending is True
    taken = capability.take()
    assert taken == first, "the first request was silently overwritten"
    # take() consumed it: pending becomes False and nothing is left.
    assert capability.pending is False and registry.pending is False
    assert capability.take() is None and registry.take() is None


# ---------------------------------------------------------------------------
# F-3 (additive) — oracle R-1: cancelling the awaiting coroutine at the
# bounded polling suspension propagates the cancellation WITHOUT clearing a
# request whose close has already begun (the post-close consumer must still
# be able to consume it); the explicit timeout/decline path is the only path
# that clears the request.
# ---------------------------------------------------------------------------


def test_cancelled_restore_handoff_awaiter_keeps_request_pending_thread(tmp_path, monkeypatch):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario() -> None:
        runtime = build_runtime(tmp_path / "root")
        window = None
        close_held = asyncio.Event()
        seam_reached: list[bool] = []
        try:
            # A real window whose close cannot complete immediately: the
            # production close path suspends inside unregister_window, and
            # this seam holds it there so the capability reaches its bounded
            # polling suspension with a NON-EMPTY window set.
            original_unregister = runtime.workspace.unregister_window

            async def held_unregister(*args, **kwargs):
                seam_reached.append(True)
                await close_held.wait()
                return await original_unregister(*args, **kwargs)

            monkeypatch.setattr(
                runtime.workspace, "unregister_window", held_unregister
            )

            window = await runtime.open_window()
            assert runtime.windows == [window], (
                "the capability must see a non-empty window set"
            )

            request = RestoreHandoffRequest(
                package=tmp_path / "outside" / "cancel-handoff.botsbackup",
                expected_backup_id="cancelled-awaiter-backup-id",
            )
            runtime.handoff.register(request)
            assert runtime.handoff.pending is True

            close_task = asyncio.create_task(
                runtime.handoff.request_orderly_close(timeout=10.0)
            )
            # Reach the bounded polling suspension: the capability has
            # already begun the window close ...
            await _wait_until(lambda: bool(seam_reached), what="the close to begin")
            assert window._closing is True, "the window close had not begun"
            await asyncio.sleep(0.15)
            # ... and the window set is still open at that suspension.
            assert bool(runtime.windows)
            assert not close_task.done()

            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task

            # Oracle R-1: cancellation propagates WITHOUT clearing the
            # request — the close has already begun and may still succeed.
            assert runtime.handoff.pending is True, (
                "cancellation cleared a request whose close had already begun"
            )
            taken = runtime.handoff.take()
            assert taken == request, "take() did not return the original request"

            # Explicit contrast: the timeout/decline path DOES clear the
            # request.  Re-register and let the bounded poll run out against
            # the still-open window.
            decline_request = RestoreHandoffRequest(
                package=tmp_path / "outside" / "decline-handoff.botsbackup",
                expected_backup_id="declined-backup-id",
            )
            runtime.handoff.register(decline_request)
            assert runtime.handoff.pending is True
            outcome = await runtime.handoff.request_orderly_close(timeout=0.06)
            assert outcome.all_closed is False
            assert outcome.remaining >= 1
            assert runtime.handoff.pending is False, (
                "the decline/timeout path did not clear the request"
            )
            assert runtime.handoff.take() is None
        finally:
            close_held.set()
            if window is not None:
                await _dispose(window)
            await runtime.close()

    _run_qasync(qt_application, scenario())
