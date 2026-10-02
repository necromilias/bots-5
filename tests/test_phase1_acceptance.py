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
            for _ in range(100):
                _, messages = await runtime.application.open_chat(chat_id)
                if messages and messages[-1].state.value == "complete":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("desktop generation did not terminalize")
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


def test_walking_skeleton_persists_rename_and_messages_across_full_restart(tmp_path):
    """A-1 durability: a renamed chat with a completed turn survives a whole-runtime restart.

    Unlike a pure service probe this walks the real desktop boot twice: the
    rename and the completed generation must both be durable across the
    close/reopen of the full runtime, and the rename must not have bumped the
    chat revision.
    """
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        runtime = build_runtime(tmp_path / "data")
        window = MainWindow(runtime.application)
        try:
            await window.initialize()
            chat_id = window._current_chat_id
            assert chat_id is not None
            window.composer.setPlainText("durable before rename")
            await window._send_message()
            for _ in range(100):
                _, messages = await runtime.application.open_chat(chat_id)
                if messages and messages[-1].state.value == "complete":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("desktop generation did not terminalize")
            revision_before_rename = (await runtime.application.open_chat(chat_id))[0].revision

            # Feature-positive: the rename lands through the live runtime.
            renamed = await runtime.application.rename_chat(
                chat_id, "Durable Renamed Chat"
            )
            assert renamed.title == "Durable Renamed Chat"
            assert renamed.revision == revision_before_rename
        finally:
            window.stop_bridge()
            window.deleteLater()
            await runtime.close()

        restarted = build_runtime(tmp_path / "data")
        try:
            chats = await restarted.application.list_chats()
            assert len(chats) == 1
            # The rename was durable: the restarted runtime reads the new title.
            assert chats[0].title == "Durable Renamed Chat"
            # ...and the rename did not bump the persisted revision.
            assert chats[0].revision == revision_before_rename
            _, messages = await restarted.application.open_chat(chats[0].id)
            assert [message.content for message in messages] == [
                "durable before rename",
                "fake response to: durable before rename",
            ]
        finally:
            await restarted.close()

    asyncio.run(scenario())
    qt_application.processEvents()
