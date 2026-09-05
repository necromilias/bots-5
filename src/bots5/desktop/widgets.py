from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bots5.domain.models import (
    Chat,
    ChatActivity,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)

from .profile import DesktopSessionInfo


class ComposerEdit(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("composer")
        self.setTabChangesFocus(False)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.NoModifier:
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class TopBar(QFrame):
    rail_toggle_requested = Signal()
    details_toggled = Signal(bool)

    def __init__(self, session: DesktopSessionInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(6)

        self.rail_toggle = QToolButton(self)
        self.rail_toggle.setText("☰")
        self.rail_toggle.setToolTip("Collapse or expand the chat rail")
        self.rail_toggle.setAccessibleName("Toggle chat rail")
        self.rail_toggle.clicked.connect(lambda: self.rail_toggle_requested.emit())
        layout.addWidget(self.rail_toggle)

        self.brand_label = QLabel("B.O.T.S.", self)
        self.brand_label.setObjectName("brandLabel")
        layout.addWidget(self.brand_label)

        self.model_pill = QLabel(session.display_label, self)
        self.model_pill.setObjectName("modelPill")
        self.model_pill.setToolTip(
            "Current runtime model identity; model switching is not available in this slice."
        )
        layout.addWidget(self.model_pill)

        self.tune_button = self._disabled_button(
            "Tune",
            "Model tuning is not configurable in the current Phase 3 runtime.",
        )
        layout.addWidget(self.tune_button)

        layout.addStretch(1)

        self.settings_button = self._disabled_button(
            "⚙",
            "Provider and settings management are deferred beyond this UI slice.",
        )
        self.settings_button.setAccessibleName("Settings unavailable")
        layout.addWidget(self.settings_button)

        self.details_button = QToolButton(self)
        self.details_button.setText("Details")
        self.details_button.setCheckable(True)
        self.details_button.setToolTip("Show or hide the read-only details inspector")
        self.details_button.setAccessibleName("Toggle details inspector")
        self.details_button.toggled.connect(self.details_toggled)
        layout.addWidget(self.details_button)

    @staticmethod
    def _disabled_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("disabledAffordance")
        button.setText(text)
        button.setEnabled(False)
        button.setToolTip(tooltip)
        return button


class LeftRail(QFrame):
    new_chat_requested = Signal()
    chat_indicator_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("leftRail")
        self._collapsed = False
        self._expanded_width = 220
        self._collapsed_width = 48

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)

        self.icon_row = QHBoxLayout()
        self.icon_row.setSpacing(4)
        layout.addLayout(self.icon_row)

        self.chat_button = self._icon_button("▦", "Show chats", checked=True)
        self.chat_button.clicked.connect(self._focus_chat_list)
        self.icon_row.addWidget(self.chat_button)

        self.new_chat_button = self._icon_button("＋", "Create a new chat")
        self.new_chat_button.clicked.connect(lambda: self.new_chat_requested.emit())
        self.icon_row.addWidget(self.new_chat_button)
        self.icon_row.addStretch(1)

        self.section_label = QLabel("Chats", self)
        self.section_label.setObjectName("chatTitle")
        layout.addWidget(self.section_label)

        self.chat_list = QListWidget(self)
        self.chat_list.setObjectName("chatList")
        self.chat_list.setAccessibleName("Chats")
        layout.addWidget(self.chat_list, 1)

        self.activity_column = QVBoxLayout()
        self.activity_column.setContentsMargins(0, 2, 0, 0)
        self.activity_column.setSpacing(4)
        layout.addLayout(self.activity_column)
        self._chat_buttons: dict[str, QToolButton] = {}
        self._chat_titles: dict[str, str] = {}

    def _focus_chat_list(self) -> None:
        if self._collapsed:
            self.set_collapsed(False)
        self.chat_list.setFocus()

    @staticmethod
    def _icon_button(text: str, tooltip: str, *, checked: bool = False) -> QToolButton:
        button = QToolButton()
        button.setObjectName("railIcon")
        button.setText(text)
        button.setCheckable(checked)
        button.setChecked(checked)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        width = self._collapsed_width if collapsed else self._expanded_width
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)
        self.icon_row.setDirection(
            QBoxLayout.Direction.TopToBottom
            if collapsed
            else QBoxLayout.Direction.LeftToRight
        )
        self.section_label.setVisible(not collapsed)
        self.chat_list.setVisible(not collapsed)
        for button in self._chat_buttons.values():
            button.setVisible(collapsed)

    def set_chats(self, chats: Iterable[Chat], selected_chat_id: str | None) -> None:
        chats = tuple(chats)
        while self.activity_column.count():
            item = self.activity_column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._chat_buttons.clear()
        self._chat_titles = {chat.id: chat.title for chat in chats}
        for chat in chats:
            button = self._icon_button("·", chat.title)
            button.setObjectName("chatActivityIndicator")
            button.setCheckable(True)
            button.setFixedSize(32, 32)
            button.setVisible(self._collapsed)
            button.clicked.connect(
                lambda checked=False, chat_id=chat.id: self.chat_indicator_requested.emit(chat_id)
            )
            self.activity_column.addWidget(button)
            self._chat_buttons[chat.id] = button
        self.activity_column.addStretch(1)
        self.chat_list.blockSignals(True)
        try:
            self.chat_list.clear()
            selected_row = -1
            for row, chat in enumerate(chats):
                item = QListWidgetItem(chat.title)
                item.setData(Qt.ItemDataRole.UserRole, chat.id)
                self.chat_list.addItem(item)
                if chat.id == selected_chat_id:
                    selected_row = row
            if selected_row >= 0:
                self.chat_list.setCurrentRow(selected_row)
            elif chats:
                self.chat_list.setCurrentRow(0)
        finally:
            self.chat_list.blockSignals(False)
        self.set_activity({}, selected_chat_id)

    def set_activity(
        self,
        activities: Mapping[str, ChatActivity],
        selected_chat_id: str | None,
    ) -> None:
        for chat_id, button in self._chat_buttons.items():
            activity = activities.get(chat_id, ChatActivity())
            if activity.has_running:
                marker = "●"
                state = "running"
            elif activity.needs_attention:
                marker = "!"
                state = "needs attention"
            elif activity.background_completion:
                marker = "✓"
                state = "completed in background"
            else:
                marker = "·"
                state = "idle"
            button.setText(marker)
            title = self._chat_titles.get(chat_id, chat_id)
            selected = "; selected" if chat_id == selected_chat_id else ""
            button.setToolTip(f"{title} — {state}{selected}")
            button.setAccessibleName(button.toolTip())
            button.setChecked(chat_id == selected_chat_id)


