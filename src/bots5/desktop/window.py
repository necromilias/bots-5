from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from bots5.core.application import BotsApplication
from bots5.core.events import CoreEvent
from bots5.domain.models import MessageRole

from .bridge import CoreEventBridge


class MainWindow(QMainWindow):
    closed = Signal()

    def __init__(self, application: BotsApplication):
        super().__init__()
        self._application = application
        self._bridge = CoreEventBridge(application)
        self._chat_ids: list[str] = []
        self._current_chat_id: str | None = None
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._refresh_generation = 0

        self.setWindowTitle("B.O.T.S. 5")
        self.resize(900, 600)
        self._build_ui()
        self._bridge.event_received.connect(self._on_event)

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        splitter = QSplitter(Qt.Horizontal, root)

        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Chats", left))
        self.chat_list = QListWidget(left)
        self.chat_list.currentRowChanged.connect(self._on_chat_selected)
        left_layout.addWidget(self.chat_list)
        self.new_chat_button = QPushButton("New Chat", left)
        self.new_chat_button.clicked.connect(self._on_new_chat)
        left_layout.addWidget(self.new_chat_button)

        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        self.transcript = QPlainTextEdit(right)
        self.transcript.setReadOnly(True)
        right_layout.addWidget(self.transcript)
        composer_row = QHBoxLayout()
        self.composer = QPlainTextEdit(right)
        self.composer.setPlaceholderText("Message")
        self.composer.setFixedHeight(80)
        composer_row.addWidget(self.composer)
        self.send_button = QPushButton("Send", right)
        self.send_button.clicked.connect(self._on_send)
        composer_row.addWidget(self.send_button)
        right_layout.addLayout(composer_row)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([220, 680])
        layout.addWidget(splitter)
        self.setCentralWidget(root)

    async def initialize(self) -> None:
        self._bridge.start()
        chats = await self._application.list_chats()
        if not chats:
            await self._application.create_chat()
            chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        if chats:
            self.chat_list.setCurrentRow(0)
            await self._refresh_transcript(chats[0].id)

    def _replace_chat_list(self, chats) -> None:
        self._chat_ids = [chat.id for chat in chats]
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        self.chat_list.addItems([chat.title for chat in chats])
        self.chat_list.blockSignals(False)

    def _on_chat_selected(self, row: int) -> None:
        if 0 <= row < len(self._chat_ids):
            self._current_chat_id = self._chat_ids[row]
            self._schedule(self._refresh_transcript(self._current_chat_id))

    def _on_new_chat(self) -> None:
        self._schedule(self._create_chat())

    async def _create_chat(self) -> None:
        chat = await self._application.create_chat()
        chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        self.chat_list.setCurrentRow(self._chat_ids.index(chat.id))

    def _on_send(self) -> None:
        self._schedule(self._send_message())

    async def _send_message(self) -> None:
        if self._current_chat_id is None:
            return
        text = self.composer.toPlainText()
        if not text.strip():
            return
        self.composer.clear()
        try:
            await self._application.send_message(self._current_chat_id, text)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    def _on_event(self, event: CoreEvent) -> None:
        if not isinstance(event, CoreEvent):
            return
        self._schedule(self._handle_event(event))

    async def _handle_event(self, event: CoreEvent) -> None:
        chat_id = event.payload.get("chat_id")
        if chat_id != self._current_chat_id and event.kind not in {"chat_created"}:
            return
        if event.kind == "chat_created":
            chats = await self._application.list_chats()
            self._replace_chat_list(chats)
        if chat_id == self._current_chat_id:
            await self._refresh_transcript(self._current_chat_id)

    async def _refresh_transcript(self, chat_id: str) -> None:
        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            _, messages = await self._application.open_chat(chat_id)
        except Exception:
            return
        if generation != self._refresh_generation or chat_id != self._current_chat_id:
            return
        lines: list[str] = []
        for message in messages:
            label = "You" if message.role == MessageRole.USER else "B.O.T.S."
            lines.append(f"{label} [r{message.revision} {message.state.value}]\n{message.content}")
        self.transcript.setPlainText("\n\n".join(lines))
        scrollbar = self.transcript.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _schedule(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    def stop_bridge(self) -> None:
        self._bridge.stop()
        for task in tuple(self._refresh_tasks):
            if not task.done():
                task.cancel()
        self._refresh_tasks.clear()

    def closeEvent(self, event) -> None:
        self._bridge.stop()
        self.closed.emit()
        event.accept()
