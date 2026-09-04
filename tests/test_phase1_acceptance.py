from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from bots5.bootstrap.desktop import build_runtime
from bots5.desktop.window import MainWindow


def test_walking_skeleton_survives_close_and_restart(tmp_path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        runtime = build_runtime(tmp_path / "data")
        window = MainWindow(runtime.application)
        try:
            await window.initialize()
            chat_id = window._current_chat_id
            assert chat_id is not None
            window.composer.setPlainText("hello from the desktop")
            await window._send_message()
            await asyncio.sleep(0.05)
            _, messages = await runtime.application.open_chat(chat_id)
            assert messages[-1].content == "fake response to: hello from the desktop"
            assert messages[-1].state.value == "complete"
        finally:
            window.stop_bridge()
            await runtime.close()

        restarted = build_runtime(tmp_path / "data")
        try:
            chats = await restarted.application.list_chats()
            assert len(chats) == 1
            _, messages = await restarted.application.open_chat(chats[0].id)
            assert [message.content for message in messages] == [
                "hello from the desktop",
                "fake response to: hello from the desktop",
            ]
        finally:
            await restarted.close()

    asyncio.run(scenario())
    qt_application.processEvents()