class MessageRow(QWidget):
    copy_requested = Signal(object)
    edit_requested = Signal(object)
    inspect_requested = Signal(object)
    regenerate_requested = Signal(object)

    def __init__(self, message: Message, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.message = message
        self.setProperty("messageRole", message.role.value)

        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(5, 3, 5, 3)
        row_layout.setSpacing(8)

        self.avatar = QLabel("B" if message.role is MessageRole.ASSISTANT else "M", self)
        self.avatar.setObjectName(
            "assistantAvatar" if message.role is MessageRole.ASSISTANT else "userAvatar"
        )
        self.avatar.setFixedSize(28, 28)

        self.bubble = QFrame(self)
        self.bubble.setObjectName("messageBubble")
        self.bubble.setProperty("role", message.role.value)
        self.bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(11, 8, 8, 6)
        bubble_layout.setSpacing(4)

        self.body = QLabel(message.content, self.bubble)
        self.body.setObjectName("messageBody")
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        bubble_layout.addWidget(self.body)

        self.activity_label = QLabel("● Generating…", self.bubble)
        self.activity_label.setObjectName("messageActivity")
        self.activity_label.setVisible(False)
        bubble_layout.addWidget(self.activity_label)

        self.state_label = QLabel(message.state.value, self.bubble)
        self.state_label.setObjectName("messageState")
        bubble_layout.addWidget(self.state_label)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 1, 0, 0)
        actions.setSpacing(2)
        self.copy_button = self._action_button("Copy", "Copy message text")
        self.copy_button.clicked.connect(lambda: self.copy_requested.emit(self.message))
        actions.addWidget(self.copy_button)

        self.edit_button = self._action_button("Edit", "Edit this user message")
        self.edit_button.setEnabled(
            message.role is MessageRole.USER and message.state is MessageState.SENT
        )
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.message))
        actions.addWidget(self.edit_button)

        self.branch_button = self._action_button(
            "Branch",
            "Branch management is deferred; Edit and Regenerate create immutable siblings.",
        )
        self.branch_button.setEnabled(False)
        actions.addWidget(self.branch_button)

        self.more_button = QToolButton(self.bubble)
        self.more_button.setText("More")
        self.more_button.setToolTip("More supported message actions")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self.inspect_action = self.more_menu.addAction("Inspect details")
        self.inspect_action.triggered.connect(lambda: self.inspect_requested.emit(self.message))
        self.regenerate_action = self.more_menu.addAction("Regenerate")
        self.regenerate_action.setEnabled(
            message.role is MessageRole.ASSISTANT and message.state is not MessageState.STREAMING
        )
        self.regenerate_action.setToolTip(
            "Create a sibling assistant revision from the current user turn."
        )
        self.regenerate_action.triggered.connect(
            lambda: self.regenerate_requested.emit(self.message)
        )
        self.more_button.setMenu(self.more_menu)
        actions.addWidget(self.more_button)
        actions.addStretch(1)
        bubble_layout.addLayout(actions)

        if message.role is MessageRole.ASSISTANT:
            row_layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
            row_layout.addWidget(self.bubble, 0, Qt.AlignmentFlag.AlignLeft)
            row_layout.addStretch(1)
        else:
            row_layout.addStretch(1)
            row_layout.addWidget(self.bubble, 0, Qt.AlignmentFlag.AlignRight)
            row_layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

    @staticmethod
    def _action_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

    def set_generation_busy(self, busy: bool) -> None:
        self.edit_button.setEnabled(
            not busy
            and self.message.role is MessageRole.USER
            and self.message.state is MessageState.SENT
        )
        self.regenerate_action.setEnabled(
            not busy
            and self.message.role is MessageRole.ASSISTANT
            and self.message.state is not MessageState.STREAMING
        )
        self.set_generation_active(busy)

    def set_generation_active(self, active: bool) -> None:
        active = bool(
            active
            and self.message.role is MessageRole.ASSISTANT
            and self.message.state is MessageState.STREAMING
        )
        self.activity_label.setVisible(active)
        self.bubble.setProperty("generationActive", active)
        self.bubble.style().unpolish(self.bubble)
        self.bubble.style().polish(self.bubble)

    def set_bubble_max_width(self, width: int) -> None:
        width = max(240, width)
        if self.bubble.maximumWidth() != width:
            self.bubble.setMaximumWidth(width)


