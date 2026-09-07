from __future__ import annotations

import asyncio
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QClipboard
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bots5.core.application import BotsApplication
from bots5.core.errors import RevisionConflict
from bots5.core.events import CoreEvent
from bots5.core.secrets import sanitize_secret_error
from bots5.domain.models import AttemptState, Chat, ChatActivity, Message, MessageRole, WorkspaceWindowState
from bots5.domain.provider import BackendType, CapabilityOverride, CapabilityState, CredentialSource, GenerationSettings, ProviderProfile
from bots5.providers.discovery import discoverer_for_connection

from .profile import DesktopSessionInfo
from .session import DesktopSessionController
from .theme import apply_draft1_theme
from .widgets import (
    AddConnectionDialog,
    ComposerEdit,
    InspectorPanel,
    LeftRail,
    MessageRow,
    SettingsDialog,
    TopBar,
    TranscriptView,
    TuneDialog,
)


_TERMINAL_EVENT_KINDS = frozenset(
    {
        "generation_completed",
        "generation_incomplete",
        "generation_failed",
        "generation_aborted",
    }
)


class MainWindow(QMainWindow):
    closed = Signal()
    new_window_requested = Signal()

    def __init__(
        self,
        application: BotsApplication,
        session: DesktopSessionInfo | None = None,
        workspace: DesktopSessionController | None = None,
        window_state: WorkspaceWindowState | None = None,
    ) -> None:
        super().__init__()
        self._application = application
        self._session = session or DesktopSessionInfo("fake", "fake-v0.1")
        self._workspace = workspace or DesktopSessionController(application, self._session)
        self._owns_workspace = workspace is None
        self._bridge = self._workspace.bridge
        self._window_state = window_state
        self._window_id = window_state.window_id if window_state is not None else None
        self._window_ordinal = window_state.ordinal if window_state is not None else None
        self._closing = False
        self._chat_ids: list[str] = []
        self._current_chat_id: str | None = None
        self._current_chat: Chat | None = None
        self._current_messages: tuple[Message, ...] = ()
        self._selected_message: Message | None = None
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._refresh_generation = 0
        self._active_attempt_id: str | None = None
        self._generation_busy = False
        self._editing_message_id: str | None = None
        self._editing_chat_id: str | None = None
        self._activity: dict[str, ChatActivity] = {}
        self._workspace_attached = True
        self._phase5 = getattr(application, "_configuration", None) is not None
        self._tune_dialog: TuneDialog | None = None
        self._tune_chat_id: str | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._add_connection_dialog: AddConnectionDialog | None = None
        self._phase5_refresh_lock = asyncio.Lock()
        self._last_phase5_event_sequence = 0

        self.setWindowTitle("B.O.T.S. 5")
        self.resize(1180, 760)
        application_instance = self._qt_application()
        if application_instance is not None:
            apply_draft1_theme(application_instance)
        self._build_ui()
        self._workspace.event_received.connect(self._on_event)
        self._workspace.activity_changed.connect(self._on_activity_changed)

    @staticmethod
    def _qt_application():
        from PySide6.QtWidgets import QApplication

        return QApplication.instance()

    def _build_ui(self) -> None:
        self.new_window_action = QAction("New Window", self)
        self.new_window_action.setShortcut("Ctrl+Shift+N")
        self.new_window_action.setToolTip("Open another window over this B.O.T.S. session")
        self.new_window_action.triggered.connect(
            lambda checked=False: self.new_window_requested.emit()
        )
        self.menuBar().addAction(self.new_window_action)

        root = QWidget(self)
        root.setObjectName("draft1Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.top_bar = TopBar(self._session, root, phase5=self._phase5)
        self.top_bar.rail_toggle_requested.connect(self._toggle_rail)
        self.top_bar.details_toggled.connect(self._toggle_inspector)
        self.top_bar.model_selected.connect(self._on_model_selected)
        self.top_bar.tune_requested.connect(self._on_tune_requested)
        self.top_bar.settings_requested.connect(self._on_settings_requested)
        root_layout.addWidget(self.top_bar)

        body = QWidget(root)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.rail = LeftRail(body)
        self.rail.new_chat_requested.connect(self._on_new_chat)
        self.rail.chat_indicator_requested.connect(self._on_chat_indicator_selected)
        self.rail.chat_list.currentRowChanged.connect(self._on_chat_selected)
        body_layout.addWidget(self.rail)
        self.chat_list = self.rail.chat_list
        self.new_chat_button = self.rail.new_chat_button

        workspace = QWidget(body)
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(18, 10, 18, 12)
        workspace_layout.setSpacing(8)

        self.chat_title = QLabel("New chat", workspace)
        self.chat_title.setObjectName("chatTitle")
        workspace_layout.addWidget(self.chat_title)

        self.transcript = TranscriptView(workspace)
        workspace_layout.addWidget(self.transcript, 1)

        self.composer_frame = QFrame(workspace)
        self.composer_frame.setObjectName("composerFrame")
        composer_layout = QVBoxLayout(self.composer_frame)
        composer_layout.setContentsMargins(10, 8, 10, 8)
        composer_layout.setSpacing(5)

        editing_row = QHBoxLayout()
        self.editing_label = QLabel("Editing message", self.composer_frame)
        self.editing_label.setObjectName("editingLabel")
        self.editing_label.setVisible(False)
        editing_row.addWidget(self.editing_label)
        self.cancel_edit_button = QToolButton(self.composer_frame)
        self.cancel_edit_button.setText("Cancel edit")
        self.cancel_edit_button.setToolTip("Return to composing a new message")
        self.cancel_edit_button.setVisible(False)
        self.cancel_edit_button.clicked.connect(self._clear_editing)
        editing_row.addWidget(self.cancel_edit_button)
        editing_row.addStretch(1)
        composer_layout.addLayout(editing_row)

        self.generation_indicator = QLabel("● Generating…", self.composer_frame)
        self.generation_indicator.setObjectName("generationIndicator")
        self.generation_indicator.setAccessibleName("Generation activity")
        self.generation_indicator.setToolTip("A response is being generated")
        self.generation_indicator.setVisible(False)
        composer_layout.addWidget(self.generation_indicator)

        composer_controls = QHBoxLayout()
        composer_controls.setSpacing(6)
        self.attachment_button = QToolButton(self.composer_frame)
        self.attachment_button.setObjectName("attachmentAffordance")
        self.attachment_button.setText("Attach")
        self.attachment_button.setToolTip("Attach a UTF-8 text file to the next message")
        self.attachment_button.clicked.connect(self._on_attach_file)
        composer_controls.addWidget(self.attachment_button)

        self.tool_button = self._disabled_composer_button(
            "Tools",
            "Tool invocation is not implemented in this UI slice.",
        )
        composer_controls.addWidget(self.tool_button)

        self.composer = ComposerEdit(self.composer_frame)
        self.composer.setPlaceholderText("Message")
        self.composer.setMinimumHeight(56)
        self.composer.setMaximumHeight(112)
        self.composer.send_requested.connect(self._on_send)
        self.composer.textChanged.connect(self._update_controls)
        composer_controls.addWidget(self.composer, 1)

        self.send_button = QPushButton("Send", self.composer_frame)
        self.send_button.setObjectName("sendButton")
        self.send_button.setToolTip("Send message (Enter)")
        self.send_button.clicked.connect(self._on_send)
        composer_controls.addWidget(self.send_button)

        self.cancel_button = QPushButton("Stop", self.composer_frame)
        self.cancel_button.setObjectName("stopButton")
        self.cancel_button.setToolTip("Stop the active generation")
        self.cancel_button.clicked.connect(self._on_cancel)
        composer_controls.addWidget(self.cancel_button)
        composer_layout.addLayout(composer_controls)
        workspace_layout.addWidget(self.composer_frame)
        body_layout.addWidget(workspace, 1)
        root_layout.addWidget(body, 1)
        self.setCentralWidget(root)

        self.inspector = InspectorPanel(self)
        self.inspector_dock = QDockWidget("Details", self)
        self.inspector_dock.setObjectName("inspectorDock")
        self.inspector_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.inspector_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.inspector_dock.setMinimumWidth(310)
        self.inspector_dock.setWidget(self.inspector)
        self.inspector_dock.visibilityChanged.connect(self._sync_inspector_button)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.inspector_dock.hide()
        self._update_controls()

    @staticmethod
    def _disabled_composer_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("disabledAffordance")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setEnabled(False)
        return button

    async def initialize(self) -> None:
        if self._window_id is None:
            self._window_id, self._window_ordinal = self._workspace.register_window(self)
        else:
            self._window_id, self._window_ordinal = self._workspace.register_window(
                self,
                window_id=self._window_id,
                ordinal=self._window_ordinal,
            )
        self._workspace.start()
        if self._window_state is not None:
            self.rail.set_collapsed(self._window_state.rail_collapsed)
            geometry = self._window_state.geometry
            if geometry is not None:
                self.setGeometry(*geometry)
        chats = await self._application.list_chats()
        if not chats:
            await self._application.create_chat()
            chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        if chats:
            selected = (
                self._window_state.selected_chat_id
                if self._window_state is not None
                else None
            )
            if selected not in {chat.id for chat in chats}:
                selected = chats[0].id
            self._current_chat_id = selected
            self._select_chat_row(selected)
            self._workspace.set_selected_chat(self._window_id, selected)
            await self._refresh_transcript(selected)
            await self._sync_current_activity()
        await self._refresh_phase5_state()
        await self._save_workspace()

    def _replace_chat_list(self, chats: tuple[Chat, ...] | list[Chat]) -> None:
        chats = tuple(chats)
        selected = self._current_chat_id
        self._chat_ids = [chat.id for chat in chats]
        self.rail.set_chats(chats, selected)
        if self._current_chat_id not in self._chat_ids:
            self._current_chat_id = self._chat_ids[0] if self._chat_ids else None
        self.rail.set_activity(self._activity, self._current_chat_id)
        self._update_controls()

    def _select_chat_row(self, chat_id: str | None) -> None:
        if chat_id is None or chat_id not in self._chat_ids:
            return
        self.rail.chat_list.setCurrentRow(self._chat_ids.index(chat_id))

    def _on_chat_selected(self, row: int) -> None:
        if 0 <= row < len(self._chat_ids):
            chat_id = self._chat_ids[row]
            if chat_id != self._current_chat_id:
                self._clear_editing()
            self._current_chat_id = chat_id
            self._selected_message = None
            if self._window_id is not None:
                self._workspace.set_selected_chat(self._window_id, chat_id)
            self.rail.set_activity(self._activity, chat_id)
            self._schedule(self._refresh_transcript(self._current_chat_id))
            self._schedule(self._refresh_pending_attachment_button(self._current_chat_id))
            self._schedule(self._refresh_phase5_state())
            self._schedule(self._sync_current_activity())
            self._schedule(self._save_workspace())

    def _on_chat_indicator_selected(self, chat_id: str) -> None:
        self._select_chat_row(chat_id)

    def _toggle_rail(self) -> None:
        self.rail.set_collapsed(not self.rail.collapsed)
        self._schedule(self._save_workspace())

    def _on_new_chat(self) -> None:
        self._clear_editing()
        self._schedule(self._create_chat())

    async def _create_chat(self) -> None:
        chat = await self._application.create_chat()
        chats = await self._application.list_chats()
        self._clear_editing()
        self._current_chat_id = chat.id
        self._replace_chat_list(chats)
        self._select_chat_row(chat.id)
        await self._refresh_transcript(chat.id)
        await self._refresh_phase5_state()

    async def _refresh_phase5_state(self) -> None:
        if not self._phase5:
            return
        async with self._phase5_refresh_lock:
            try:
                connections = await self._application.list_provider_connections()
                models = await self._application.list_model_catalogue()
                selection = (
                    None
                    if self._current_chat_id is None
                    else await self._application.chat_model_selection(self._current_chat_id)
                )
            except Exception as exc:
                self.statusBar().showMessage(str(exc))
                return
            connection_by_id = {connection.id: connection for connection in connections}
            entries = []
            selected_model = None
            for model in models:
                connection = connection_by_id.get(model.connection_id)
                connection_name = connection.name if connection is not None else "missing connection"
                state = model.availability.value
                label = f"{connection_name} / {model.provider_model_id} [{state}]"
                entries.append((label, model.id))
                if selection is not None and model.id == selection.model_entry_id:
                    selected_model = (connection, model)
            self.top_bar.set_models(entries, None if selection is None else selection.model_entry_id)
            if selection is None or selection.selection_required or selected_model is None:
                self.top_bar.model_pill.setText("Selection required")
                self.top_bar.model_pill.setToolTip("This chat has no durable current model selection.")
            else:
                connection, model = selected_model
                self.top_bar.model_pill.setText(model.provider_model_id)
                self.top_bar.model_pill.setToolTip(
                    f"{connection.name} / {model.provider_model_id} — {model.availability.value}"
                )
            await self._refresh_settings_dialog_unlocked()
            await self._refresh_tune_dialog_unlocked()

    def _on_model_selected(self, model_entry_id: str) -> None:
        if self._current_chat_id is not None:
            self._schedule(self._select_model(self._current_chat_id, model_entry_id))

    async def _select_model(self, chat_id: str, model_entry_id: str) -> None:
        try:
            selection = await self._application.chat_model_selection(chat_id)
            expected_revision = None if selection is None else selection.revision
            await self._application.select_model(chat_id, model_entry_id, expected_revision=expected_revision)
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            await self._refresh_phase5_state()

    def _on_tune_requested(self) -> None:
        if self._current_chat_id is not None:
            self._schedule(self._open_tune(self._current_chat_id))

    async def _open_tune(self, chat_id: str) -> None:
        try:
            selection = await self._application.chat_model_selection(chat_id)
            settings = await self._application.resolve_chat_generation_settings(chat_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        if (
            settings is None
            or selection is None
            or selection.selection_required
            or selection.model_entry_id is None
        ):
            self.statusBar().showMessage("Select a model before tuning this chat")
            return
        dialog = TuneDialog(self)
        override, override_revision = await self._application.chat_generation_settings_override_with_revision(chat_id)
        dialog.set_settings(
            settings.as_dict(),
            settings.provenance,
            timeout_override=None if override is None else override.timeout_seconds,
            override_revision=0 if override_revision is None else override_revision,
            model_entry_id=selection.model_entry_id,
        )
        dialog.save_requested.connect(lambda values: self._schedule(self._save_tune(chat_id, values)))
        dialog.inherit_requested.connect(lambda values: self._schedule(self._save_tune(chat_id, values)))
        self._tune_dialog = dialog
        self._tune_chat_id = chat_id
        dialog.show()

    async def _save_tune(self, chat_id: str, values: dict[str, object]) -> None:
        try:
            expected_revision = values.pop("expected_revision", None)
            expected_model_entry_id = values.pop("expected_model_entry_id", None)
            await self._application.set_chat_generation_settings(
                chat_id,
                GenerationSettings(**values),
                expected_revision=expected_revision,
                expected_model_entry_id=expected_model_entry_id,
            )
            self.statusBar().showMessage("Tune settings saved")
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    def _on_settings_requested(self) -> None:
        if not self._phase5:
            return
        dialog = SettingsDialog(self)
        dialog.add_connection_requested.connect(self._on_add_connection_requested)
        dialog.create_connection_requested.connect(lambda values: self._schedule(self._create_connection_from_settings(values)))
        dialog.edit_connection_requested.connect(lambda values: self._schedule(self._edit_connection_from_settings(values)))
        dialog.refresh_requested.connect(lambda connection_id: self._schedule(self._refresh_connection_from_settings(connection_id)))
        dialog.add_manual_model_requested.connect(lambda values: self._schedule(self._add_manual_model_from_settings(values)))
        dialog.model_defaults_refresh_requested.connect(lambda model_id: self._schedule(self._refresh_settings_model(model_id)))
        dialog.model_defaults_requested.connect(lambda values: self._schedule(self._save_model_defaults_from_settings(values)))
        dialog.capability_override_requested.connect(lambda values: self._schedule(self._save_capability_override_from_settings(values)))
        dialog.connection_enabled_requested.connect(lambda values: self._schedule(self._set_connection_enabled_from_settings(values)))
        dialog.connection_retire_requested.connect(lambda values: self._schedule(self._retire_connection_from_settings(values)))
        dialog.application_default_model_requested.connect(lambda values: self._schedule(self._save_application_default_model_from_settings(values)))
        dialog.application_defaults_requested.connect(lambda values: self._schedule(self._save_application_defaults_from_settings(values)))
        dialog.credential_save_requested.connect(lambda values: self._schedule(self._save_credential_from_settings(values)))
        dialog.credential_delete_requested.connect(lambda values: self._schedule(self._delete_credential_from_settings(values)))
        self._settings_dialog = dialog
        self._schedule(self._refresh_settings_dialog())
        dialog.show()

    def _on_add_connection_requested(self) -> None:
        if not self._phase5:
            return
        if self._add_connection_dialog is not None:
            self._add_connection_dialog.raise_()
            self._add_connection_dialog.activateWindow()
            return
        dialog = AddConnectionDialog(self)
        dialog.save_requested.connect(
            lambda values: self._schedule(self._create_connection_from_add(values, refresh=False))
        )
        dialog.save_refresh_requested.connect(
            lambda values: self._schedule(self._create_connection_from_add(values, refresh=True))
        )
        dialog.finished.connect(lambda _result: self._clear_add_connection_dialog(dialog))
        self._add_connection_dialog = dialog
        dialog.show()

    def _clear_add_connection_dialog(self, dialog: AddConnectionDialog) -> None:
        if self._add_connection_dialog is dialog:
            self._add_connection_dialog = None

    async def _refresh_settings_dialog(self) -> None:
        async with self._phase5_refresh_lock:
            await self._refresh_settings_dialog_unlocked()

    async def _refresh_settings_dialog_unlocked(self) -> None:
        if self._settings_dialog is None:
            return
        connections = await self._application.list_provider_connections()
        statuses = {}
        for connection in connections:
            status = await self._application.provider_credential_status(connection.id)
            if status is not None:
                statuses[connection.id] = status.status
        self._settings_dialog.set_connections(connections, statuses)
        models = await self._application.list_model_catalogue()
        self._settings_dialog.set_models(models, {connection.id: connection for connection in connections})
        current = self._settings_dialog.connection_list.currentItem()
        if current is not None:
            connection = next((item for item in connections if item.id == current.data(Qt.ItemDataRole.UserRole)), None)
            self._settings_dialog.set_connection_fields(connection)
        model_item = self._settings_dialog.model_list.currentItem()
        if model_item is not None:
            model_entry_id = str(model_item.data(Qt.ItemDataRole.UserRole))
            defaults, revision = await self._application.model_defaults_with_revision(model_entry_id)
            self._settings_dialog.set_model_defaults(defaults, revision)
            overrides = await self._application.capability_overrides(model_entry_id)
            self._settings_dialog.set_capability_overrides(overrides)
            capabilities = await self._application.model_capabilities(model_entry_id)
            self._settings_dialog.set_capabilities(capabilities)
        application_config = await self._application.application_generation_config()
        if application_config is not None:
            application_defaults, default_model_id, revision = application_config
            self._settings_dialog.set_application_defaults(
                application_defaults, revision, default_model_id
            )

    async def _refresh_settings_model(self, model_entry_id: str) -> None:
        async with self._phase5_refresh_lock:
            if self._settings_dialog is None:
                return
            current = self._settings_dialog.model_list.currentItem()
            if current is None or str(current.data(Qt.ItemDataRole.UserRole)) != model_entry_id:
                return
            defaults, revision = await self._application.model_defaults_with_revision(model_entry_id)
            self._settings_dialog.set_model_defaults(defaults, revision)
            overrides = await self._application.capability_overrides(model_entry_id)
            self._settings_dialog.set_capability_overrides(overrides)
            capabilities = await self._application.model_capabilities(model_entry_id)
            self._settings_dialog.set_capabilities(capabilities)

    async def _refresh_tune_dialog_unlocked(self) -> None:
        if self._tune_dialog is None or self._tune_chat_id is None:
            return
        try:
            selection = await self._application.chat_model_selection(self._tune_chat_id)
            settings = await self._application.resolve_chat_generation_settings(self._tune_chat_id)
            override, override_revision = await self._application.chat_generation_settings_override_with_revision(self._tune_chat_id)
        except Exception:
            return
        if (
            settings is None
            or selection is None
            or selection.selection_required
            or selection.model_entry_id is None
        ):
            return
        self._tune_dialog.set_settings(
            settings.as_dict(),
            settings.provenance,
            timeout_override=None if override is None else override.timeout_seconds,
            override_revision=0 if override_revision is None else override_revision,
            model_entry_id=selection.model_entry_id,
        )

    async def _create_connection_from_settings(self, values: dict[str, object]) -> None:
        try:
            await self._application.create_provider_connection(
                name=values["name"],
                backend_type=BackendType(values["backend_type"]),
                profile=ProviderProfile(values["profile"]),
                endpoint=values["endpoint"],
                credential_source=CredentialSource(values["credential_source"]),
                credential_reference=values["credential_reference"],
            )
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _create_connection_from_add(
        self,
        values: dict[str, object],
        *,
        refresh: bool,
    ) -> None:
        dialog = self._add_connection_dialog
        credential_value = values.pop("credential_value", None)
        created = None
        try:
            created = await self._application.create_provider_connection(
                name=values["name"],
                backend_type=BackendType(values["backend_type"]),
                profile=ProviderProfile(values["profile"]),
                endpoint=values["endpoint"],
                credential_source=CredentialSource(values["credential_source"]),
                credential_reference=values["credential_reference"],
            )
            if credential_value is not None:
                try:
                    credential_task = asyncio.create_task(
                        self._application.save_connection_credential(
                            created.id,
                            credential_value,
                            expected_revision=created.revision,
                            expected_credential_reference=created.credential_reference,
                        )
                    )
                    credential_value = None
                    status = await credential_task
                except Exception as exc:
                    sanitized = sanitize_secret_error(exc, None)
                    credential_value = None
                    raise RuntimeError(sanitized) from None
                self.statusBar().showMessage(
                    f"Connection saved; credential {status.status} in Secret Service"
                )
            else:
                self.statusBar().showMessage("Connection saved; provider was not contacted")
            if refresh:
                await self._application.refresh_models(
                    created.id,
                    discoverer_for_connection(created),
                )
                self.statusBar().showMessage("Connection saved and catalogue refreshed")
            if dialog is not None:
                dialog.submission_succeeded()
        except Exception as exc:
            detail = sanitize_secret_error(exc, credential_value if isinstance(credential_value, str) else None)
            if created is None:
                if dialog is not None:
                    dialog.submission_failed(detail)
            else:
                if dialog is not None:
                    dialog.submission_succeeded()
                detail = f"Connection saved, but the requested follow-up failed: {detail}"
            self.statusBar().showMessage(detail)
        finally:
            credential_value = None
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()

    async def _edit_connection_from_settings(self, values: dict[str, object]) -> None:
        try:
            connection = next(
                item for item in await self._application.list_provider_connections()
                if item.id == values["connection_id"]
            )
            expected_revision = int(values["revision"])
            if connection.revision != expected_revision:
                raise RevisionConflict("provider connection settings are stale")
            edited = replace(connection, name=values["name"], backend_type=BackendType(values["backend_type"]), profile=ProviderProfile(values["profile"]), endpoint=values["endpoint"], credential_source=CredentialSource(values["credential_source"]), credential_reference=values["credential_reference"])
            updated = await self._application.edit_provider_connection(edited, expected_revision=expected_revision)
            await self._refresh_connection_from_settings(updated.id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _refresh_connection_from_settings(self, connection_id: str) -> None:
        try:
            connection = next(item for item in await self._application.list_provider_connections() if item.id == connection_id)
            await self._application.refresh_models(connection_id, discoverer_for_connection(connection))
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _add_manual_model_from_settings(self, values: dict[str, object]) -> None:
        try:
            await self._application.add_manual_model(**values)
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _save_model_defaults_from_settings(self, values: dict[str, object]) -> None:
        try:
            expected_revision = values.pop("expected_revision", None)
            await self._application.set_model_defaults(
                str(values.pop("model_entry_id")),
                GenerationSettings(**values),
                expected_revision=expected_revision,
            )
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _save_capability_override_from_settings(self, values: dict[str, object]) -> None:
        try:
            expected_revision = values.get("expected_revision")
            revision = 1 if expected_revision is None else int(expected_revision) + 1
            await self._application.set_capability_override(
                CapabilityOverride(
                    model_entry_id=str(values["model_entry_id"]),
                    key=str(values["key"]),
                    state=CapabilityState(str(values["state"])),
                    value=values.get("value"),
                    reason=values.get("reason"),
                    revision=revision,
                ),
                expected_revision=expected_revision,
            )
            await self._refresh_settings_dialog()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _set_connection_enabled_from_settings(self, values: dict[str, object]) -> None:
        try:
            await self._application.set_provider_connection_enabled(
                str(values["connection_id"]),
                bool(values["enabled"]),
                expected_revision=int(values["revision"]),
            )
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _save_application_defaults_from_settings(self, values: dict[str, object]) -> None:
        try:
            expected_revision = values.pop("expected_revision", None)
            await self._application.set_application_generation_settings(
                GenerationSettings(**values), expected_revision=expected_revision
            )
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _save_credential_from_settings(self, values: dict[str, object]) -> None:
        credential_value = values.pop("value", None)
        try:
            credential_task = asyncio.create_task(
                self._application.save_connection_credential(
                    str(values["connection_id"]),
                    str(credential_value),
                    expected_revision=(
                        int(values["expected_revision"])
                        if values.get("expected_revision") is not None
                        else None
                    ),
                    expected_credential_reference=(
                        str(values["expected_credential_reference"])
                        if values.get("expected_credential_reference") is not None
                        else None
                    ),
                )
            )
            credential_value = None
            status = await credential_task
            self.statusBar().showMessage(f"Credential {status.status}; value was not retained by the UI")
            await self._refresh_settings_dialog()
        except Exception as exc:
            self.statusBar().showMessage(sanitize_secret_error(exc, None))
        finally:
            credential_value = None
            values.pop("value", None)

    async def _delete_credential_from_settings(self, values: dict[str, object]) -> None:
        try:
            status = await self._application.delete_connection_credential(
                str(values["connection_id"]),
                expected_revision=(
                    int(values["expected_revision"])
                    if values.get("expected_revision") is not None
                    else None
                ),
                expected_credential_reference=(
                    str(values["expected_credential_reference"])
                    if values.get("expected_credential_reference") is not None
                    else None
                ),
            )
            self.statusBar().showMessage(f"Credential {status.status}")
            await self._refresh_settings_dialog()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _retire_connection_from_settings(self, values: dict[str, object]) -> None:
        try:
            await self._application.retire_provider_connection(
                str(values["connection_id"]),
                expected_revision=int(values["revision"]),
                replacement_model_entry_id=(
                    values.get("replacement_model_entry_id")
                    if isinstance(values.get("replacement_model_entry_id"), str)
                    else None
                ),
            )
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _save_application_default_model_from_settings(self, values: dict[str, object]) -> None:
        try:
            await self._application.set_application_default_model(
                str(values["model_entry_id"]),
                expected_revision=(
                    None
                    if values.get("expected_revision") is None
                    else int(values["expected_revision"])
                ),
            )
            await self._refresh_settings_dialog()
            await self._refresh_phase5_state()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    def _on_attach_file(self) -> None:
        if self._generation_busy or self._current_chat_id is None:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Attach file",
            "",
            "Text or data files (*)",
        )
        if path:
            self._schedule(self._attach_file(path))

    async def _attach_file(self, path: str) -> None:
        chat_id = self._current_chat_id
        if chat_id is None:
            return
        try:
            attachment = await self._application.attach_file(path)
            await self._application.stage_attachment(chat_id, attachment.id)
            # Capture may finish after the user switches chats.  The staged
            # attachment remains associated with the chat that initiated the
            # command; the visible button must reflect the window's current
            # chat, queried after the asynchronous capture completes.
            current_chat_id = self._current_chat_id
            if current_chat_id is not None:
                await self._refresh_pending_attachment_button(current_chat_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    async def _refresh_pending_attachment_button(self, chat_id: str) -> None:
        try:
            pending = await self._application.pending_attachments(chat_id)
        except Exception:
            return
        self.attachment_button.setText("Attach" if not pending else f"Attach ({len(pending)})")
        self.attachment_button.setToolTip(
            "Attach a UTF-8 text file to the next message"
            if not pending
            else "Selected for the next message: " + ", ".join(item.filename for item in pending)
        )

    def _on_send(self) -> None:
        self._schedule(self._send_message())

    async def _send_message(self) -> None:
        if self._generation_busy or self._current_chat_id is None:
            return
        text = self.composer.toPlainText()
        if not text.strip():
            return
        chat_id = self._current_chat_id
        editing_message_id = self._valid_edit_target(chat_id)
        self._set_generation_busy(True)
        try:
            if editing_message_id is None:
                attempt = await self._application.send_message(chat_id, text)
            else:
                attempt = await self._application.edit_message(chat_id, editing_message_id, text)
        except Exception as exc:
            self._set_generation_busy(False)
            self.statusBar().showMessage(str(exc))
            return
        self.composer.clear()
        self._clear_editing()
        self._set_active_attempt(attempt.id, attempt.state)
        await self._refresh_attempt_state(chat_id, attempt.id)

    async def _refresh_attempt_state(self, chat_id: str, attempt_id: str) -> None:
        attempts = await self._application.list_generation_attempts(chat_id)
        current = next((attempt for attempt in attempts if attempt.id == attempt_id), None)
        if current is None or current.state is AttemptState.RUNNING:
            if chat_id == self._current_chat_id:
                self._set_generation_busy(True)
                self._active_attempt_id = attempt_id
        else:
            if chat_id == self._current_chat_id:
                self._active_attempt_id = None
                self._set_generation_busy(False)
        await self._refresh_transcript(chat_id)

    def _on_cancel(self) -> None:
        if self._active_attempt_id is not None:
            self._schedule(self._cancel_generation())

    async def _cancel_generation(self) -> None:
        attempt_id = self._active_attempt_id
        chat_id = self._current_chat_id
        if attempt_id is None or chat_id is None:
            return
        try:
            await self._application.cancel_generation(attempt_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
        finally:
            self._active_attempt_id = None
            self._set_generation_busy(False)
            await self._refresh_transcript(chat_id)

    def _on_copy_message(self, message: Message) -> None:
        application = self._qt_application()
        if application is not None:
            application.clipboard().setText(message.content, QClipboard.Mode.Clipboard)
        self.statusBar().showMessage("Message copied", 2000)

    def _on_edit_message(self, message: Message) -> None:
        if (
            self._generation_busy
            or self._current_chat_id != message.chat_id
            or message.role is not MessageRole.USER
            or message.state.value != "sent"
        ):
            return
        self._editing_message_id = message.id
        self._editing_chat_id = message.chat_id
        self.composer.setPlainText(message.content)
        self.editing_label.setText(f"Editing revision {message.revision}")
        self.editing_label.setVisible(True)
        self.cancel_edit_button.setVisible(True)
        self.composer.setFocus()
        self._update_controls()

    def _clear_editing(self) -> None:
        self._editing_message_id = None
        self._editing_chat_id = None
        self.editing_label.setVisible(False)
        self.cancel_edit_button.setVisible(False)
        self._update_controls()

    def _valid_edit_target(self, chat_id: str) -> str | None:
        message_id = self._editing_message_id
        if message_id is None:
            return None
        if self._editing_chat_id is not None and self._editing_chat_id != chat_id:
            self._clear_editing()
            return None
        target = next(
            (message for message in self._current_messages if message.id == message_id),
            None,
        )
        if (
            target is None
            or target.chat_id != chat_id
            or target.role is not MessageRole.USER
            or target.state.value != "sent"
        ):
            self._clear_editing()
            return None
        return message_id

    def _on_regenerate_message(self, message: Message) -> None:
        if self._current_chat_id is not None:
            self._schedule(self._regenerate_message(message))

    async def _regenerate_message(self, message: Message) -> None:
        if self._current_chat_id is None:
            return
        chat_id = self._current_chat_id
        self._set_generation_busy(True)
        try:
            attempt = await self._application.regenerate_message(chat_id, message.id)
        except Exception as exc:
            self._set_generation_busy(False)
            self.statusBar().showMessage(str(exc))
            return
        self._set_active_attempt(attempt.id, attempt.state)
        await self._refresh_attempt_state(chat_id, attempt.id)

    def _on_inspect_message(self, message: Message) -> None:
        self._selected_message = message
        self.top_bar.details_button.setChecked(True)
        self._schedule(self._refresh_inspector())

    def _set_active_attempt(self, attempt_id: str, state: AttemptState) -> None:
        if state is AttemptState.RUNNING:
            self._active_attempt_id = attempt_id
            self._set_generation_busy(True)
        else:
            self._active_attempt_id = None
            self._set_generation_busy(False)

    def _set_generation_busy(self, busy: bool) -> None:
        self._generation_busy = busy
        self.generation_indicator.setVisible(busy)
        self.composer.setReadOnly(busy)
        self.chat_list.setEnabled(True)
        self.new_chat_button.setEnabled(True)
        self.send_button.setEnabled(not busy and bool(self.composer.toPlainText().strip()))
        self.attachment_button.setEnabled(not busy and self._current_chat_id is not None)
        self.cancel_button.setEnabled(busy and self._active_attempt_id is not None)
        self.rail.chat_button.setEnabled(True)
        for row in self.transcript.message_rows.values():
            row.set_generation_busy(busy)

    def _update_controls(self) -> None:
        self.send_button.setEnabled(
            not self._generation_busy and bool(self.composer.toPlainText().strip())
        )
        self.attachment_button.setEnabled(not self._generation_busy and self._current_chat_id is not None)
        self.cancel_button.setEnabled(self._generation_busy and self._active_attempt_id is not None)

    def _on_event(self, event: CoreEvent) -> None:
        if not self._workspace_attached:
            return
        if not isinstance(event, CoreEvent):
            return
        chat_id = event.payload.get("chat_id")
        attempt_id = event.payload.get("attempt_id")
        if event.kind == "generation_started" and chat_id == self._current_chat_id:
            self._set_active_attempt(attempt_id, AttemptState.RUNNING)
        elif (
            event.kind in _TERMINAL_EVENT_KINDS
            and chat_id == self._current_chat_id
            and (attempt_id == self._active_attempt_id or self._generation_busy)
        ):
            self._active_attempt_id = None
            self._set_generation_busy(False)
        self._schedule(self._handle_event(event))

    def _on_activity_changed(self, chat_id: str, activity: ChatActivity) -> None:
        if not self._workspace_attached:
            return
        self._activity[chat_id] = activity
        self.rail.set_activity(self._activity, self._current_chat_id)
        if chat_id == self._current_chat_id:
            self._schedule(self._sync_current_activity())

    async def _sync_current_activity(self) -> None:
        chat_id = self._current_chat_id
        if chat_id is None:
            self._active_attempt_id = None
            self._set_generation_busy(False)
            return
        activity = self._workspace.activity_for(chat_id)
        if not activity.active_attempt_ids:
            try:
                activity = await self._application.chat_activity(chat_id)
            except Exception:
                activity = ChatActivity()
        self._activity[chat_id] = activity
        if activity.active_attempt_ids:
            self._active_attempt_id = activity.active_attempt_ids[0]
            self._set_generation_busy(True)
        else:
            self._active_attempt_id = None
            self._set_generation_busy(False)
        self.rail.set_activity(self._activity, chat_id)

    async def _handle_event(self, event: CoreEvent) -> None:
        chat_id = event.payload.get("chat_id")
        if event.kind == "chat_created":
            chats = await self._application.list_chats()
            self._replace_chat_list(chats)
        if event.kind == "pending_attachments_changed" and chat_id == self._current_chat_id:
            await self._refresh_pending_attachment_button(self._current_chat_id)
        if event.kind in {
            "chat_model_selection_changed",
            "provider_connection_changed",
            "model_catalogue_changed",
            "capability_changed",
            "provider_credential_status_changed",
            "application_default_model_changed",
            "application_generation_settings_changed",
            "model_generation_settings_changed",
            "chat_model_generation_settings_changed",
        }:
            if event.sequence <= self._last_phase5_event_sequence:
                return
            self._last_phase5_event_sequence = event.sequence
            await self._refresh_phase5_state()
        if chat_id == self._current_chat_id:
            await self._refresh_transcript(self._current_chat_id)

    async def _refresh_transcript(self, chat_id: str) -> None:
        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            chat, messages = await self._application.open_chat(chat_id)
        except Exception:
            return
        if generation != self._refresh_generation or chat_id != self._current_chat_id:
            return
        self._current_chat = chat
        self._current_messages = tuple(messages)
        if chat is not None:
            self.chat_title.setText(chat.title)
        self.transcript.render(messages, self._generation_busy)
        for row in self.transcript.message_rows.values():
            row.copy_requested.connect(self._on_copy_message)
            row.edit_requested.connect(self._on_edit_message)
            row.inspect_requested.connect(self._on_inspect_message)
            row.regenerate_requested.connect(self._on_regenerate_message)
        if self._selected_message is not None:
            self._selected_message = next(
                (message for message in self._current_messages if message.id == self._selected_message.id),
                None,
            )
        self._update_controls()
        if self.inspector_dock.isVisible():
            await self._refresh_inspector()

    async def _save_workspace(self) -> None:
        if self._window_id is None:
            return
        geometry = self.geometry()
        await self._workspace.save_window(
            window_id=self._window_id,
            geometry=(geometry.x(), geometry.y(), geometry.width(), geometry.height()),
            selected_chat_id=self._current_chat_id,
            rail_collapsed=self.rail.collapsed,
            restore_open=True,
        )

    async def _refresh_inspector(self) -> None:
        if self._current_chat is None:
            return
        if self._selected_message is None:
            self.inspector.show_chat(self._current_chat)
            return
        attempts = await self._application.list_generation_attempts(self._current_chat.id)
        revisions = await self._application.list_revisions(
            self._current_chat.id,
            self._selected_message.lineage_id or self._selected_message.id,
        )
        self.inspector.show_message(
            self._current_chat,
            self._selected_message,
            attempts,
            len(revisions),
        )

    def _toggle_inspector(self, visible: bool) -> None:
        self.inspector_dock.setVisible(visible)
        if visible:
            self._schedule(self._refresh_inspector())

    def _sync_inspector_button(self, visible: bool) -> None:
        if self.top_bar.details_button.isChecked() != visible:
            self.top_bar.details_button.blockSignals(True)
            self.top_bar.details_button.setChecked(visible)
            self.top_bar.details_button.blockSignals(False)
        if visible:
            self._schedule(self._refresh_inspector())

    def _schedule(self, coroutine) -> None:
        if not self._workspace_attached:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    def stop_bridge(self) -> None:
        self._detach_workspace()
        if self._owns_workspace:
            self._workspace.bridge.stop()
        for task in tuple(self._refresh_tasks):
            if not task.done():
                task.cancel()
        self._refresh_tasks.clear()

    def _detach_workspace(self) -> None:
        if not self._workspace_attached:
            return
        self._workspace_attached = False
        for signal, slot in (
            (self._workspace.event_received, self._on_event),
            (self._workspace.activity_changed, self._on_activity_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    async def stop_bridge_async(self) -> None:
        tasks = tuple(self._refresh_tasks)
        self.stop_bridge()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_workspace:
            if self._window_id is not None:
                await self._workspace.unregister_window(
                    self._window_id,
                    geometry=(
                        self.geometry().x(),
                        self.geometry().y(),
                        self.geometry().width(),
                        self.geometry().height(),
                    ),
                    selected_chat_id=self._current_chat_id,
                    rail_collapsed=self.rail.collapsed,
                )
                self._window_id = None
            await self._workspace.close()
        elif self._window_id is not None:
            await self._workspace.unregister_window(
                self._window_id,
                geometry=(
                    self.geometry().x(),
                    self.geometry().y(),
                    self.geometry().width(),
                    self.geometry().height(),
                ),
                selected_chat_id=self._current_chat_id,
                rail_collapsed=self.rail.collapsed,
            )
            self._window_id = None

    def closeEvent(self, event) -> None:
        if self._closing:
            event.accept()
            return
        if (
            self._window_id is not None
            and self._workspace.is_last_window(self._window_id)
            and self._application.has_active_generations()
        ):
            answer = QMessageBox.question(
                self,
                "Stop active generations?",
                "Active generations will be cancelled and their partial output preserved.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        event.accept()
        self._schedule(self._finish_close())

    async def _finish_close(self) -> None:
        self.stop_bridge()
        tasks = tuple(self._refresh_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._window_id is not None and not self._owns_workspace:
            final_window = self._workspace.is_last_window(self._window_id)
            await self._workspace.unregister_window(
                self._window_id,
                geometry=(
                    self.geometry().x(),
                    self.geometry().y(),
                    self.geometry().width(),
                    self.geometry().height(),
                ),
                selected_chat_id=self._current_chat_id,
                rail_collapsed=self.rail.collapsed,
                restore_open=final_window,
            )
            self._window_id = None
        elif self._window_id is not None:
            final_window = self._workspace.is_last_window(self._window_id)
            await self._workspace.unregister_window(
                self._window_id,
                geometry=(
                    self.geometry().x(),
                    self.geometry().y(),
                    self.geometry().width(),
                    self.geometry().height(),
                ),
                selected_chat_id=self._current_chat_id,
                rail_collapsed=self.rail.collapsed,
                restore_open=final_window,
            )
            self._window_id = None
            await self._workspace.close()
        self.closed.emit()
