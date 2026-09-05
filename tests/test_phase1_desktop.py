from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database
from bots5.desktop.window import MainWindow
from bots5.domain.models import MessageRole
from bots5.bootstrap.desktop import _parser


def test_minimal_native_window_has_chat_list_transcript_and_composer(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        window = MainWindow(application)
        try:
            await window.initialize()
            assert window.chat_list.count() == 1
            assert window.transcript.isReadOnly()
            assert window.composer.placeholderText() == "Message"
        finally:
            window.stop_bridge()
            await application.close()

    import asyncio

    asyncio.run(scenario())
    qt_application.processEvents()


def test_stale_transcript_refresh_cannot_overwrite_the_selected_chat():
    qt_application = QApplication.instance() or QApplication([])
    release_first = asyncio.Event()

    class FakeApplication:
        async def open_chat(self, chat_id):
            if chat_id == "first":
                await release_first.wait()
            return None, (
                SimpleNamespace(
                    role=MessageRole.ASSISTANT,
                    revision=1,
                    state=SimpleNamespace(value="complete"),
                    content=f"content-{chat_id}",
                ),
            )

    async def scenario():
        window = MainWindow(FakeApplication())
        try:
            window._current_chat_id = "first"
            first_refresh = asyncio.create_task(window._refresh_transcript("first"))
            await asyncio.sleep(0)

            window._current_chat_id = "second"
            second_refresh = asyncio.create_task(window._refresh_transcript("second"))
            await second_refresh
            assert window.transcript.toPlainText().endswith("content-second")

            release_first.set()
            await first_refresh
            assert window._current_chat_id == "second"
            assert window.transcript.toPlainText().endswith("content-second")
        finally:
            window.stop_bridge()
            window.close()

    asyncio.run(scenario())
    qt_application.processEvents()


def test_desktop_parser_exposes_explicit_local_backend_configuration():
    args = _parser().parse_args(
        [
            "--backend",
            "local_openai",
            "--base-url",
            "http://127.0.0.1:8000/v1",
            "--model",
            "Qwen3-8B",
            "--api-key-env",
            "LOCAL_QWEN_KEY",
            "--reasoning-effort",
            "none",
        ]
    )

    assert args.backend == "local_openai"
    assert args.base_url == "http://127.0.0.1:8000/v1"
    assert args.model == "Qwen3-8B"
    assert args.api_key_env == "LOCAL_QWEN_KEY"
    assert args.reasoning_effort == "none"
    assert _parser().parse_args([]).backend == "fake"
    assert _parser().parse_args([]).reasoning_effort is None