class TranscriptView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("transcriptView")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget(self)
        self.content.setObjectName("transcriptContent")
        self._layout = QVBoxLayout(self.content)
        self._layout.setContentsMargins(12, 9, 12, 12)
        self._layout.setSpacing(4)
        self.empty_label = QLabel("Start a conversation", self.content)
        self.empty_label.setObjectName("emptyTranscript")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch(1)
        self.setWidget(self.content)
        self.message_rows: dict[str, MessageRow] = {}
        self._bubble_cap: int | None = None

    def render(self, messages: Iterable[Message], generation_busy: bool = False) -> None:
        for row in tuple(self.message_rows.values()):
            self._layout.removeWidget(row)
            row.deleteLater()
        self.message_rows.clear()
        messages = tuple(messages)
        self.empty_label.setVisible(not messages)
        for message in messages:
            row = MessageRow(message, self.content)
            row.set_generation_busy(generation_busy)
            message_id = getattr(message, "id", f"message-{id(message)}")
            self.message_rows[message_id] = row
            self._layout.insertWidget(self._layout.count() - 1, row)
        self._resize_bubbles()
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _resize_bubbles(self) -> None:
        available = self.viewport().width() - 30
        cap = max(240, int(max(280, available) * 0.84))
        if cap == self._bubble_cap:
            return
        self._bubble_cap = cap
        for row in self.message_rows.values():
            row.set_bubble_max_width(cap)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_bubbles()

    def toPlainText(self) -> str:
        return "\n\n".join(row.message.content for row in self.message_rows.values())

    def isReadOnly(self) -> bool:
        return True


class InspectorPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorPanel")
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 12)
        self._layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._layout.setLabelAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setVerticalSpacing(8)
        self._layout.setHorizontalSpacing(10)
        self._show_chat_context(None, None, ())

    def _clear(self) -> None:
        while self._layout.rowCount():
            self._layout.removeRow(0)

    def _field(self, name: str, value: object) -> None:
        label = QLabel(str(value), self)
        label.setObjectName("inspectorValue")
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._layout.addRow(QLabel(name, self), label)

    def _show_chat_context(
        self,
        chat: Chat | None,
        message: Message | None,
        attempts: Iterable[GenerationAttempt],
        revision_count: int = 0,
    ) -> None:
        self._clear()
        if chat is None:
            self._field("Selection", "No chat selected")
            return
        self._field("Chat", chat.title)
        self._field("Chat ID", chat.id)
        self._field("Chat revision", chat.revision)
        self._field("Active head", chat.head_message_id or "none")
        if message is None:
            self._field("Message", "Select a message for details")
            return
        self._field("Role", message.role.value)
        self._field("State", message.state.value)
        self._field("Message ID", message.id)
        self._field("Sequence", message.sequence)
        self._field("Created", message.created_at.isoformat())
        self._field("Parent", message.parent_id or "none")
        self._field("Lineage", message.lineage_id or message.id)
        self._field("Revision", f"{message.revision} of {revision_count}")
        self._field("Supersedes", message.supersedes_id or "none")

        associated = tuple(
            attempt
            for attempt in attempts
            if attempt.assistant_message_id == message.id
            or attempt.user_message_id == message.id
        )
        if not associated:
            self._field("Generation", "No associated generation attempt")
            return
        for index, attempt in enumerate(associated, start=1):
            prefix = f"Generation {index}"
            self._field(f"{prefix} ID", attempt.id)
            self._field(f"{prefix} state", attempt.state.value)
            self._field(f"{prefix} backend", attempt.backend_id)
            self._field(f"{prefix} provider", attempt.provider_id or "none")
            self._field(f"{prefix} model", attempt.model)
            self._field(f"{prefix} returned model", attempt.returned_model or "unknown")
            self._field(f"{prefix} request ID", attempt.request_id or "unknown")
            self._field(f"{prefix} finish", attempt.finish_reason or "unknown")
            self._field(
                f"{prefix} uncertainty",
                (
                    "not recorded"
                    if attempt.remote_outcome_unknown is None
                    else "unknown"
                    if attempt.remote_outcome_unknown
                    else "known/not marked unknown"
                ),
            )
            self._field(f"{prefix} started", attempt.started_at.isoformat())
            self._field(
                f"{prefix} ended",
                attempt.ended_at.isoformat() if attempt.ended_at else "running",
            )
            self._field(f"{prefix} error", attempt.error_message or "none")
            self._field(
                f"{prefix} tokens",
                ", ".join(
                    f"{name}={value}"
                    for name, value in (
                        ("prompt", attempt.prompt_tokens),
                        ("completion", attempt.completion_tokens),
                        ("reasoning", attempt.reasoning_tokens),
                        ("total", attempt.total_tokens),
                    )
                    if value is not None
                )
                or "unknown",
            )
            self._field(
                f"{prefix} known cost",
                attempt.known_cost_usd if attempt.known_cost_usd is not None else "unknown",
            )
            self._field(f"{prefix} snapshot", self._snapshot_summary(attempt.request_snapshot))

    @staticmethod
    def _snapshot_summary(snapshot: str) -> str:
        try:
            parsed = json.loads(snapshot)
        except (TypeError, ValueError):
            return "present but not displayable"
        if not isinstance(parsed, dict):
            return "present but not an object"
        fields = [
            key
            for key in ("backend_id", "provider_id", "model", "base_url", "prompt")
            if key in parsed
        ]
        return "present; fields=" + ", ".join(fields)

    def show_chat(self, chat: Chat | None) -> None:
        self._show_chat_context(chat, None, ())

    def show_message(
        self,
        chat: Chat,
        message: Message,
        attempts: Iterable[GenerationAttempt],
        revision_count: int,
    ) -> None:
        self._show_chat_context(chat, message, attempts, revision_count)
