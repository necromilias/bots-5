from __future__ import annotations

import asyncio
from dataclasses import replace

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QClipboard, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from bots5.core.application import BotsApplication
from bots5.core.errors import (
    RevisionConflict,
    SearchCursorStale,
    SearchError,
    SearchIndexInvalid,
    SearchInvalidQuery,
    SearchRebuilding,
    SearchResultGone,
    SearchStaleIndex,
    SearchUnavailable,
    StateError,
)
from bots5.core.events import CoreEvent
from bots5.core.secrets import sanitize_secret_error
from bots5.domain.models import (
    AttemptState,
    Chat,
    ChatActivity,
    Message,
    MessageRole,
    MessageState,
    WorkspaceWindowState,
)
from bots5.domain.provider import BackendType, CapabilityOverride, CapabilityState, CredentialSource, GenerationSettings, ProviderProfile
from bots5.domain.search import SearchDocumentKind, SearchFilters, SearchResult
from bots5.providers.discovery import discoverer_for_connection

from .dialog_primitives import ChamferedPanel, ContentFitLabel, ElidingLabel, scrollable, normalized_question, position_dialog_at_anchor
from .phase9 import Phase9DesktopController
from .phase9_queue_dock import ImportQueueDockWidget
from .profile import DesktopSessionInfo
from .session import DesktopSessionController
from .theme import MAIN_SIZE, PANEL_INSET, ROW_GAP, apply_draft1_theme, build_theme_stylesheet
from .widgets import (
    AddConnectionDialog,
    ComposerEdit,
    ContinuationBanner,
    DeleteChatConfirmationDialog,
    InspectorPanel,
    LeftRail,
    MessageRow,
    MoveToFolderDialog,
    SearchPanel,
    SettingsDialog,
    TopBar,
    TranscriptView,
    TuneDialog,
)
from .actions import ActionDefinition, ActionRegistry
from .campaign_dock import CampaignDockWidget
from .control_dock import ControlDockWidget
from .model_selector import ModelSelectorEntry, ModelSelectorPopup, model_readiness, readiness_details
from .icons import icon_action
from .window_chrome import NativeWindowEdges
from .palette import CommandPaletteDialog


_TERMINAL_EVENT_KINDS = frozenset(
    {
        "generation_completed",
        "generation_incomplete",
        "generation_failed",
        "generation_aborted",
    }
)

_SEARCH_SOURCE_EVENT_KINDS = frozenset(
    {
        "chat_created",
        "chat_archived",
        "chat_unarchived",
        "chat_title_changed",
        "chat_duplicated",
        # Phase 11 M3 (F7): deletions are search-visible source mutations.
        "message_deleted",
        "chat_deleted",
        "attachment_created",
        "attachment_removed",
        "message_sent",
        "message_revision_created",
        "generation_completed",
        "generation_incomplete",
        "generation_failed",
        "generation_aborted",
        "search_index_rebuilt",
    }
)


def clamp_geometry_to_available_screens(
    geometry: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Phase 11 M0a: clamp a restored window geometry on-screen.

    The restored position is brought into the UNION of every screen's
    available geometry while the user's stored width and height are
    PRESERVED (correcting the fence U-10 defect that discarded the stored
    size).  Only when the stored size itself cannot fit the union is it
    reduced to the union's size — there is no larger honest target.
    """
    x, y, width, height = (int(value) for value in geometry)
    screens = QGuiApplication.screens()
    if not screens:
        return (x, y, max(1, width), max(1, height))
    union = screens[0].availableGeometry()
    for screen in screens[1:]:
        union = union.united(screen.availableGeometry())
    width = max(1, min(width, union.width()))
    height = max(1, min(height, union.height()))
    x = min(max(x, union.left()), union.right() - width + 1)
    y = min(max(y, union.top()), union.bottom() - height + 1)
    return (x, y, width, height)


class MainWindow(QMainWindow):
    closed = Signal()
    new_window_requested = Signal()

    def __init__(
        self,
        application: BotsApplication,
        session: DesktopSessionInfo | None = None,
        workspace: DesktopSessionController | None = None,
        window_state: WorkspaceWindowState | None = None,
        *,
        handoff=None,
        campaign_bridge_factory=None,
        control_bridge_factory=None,
    ) -> None:
        super().__init__()
        self.setObjectName("botsMainWindow")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._application = application
        self._developer_provider_test_mode = (
            getattr(application, "developer_provider_test_mode", False) is True
        )
        self._session = session or DesktopSessionInfo("fake", "fake-v0.1")
        self._workspace = workspace or DesktopSessionController(application, self._session)
        self._owns_workspace = workspace is None
        self._bridge = self._workspace.bridge
        self._window_state = window_state
        # Slice E (workflow 8, F-05): the runtime-owned restore handoff
        # capability, injected keyword-only by DesktopRuntime.open_window.
        # ``None`` (any existing construction without the argument) leaves
        # the restore action inert; no module-level singleton exists.
        self._restore_handoff = handoff
        self._window_id = window_state.window_id if window_state is not None else None
        self._window_ordinal = window_state.ordinal if window_state is not None else None
        self._closing = False
        self._chat_ids: list[str] = []
        self._rail_chats: tuple[Chat, ...] = ()
        self._current_chat_id: str | None = None
        self._current_chat: Chat | None = None
        self._current_messages: tuple[Message, ...] = ()
        self._historical_leaf_message_id: str | None = None
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
        self._model_known_blocked = False
        self._tune_dialog: TuneDialog | None = None
        self._tune_chat_id: str | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._add_connection_dialog: AddConnectionDialog | None = None
        # Phase 11 F8: rich model-selector presentation state (session-only).
        self._model_selector_popup: ModelSelectorPopup | None = None
        self._model_selector_records: list[ModelSelectorEntry] = []
        self._model_selector_selected_id: str | None = None
        # Phase 11 M1: Action registry and palette
        self._action_registry: ActionRegistry = ActionRegistry()
        self._palette_dialog: CommandPaletteDialog | None = None
        self._phase5_refresh_lock = asyncio.Lock()
        self._last_phase5_event_sequence = 0
        self._last_search_query: str | None = None
        self._last_search_filters: SearchFilters | None = None
        self._next_search_cursor: str | None = None
        self._search_busy = False
        # Phase 10 M2.0b: optional campaign dock
        self._campaign_bridge_factory = campaign_bridge_factory
        self._campaign_dock: CampaignDockWidget | None = None
        # v0.2: optional control plane dock
        self._control_bridge_factory = control_bridge_factory
        self._control_dock: ControlDockWidget | None = None
        # Phase 11 M4b/M6: workspace/settings plane wiring state.
        self._applying_dock_layout = False
        self._qaction_by_action_id: dict[str, QAction] = {}
        self._default_shortcut_by_action_id: dict[str, str] = {}

        self.setWindowTitle("B.O.T.S. 5")
        from .application_icon import application_icon
        self.setWindowIcon(application_icon())
        if self._developer_provider_test_mode:
            self.setWindowTitle("B.O.T.S. 5 — INEXACT / PROVIDER TEST MODE")
        self.resize(*MAIN_SIZE)
        # Additive Phase 9 delegation controller (task orchestration only);
        # created before _build_ui so the docks and menus can attach to it.
        self._phase9 = Phase9DesktopController(
            application,
            parent=self,
            notify=lambda message: self.statusBar().showMessage(message, 5000),
        )
        application_instance = self._qt_application()
        if application_instance is not None:
            apply_draft1_theme(application_instance)
        self._build_ui()
        self._native_edges = NativeWindowEdges(self)
        self._workspace.event_received.connect(self._on_event)
        self._workspace.activity_changed.connect(self._on_activity_changed)

    @staticmethod
    def _qt_application():
        from PySide6.QtWidgets import QApplication

        return QApplication.instance()

    def _build_ui(self) -> None:
        # Phase 11 M1: Register standard actions
        self._register_standard_actions()

        self.new_window_action = QAction("New Window", self)
        self.new_window_action.setShortcut("Ctrl+Shift+N")
        self.new_window_action.setToolTip("Open another window over this B.O.T.S. session")
        self.new_window_action.triggered.connect(
            lambda checked=False: self.new_window_requested.emit()
        )
        self.menuBar().addAction(self.new_window_action)

        self.global_search_action = QAction("Search", self)
        self.global_search_action.setShortcut("Ctrl+K")
        self.global_search_action.triggered.connect(self._open_global_search)
        self.menuBar().addAction(self.global_search_action)

        self.in_chat_search_action = QAction("Search Current Chat", self)
        self.in_chat_search_action.setShortcut("Ctrl+F")
        self.in_chat_search_action.triggered.connect(self._open_in_chat_search)
        self.menuBar().addAction(self.in_chat_search_action)

        # Phase 11 M1: Command palette
        self.command_palette_action = QAction("Command Palette", self)
        self.command_palette_action.setShortcut("Ctrl+Shift+P")
        self.command_palette_action.triggered.connect(self._open_command_palette)
        self.menuBar().addAction(self.command_palette_action)

        root = QWidget(self)
        root.setObjectName("draft1Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # The global console lives above QMainWindow's dock region. Keeping it
        # in the central widget lets an open Details dock steal its width.
        self.console_header = QWidget(self)
        self.console_header.setObjectName("consoleHeader")
        self.console_header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        console_layout = QVBoxLayout(self.console_header)
        console_layout.setContentsMargins(6, 4, 6, 6)
        console_layout.setSpacing(4)

        self.provider_test_banner = ContentFitLabel(
            "INEXACT / PROVIDER TEST MODE — developer-only; not Phase 6 compliant.\n"
            "Current message only. Deterministic context budgeting, history and attachment guarantees are unavailable.",
            self.console_header,
        )
        self.provider_test_banner.setObjectName("developerProviderTestBanner")
        self.provider_test_banner.setWordWrap(True)
        self.provider_test_banner.setVisible(self._developer_provider_test_mode)
        console_layout.addWidget(self.provider_test_banner)

        self.top_bar = TopBar(self._session, self.console_header, phase5=self._phase5)
        self.top_bar.rail_toggle_requested.connect(self._toggle_rail)
        self.top_bar.search_toggled.connect(self._toggle_search)
        self.top_bar.details_toggled.connect(self._toggle_inspector)
        self.top_bar.model_selected.connect(self._on_model_selected)
        self.top_bar.tune_requested.connect(self._on_tune_requested)
        self.top_bar.settings_requested.connect(self._on_settings_requested)
        self.top_bar.move_requested.connect(self._start_system_move)
        self.top_bar.minimize_requested.connect(self.showMinimized)
        self.top_bar.maximize_requested.connect(self._toggle_window_maximized)
        self.top_bar.close_requested.connect(self.close)
        # Phase 11 F8: the selector button only requests the popup; the
        # durable selection still runs through the landed command path.
        self.top_bar.model_selector.open_requested.connect(self._open_model_selector)
        console_layout.addWidget(self.top_bar)
        self.console_toolbar = QToolBar(self)
        self.console_toolbar.setObjectName("consoleToolbar")
        self.console_toolbar.setMovable(False)
        self.console_toolbar.setFloatable(False)
        self.console_toolbar.setAllowedAreas(Qt.ToolBarArea.TopToolBarArea)
        # This global navigation and permanent test-mode notice are not an
        # optional tool palette. Disable Qt's default context-menu hide action.
        self.console_toolbar.toggleViewAction().setEnabled(False)
        self.console_toolbar.addWidget(self.console_header)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.console_toolbar)

        body = QWidget(root)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(6, 0, 6, 6)
        body_layout.setSpacing(6)

        self.rail = LeftRail(body)
        self.rail.new_chat_requested.connect(self._on_new_chat)
        self.rail.chat_indicator_requested.connect(self._on_chat_indicator_selected)
        self.rail.chat_list.currentRowChanged.connect(self._on_chat_selected)
        body_layout.addWidget(self.rail)
        self.chat_list = self.rail.chat_list
        self.new_chat_button = self.rail.new_chat_button

        workspace = ChamferedPanel(body, chamfer=8)
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(4, 4, 4, 4)
        workspace_layout.setSpacing(0)

        self.chat_header = QFrame(workspace)
        self.chat_header.setObjectName("conversationHeader")
        self.chat_header.setMinimumHeight(40)
        chat_header = QHBoxLayout(self.chat_header)
        chat_header.setContentsMargins(12, 5, 10, 5)
        self.chat_title = ElidingLabel("New chat", self.chat_header)
        self.chat_title.setObjectName("chatTitle")
        chat_header.addWidget(self.chat_title, 1)
        self.archived_badge = QLabel("Archived", workspace)
        self.archived_badge.setObjectName("archivedBadge")
        self.archived_badge.setVisible(False)
        chat_header.addWidget(self.archived_badge)
        chat_header.addStretch(1)
        self.archive_button = QToolButton(workspace)
        self.archive_button.setObjectName("archiveChatButton")
        self.archive_button.setText("Archive")
        self.archive_button.setToolTip("Archive this chat")
        self.archive_button.clicked.connect(self._on_archive_chat)
        chat_header.addWidget(self.archive_button)
        workspace_layout.addWidget(self.chat_header)

        self.historical_banner = QFrame(workspace)
        self.historical_banner.setObjectName("historicalBanner")
        historical_layout = QHBoxLayout(self.historical_banner)
        historical_layout.setContentsMargins(9, 6, 9, 6)
        self.historical_label = QLabel(
            "Historical branch — the authoritative active head is unchanged.",
            self.historical_banner,
        )
        self.historical_label.setObjectName("historicalViewLabel")
        self.historical_label.setWordWrap(True)
        historical_layout.addWidget(self.historical_label, 1)
        self.return_active_button = QPushButton("Return to active branch", self.historical_banner)
        self.return_active_button.setObjectName("returnActiveBranchButton")
        self.return_active_button.clicked.connect(self._on_return_active_branch)
        historical_layout.addWidget(self.return_active_button)
        self.historical_banner.setVisible(False)
        workspace_layout.addWidget(self.historical_banner)

        self.transcript = TranscriptView(workspace, application=self._application)
        workspace_layout.addWidget(self.transcript, 1)

        # Additive Phase 9 workflow-5 seam: visible only while an imported
        # continuation resolution is pending; it is presentation only and
        # carries no commands (I1).
        self.continuation_banner = ContinuationBanner(workspace)
        workspace_layout.addWidget(self.continuation_banner)

        self.composer_frame = ChamferedPanel(workspace, chamfer=8)
        self.composer_frame.setObjectName("composerFrame")
        composer_layout = QVBoxLayout(self.composer_frame)
        composer_layout.setContentsMargins(10, 4, 10, 5)
        composer_layout.setSpacing(2)

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
        icon_action(self.attachment_button, "attach", "Attach file")
        self.attachment_button.setToolTip("Attach a UTF-8 text file to the next message")
        if self._developer_provider_test_mode:
            self.attachment_button.setToolTip("Attachments are unavailable in INEXACT / PROVIDER TEST MODE")
        self.attachment_button.clicked.connect(self._on_attach_file)
        composer_controls.addWidget(self.attachment_button)

        self.tool_button = self._disabled_composer_button(
            "Tools",
            "Tool invocation is not implemented in this UI slice.",
        )
        icon_action(self.tool_button, "tools", "Tools (not available)")
        composer_controls.addWidget(self.tool_button)
        self.model_readiness_label = QLabel("", self.composer_frame)
        self.model_readiness_label.setObjectName("composerReadiness")
        self.model_readiness_label.setWordWrap(True)
        self.model_readiness_label.setMinimumWidth(0)
        self.model_readiness_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        composer_controls.addWidget(self.model_readiness_label, 1)
        self.model_recovery_button = QToolButton(self.composer_frame)
        self.model_recovery_button.setText("View connection")
        self.model_recovery_button.clicked.connect(self._on_model_recovery)
        self.model_recovery_button.setVisible(False)
        composer_controls.addWidget(self.model_recovery_button)

        self.composer = ComposerEdit(self.composer_frame)
        self.composer.setPlaceholderText("Message")
        self.composer.setMinimumHeight(52)
        self.composer.setMaximumHeight(60)
        self.composer.send_requested.connect(self._on_send)
        self.composer.textChanged.connect(self._update_controls)
        composer_layout.addWidget(self.composer)

        self.send_button = QPushButton("Send", self.composer_frame)
        self.send_button.setObjectName("sendButton")
        icon_action(self.send_button, "send", "Send message")
        self.send_button.setToolTip("Send message (Enter)")
        self.send_button.clicked.connect(self._on_send)
        composer_controls.addWidget(self.send_button)

        self.cancel_button = QPushButton("Stop", self.composer_frame)
        self.cancel_button.setObjectName("stopButton")
        icon_action(self.cancel_button, "stop", "Stop generation")
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
        self.inspector_dock.setMinimumWidth(180)
        self.inspector_dock.setTitleBarWidget(self._dock_heading("Details", self.inspector_dock))
        inspector_frame = ChamferedPanel(self.inspector_dock, chamfer=8)
        inspector_layout = QVBoxLayout(inspector_frame)
        inspector_layout.setContentsMargins(4, 4, 4, 4)
        inspector_layout.addWidget(scrollable(self.inspector, inspector_frame))
        self.inspector_dock.setWidget(inspector_frame)
        self.inspector_dock.visibilityChanged.connect(self._sync_inspector_button)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.resizeDocks([self.inspector_dock], [310], Qt.Orientation.Horizontal)
        self.inspector_dock.hide()

        self.search_panel = SearchPanel(self)
        self.search_panel.search_requested.connect(self._on_search_requested)
        self.search_panel.load_more_requested.connect(self._on_load_more_search)
        self.search_panel.result_requested.connect(self._on_search_result_requested)
        self.search_panel.rebuild_requested.connect(self._on_rebuild_search)
        self.search_dock = QDockWidget("Search", self)
        self.search_dock.setObjectName("searchDock")
        self.search_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.search_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.search_dock.setMinimumWidth(220)
        self.search_dock.setTitleBarWidget(self._dock_heading("Search", self.search_dock))
        search_frame = ChamferedPanel(self.search_dock, chamfer=8)
        search_layout = QVBoxLayout(search_frame)
        search_layout.setContentsMargins(4, 4, 4, 4)
        search_layout.addWidget(self.search_panel)
        self.search_dock.setWidget(search_frame)
        self.search_dock.visibilityChanged.connect(self._sync_search_button)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.search_dock)
        self.search_dock.hide()

        # Additive Phase 9 import-queue dock (hidden until the View entry or a
        # workflow opens it; it never calls the application while hidden).
        self.import_queue_dock = ImportQueueDockWidget(self)
        self.import_queue_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.import_queue_dock)
        self.import_queue_dock.hide()
        self._phase9.attach_queue_dock(self.import_queue_dock)

        # Phase 11 M4b: dock layout changes persist the QMainWindow.saveState()
        # blob (advisory, Qt-version-tied) through the workspace plane.
        for dock in (self.search_dock, self.inspector_dock, self.import_queue_dock):
            dock.visibilityChanged.connect(self._on_dock_layout_changed)
            dock.dockLocationChanged.connect(self._on_dock_layout_changed)
            dock.topLevelChanged.connect(self._on_dock_layout_changed)

        # Phase 10 M2.0b: optional campaign dock
        if self._campaign_bridge_factory is not None:
            self._campaign_dock = CampaignDockWidget(
                self, bridge_factory=self._campaign_bridge_factory
            )
            self._campaign_dock.setAllowedAreas(
                Qt.DockWidgetArea.BottomDockWidgetArea
                | Qt.DockWidgetArea.LeftDockWidgetArea
                | Qt.DockWidgetArea.RightDockWidgetArea
            )
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._campaign_dock)
            self._campaign_dock.hide()
            self._campaign_dock.visibilityChanged.connect(self._sync_campaign_dock_button)
            self._campaign_dock.visibilityChanged.connect(self._on_dock_layout_changed)

        # v0.2: optional control plane dock
        if self._control_bridge_factory is not None:
            self._control_dock = ControlDockWidget(
                self, bridge_factory=self._control_bridge_factory
            )
            self._control_dock.setAllowedAreas(
                Qt.DockWidgetArea.BottomDockWidgetArea
                | Qt.DockWidgetArea.LeftDockWidgetArea
                | Qt.DockWidgetArea.RightDockWidgetArea
            )
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._control_dock)
            self._control_dock.hide()
            self._control_dock.visibilityChanged.connect(self._sync_control_dock_button)
            self._control_dock.visibilityChanged.connect(self._on_dock_layout_changed)

        self._build_phase9_menus()
        self._native_menu_bar = super().menuBar()
        def keep_shortcuts_reachable(actions):
            for action in actions:
                self.addAction(action)
                if action.menu() is not None:
                    keep_shortcuts_reachable(action.menu().actions())
        keep_shortcuts_reachable(self._native_menu_bar.actions())
        self._console_menu_bar = self.top_bar.install_menu_actions(tuple(self._native_menu_bar.actions()), self.top_bar.utilities)
        self._native_menu_bar.hide()
        self._update_controls()
        self._fit_workspace_panels()

        # Phase 11 M1: Detect and report any shortcut conflicts
        conflicts = self._action_registry.detect_conflicts()
        if conflicts:
            # Log conflicts for now; in a future iteration these could be surfaced
            # to the operator via a warning dialog
            for shortcut, actions in conflicts.items():
                action_titles = ", ".join(a.title for a in actions)
                print(f"WARNING: Shortcut conflict on '{shortcut}': {action_titles}")

        # Phase 11 M4b/M6: map registry action ids onto the real QActions that
        # carry the user-visible shortcut, remembering each default so a reset
        # can restore it. Overrides are applied later, at initialize().
        self._qaction_by_action_id = {
            "chat.new": self.new_window_action,
            "view.global_search": self.global_search_action,
            "view.search_current_chat": self.in_chat_search_action,
            "palette.open": self.command_palette_action,
        }
        self._default_shortcut_by_action_id = {
            action_id: qaction.shortcut().toString()
            for action_id, qaction in self._qaction_by_action_id.items()
        }

    def menuBar(self):
        # Public callers keep the actual visible menu and the original action
        # identities after navigation moves into the full-width console.
        if hasattr(self, "_console_menu_bar"):
            return self._console_menu_bar
        return super().menuBar()

    def _dock_heading(self, title: str, dock: QDockWidget) -> QWidget:
        heading = QFrame(dock)
        heading.setObjectName("utilityHeading")
        heading.setMinimumHeight(40)
        layout = QHBoxLayout(heading)
        layout.setContentsMargins(10, 5, 8, 5)
        layout.addWidget(QLabel(title, heading), 1)
        close = QToolButton(heading)
        close.setText("×")
        close.setAccessibleName("Close " + title)
        close.setToolTip("Close " + title)
        close.clicked.connect(dock.close)
        layout.addWidget(close)
        return heading

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "search_dock"):
            self._fit_workspace_panels()

    def _start_system_move(self) -> None:
        handle = self.windowHandle()
        accepted = bool(handle is not None and handle.startSystemMove())
        self._last_system_chrome_operation = ("move", None, accepted)

    def _toggle_window_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "top_bar"):
            self.top_bar.set_window_maximized(self.isMaximized())

    def _fit_workspace_panels(self) -> None:
        # Width adaptation is presentation-only: it does not overwrite the
        # operator's persisted rail preference.
        if not hasattr(self, "search_dock"):
            return
        side_open = not self.inspector_dock.isHidden() or not self.search_dock.isHidden()
        self.rail.set_compact_for_width(self.width() < (1180 if side_open else 900))
        self.top_bar.set_rail_collapsed(self.rail.effective_collapsed)
        narrow = self.width() < 1180
        if narrow != getattr(self, "_narrow_workspace", None):
            self._narrow_workspace = narrow
            self.resizeDocks([self.inspector_dock], [270 if narrow else 300], Qt.Orientation.Horizontal)

    def _register_standard_actions(self) -> None:
        """Register Phase 11 M1 standard actions with the action registry.

        This creates the single declarative source of truth for palette/menu/
        shortcut metadata. Each action defines id, title, category, default
        shortcut, handler, and optional enabled predicate.
        """
        # Chat actions
        self._action_registry.register(
            ActionDefinition(
                action_id="chat.new",
                title="New Chat",
                category="chat",
                default_shortcut="Ctrl+Shift+N",
                handler=lambda checked=False: self.new_window_requested.emit(),
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="chat.create",
                title="Create Chat",
                category="chat",
                default_shortcut="",
                handler=lambda checked=False: self._on_new_chat(),
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="chat.archive",
                title="Archive Chat",
                category="chat",
                default_shortcut="",
                handler=lambda checked=False: self._on_archive_chat(),
                is_enabled=lambda: self._current_chat_id is not None,
            )
        )

        # Phase 11 M3 (F5/F7): pin and delete join the palette so the
        # keyboard-first surface reaches the organisation actions too.
        if hasattr(self._application, "set_chat_pinned"):
            self._action_registry.register(
                ActionDefinition(
                    action_id="chat.pin",
                    title="Pin/Unpin Chat",
                    category="chat",
                    default_shortcut="",
                    handler=self._on_palette_pin_current_chat,
                    is_enabled=lambda: self._current_chat_id is not None,
                )
            )
        if hasattr(self._application, "delete_chat"):
            self._action_registry.register(
                ActionDefinition(
                    action_id="chat.delete",
                    title="Delete Chat…",
                    category="chat",
                    default_shortcut="",
                    handler=self._on_palette_delete_current_chat,
                    is_enabled=lambda: self._current_chat_id is not None,
                )
            )

        # View actions
        self._action_registry.register(
            ActionDefinition(
                action_id="view.global_search",
                title="Global Search",
                category="view",
                default_shortcut="Ctrl+K",
                handler=lambda checked=False: self._open_global_search(),
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="view.search_current_chat",
                title="Search Current Chat",
                category="view",
                default_shortcut="Ctrl+F",
                handler=lambda checked=False: self._open_in_chat_search(),
                is_enabled=lambda: self._current_chat_id is not None,
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="view.toggle_rail",
                title="Toggle Rail",
                category="view",
                default_shortcut="",
                handler=lambda checked=False: self._toggle_rail(),
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="view.toggle_inspector",
                title="Toggle Inspector",
                category="view",
                default_shortcut="",
                handler=lambda checked=False: self._toggle_inspector(),
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="view.toggle_search",
                title="Toggle Search Panel",
                category="view",
                default_shortcut="",
                handler=lambda checked=False: self._toggle_search(not self.search_dock.isVisible()),
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="view.toggle_campaign_dock",
                title="Toggle Campaign Dock",
                category="view",
                default_shortcut="",
                handler=lambda checked=False: self._show_campaign_dock(not self._campaign_dock.isVisible() if self._campaign_dock else False),
                is_enabled=lambda: self._campaign_dock is not None,
            )
        )

        self._action_registry.register(
            ActionDefinition(
                action_id="view.toggle_control_dock",
                title="Toggle Control Plane Dock",
                category="view",
                default_shortcut="",
                handler=lambda checked=False: self._show_control_dock(not self._control_dock.isVisible() if self._control_dock else False),
                is_enabled=lambda: self._control_dock is not None,
            )
        )

        # Palette action
        self._action_registry.register(
            ActionDefinition(
                action_id="palette.open",
                title="Command Palette",
                category="palette",
                default_shortcut="Ctrl+Shift+P",
                handler=lambda checked=False: self._open_command_palette(),
            )
        )

    def _open_command_palette(self) -> None:
        """Open the command palette dialog."""
        if self._palette_dialog is None:
            self._palette_dialog = CommandPaletteDialog(self._action_registry, self)
        
        action_id = CommandPaletteDialog.show_palette(self._action_registry, self)
        if action_id:
            # Dispatch to the registered handler
            action = self._action_registry.get_action(action_id)
            if action and action.handler:
                try:
                    if action.is_enabled is None or action.is_enabled():
                        action.handler()
                except Exception as exc:
                    self.statusBar().showMessage(f"Action '{action.title}' failed: {exc}")

    def _sync_campaign_dock_button(self, visible: bool) -> None:
        if self.top_bar is not None and hasattr(self, "campaign_dock_action"):
            if self.campaign_dock_action.isChecked() != visible:
                self.campaign_dock_action.blockSignals(True)
                self.campaign_dock_action.setChecked(visible)
                self.campaign_dock_action.blockSignals(False)

    def _sync_control_dock_button(self, visible: bool) -> None:
        if self.top_bar is not None and hasattr(self, "control_dock_action"):
            if self.control_dock_action.isChecked() != visible:
                self.control_dock_action.blockSignals(True)
                self.control_dock_action.setChecked(visible)
                self.control_dock_action.blockSignals(False)

    def _build_phase9_menus(self) -> None:
        """Additive Phase 9 wiring: File/View menu entries and rail context actions.

        Everything here delegates to Phase9DesktopController; the window keeps
        no product semantics and no existing action or shutdown path changes.
        """

        file_menu = self.menuBar().addMenu("File")
        self.export_transcript_action = QAction("Export Transcript…", self)
        self.export_transcript_action.setObjectName("actionExportTranscript")
        self.export_transcript_action.triggered.connect(
            self._on_export_transcript_requested
        )
        file_menu.addAction(self.export_transcript_action)
        self.export_archive_action = QAction("Export Archive…", self)
        self.export_archive_action.setObjectName("actionExportArchive")
        self.export_archive_action.triggered.connect(
            self._on_export_archive_requested
        )
        file_menu.addAction(self.export_archive_action)
        self.import_archive_action = QAction("Import Archive…", self)
        self.import_archive_action.setObjectName("actionImportArchive")
        self.import_archive_action.triggered.connect(
            self._on_import_archive_requested
        )
        file_menu.addAction(self.import_archive_action)

        view_menu = self.menuBar().addMenu("View")
        self.import_queue_action = QAction("Import Queue", self)
        self.import_queue_action.setObjectName("actionShowImportQueue")
        self.import_queue_action.triggered.connect(self._show_import_queue)
        view_menu.addAction(self.import_queue_action)

        # Phase 10 M2.0b: campaign dock toggle.  Only present when a campaign
        # bridge factory was supplied; without one the window keeps exactly its
        # pre-Phase-10 View menu (no inert entry is added).
        if self._campaign_dock is not None:
            self.campaign_dock_action = QAction("Campaign", self)
            self.campaign_dock_action.setObjectName("actionShowCampaignDock")
            self.campaign_dock_action.setCheckable(True)
            self.campaign_dock_action.triggered.connect(self._show_campaign_dock)
            view_menu.addAction(self.campaign_dock_action)

        # v0.2: control plane dock toggle. Only present when a control
        # bridge factory was supplied; without one the window keeps exactly
        # its pre-v0.2 View menu (no inert entry is added).
        if self._control_dock is not None:
            self.control_dock_action = QAction("Control Plane", self)
            self.control_dock_action.setObjectName("actionShowControlDock")
            self.control_dock_action.setCheckable(True)
            self.control_dock_action.triggered.connect(self._show_control_dock)
            view_menu.addAction(self.control_dock_action)

        # Additive workflows 6/7 (M4): backup creation and independent
        # verification get their own Tools menu, deliberately separate from
        # File/View and from each other.  No existing action changes.
        tools_menu = self.menuBar().addMenu("Tools")
        self.create_backup_action = QAction("Create Full Backup…", self)
        self.create_backup_action.setObjectName("actionCreateFullBackup")
        self.create_backup_action.setToolTip(
            "Create a full Backup v1 package (*.botsbackup) of this workspace"
        )
        self.create_backup_action.triggered.connect(
            self._on_create_backup_requested
        )
        tools_menu.addAction(self.create_backup_action)
        self.verify_backup_action = QAction("Verify Backup Package…", self)
        self.verify_backup_action.setObjectName("actionVerifyBackupPackage")
        self.verify_backup_action.setToolTip(
            "Independently verify a *.botsbackup package against its own manifest"
        )
        self.verify_backup_action.triggered.connect(
            self._on_verify_backup_requested
        )
        tools_menu.addAction(self.verify_backup_action)
        # Additive workflow 8 (M5): the whole-installation restore handoff.
        # The action only opens the selection dialog; restore itself runs in
        # the pre-store bootstrap child AFTER this application has fully
        # closed and released authority (RESTORE_UI_HANDOFF.md §2) — never
        # in this live session, and no destructive override is offered,
        # mentioned as available, or inferred.
        self.restore_from_backup_action = QAction("Restore From Backup…", self)
        self.restore_from_backup_action.setObjectName("actionRestoreFromBackup")
        self.restore_from_backup_action.setToolTip(
            "Restore this whole installation from a *.botsbackup package: "
            "the application closes cleanly first and the restore runs "
            "before the desktop can be used again"
        )
        self.restore_from_backup_action.triggered.connect(
            self._on_restore_from_backup_requested
        )
        tools_menu.addAction(self.restore_from_backup_action)

        self.rail.export_transcript_requested.connect(
            self._on_export_transcript_chat_requested
        )
        self.rail.export_archive_requested.connect(
            self._on_export_archive_chat_requested
        )
        # Phase 11 M3 (F4/F5/F7): folders, pins and deletion.
        self.rail.pin_chat_requested.connect(self._on_pin_chat_requested)
        self.rail.move_chat_to_folder_requested.connect(
            self._on_move_chat_to_folder_requested
        )
        self.rail.open_move_dialog_requested.connect(
            self._on_open_move_dialog_requested
        )
        self.rail.delete_chat_requested.connect(self._on_delete_chat_requested)

    def _show_campaign_dock(self, checked: bool = False) -> None:
        if self._campaign_dock is not None:
            if checked:
                self._campaign_dock.show()
                self._campaign_dock.raise_()
            else:
                self._campaign_dock.hide()

    def _show_control_dock(self, checked: bool = False) -> None:
        if self._control_dock is not None:
            if checked:
                self._control_dock.show()
                self._control_dock.raise_()
            else:
                self._control_dock.hide()

    def _show_import_queue(self, _checked: bool = False) -> None:
        self.import_queue_dock.show()
        self.import_queue_dock.raise_()

    def _on_export_transcript_requested(self, _checked: bool = False) -> None:
        self._phase9.open_transcript_export(self, self._current_chat_id)

    def _on_export_archive_requested(self, _checked: bool = False) -> None:
        self._phase9.open_archive_export(self, self._current_chat_id)

    def _on_import_archive_requested(self, _checked: bool = False) -> None:
        self._phase9.open_archive_import(self)

    def _on_create_backup_requested(self, _checked: bool = False) -> None:
        self._phase9.open_backup_creation(self)

    def _on_verify_backup_requested(self, _checked: bool = False) -> None:
        self._phase9.open_backup_verification(self)

    def _on_restore_from_backup_requested(self, _checked: bool = False) -> None:
        self._phase9.open_restore_handoff(self, self._restore_handoff)

    def _on_export_transcript_chat_requested(self, chat_id: str) -> None:
        self._phase9.open_transcript_export(self, chat_id)

    def _on_export_archive_chat_requested(self, chat_id: str) -> None:
        self._phase9.open_archive_export(self, chat_id)

    # ------------------------------------------------------------------
    # Phase 11 M3 (F4/F5/F7): folders, pins and deletion.  The window only
    # orchestrates: every state change goes through an application command,
    # and the deletion confirmation shows the store-computed loss inventory
    # BEFORE anything destructive is admitted.
    # ------------------------------------------------------------------

    def _supports_organisation(self) -> bool:
        return hasattr(self._application, "list_folders")

    async def _refresh_folders(self) -> None:
        if not self._supports_organisation():
            return
        try:
            folders = await self._application.list_folders()
        except Exception:
            return
        self.rail.set_folders(folders)

    def _on_pin_chat_requested(self, chat_id: str, pinned: bool) -> None:
        self._schedule(self._pin_chat(chat_id, pinned))

    async def _pin_chat(self, chat_id: str, pinned: bool) -> None:
        try:
            await self._application.set_chat_pinned(chat_id, pinned)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        self.statusBar().showMessage(
            "Chat pinned" if pinned else "Chat unpinned", 2500
        )

    def _on_move_chat_to_folder_requested(
        self, chat_id: str, folder_id: str | None
    ) -> None:
        self._schedule(self._move_chat_to_folder(chat_id, folder_id))

    async def _move_chat_to_folder(self, chat_id: str, folder_id: str | None) -> None:
        try:
            await self._application.set_chat_folder(chat_id, folder_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        self.statusBar().showMessage("Chat moved", 2500)

    def _on_open_move_dialog_requested(self, chat_id: str) -> None:
        self._schedule(self._move_chat_via_dialog(chat_id))

    async def _move_chat_via_dialog(self, chat_id: str) -> None:
        if not self._supports_organisation():
            return
        try:
            folders = await self._application.list_folders()
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        chat = next(
            (chat for chat in self._rail_chats if chat.id == chat_id), None
        )
        if chat is None:
            return
        dialog = MoveToFolderDialog(
            chat.title,
            folders,
            chat.folder_id,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        folder_id, new_folder_name = dialog.outcome()
        try:
            if new_folder_name:
                folder = await self._application.create_folder(new_folder_name)
                folder_id = folder.id
            await self._application.set_chat_folder(chat_id, folder_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        await self._refresh_folders()
        chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        self.statusBar().showMessage("Chat moved", 2500)

    def _on_delete_chat_requested(self, chat_id: str) -> None:
        self._schedule(self._confirm_and_delete_chat(chat_id))

    async def _confirm_and_delete_chat(self, chat_id: str) -> None:
        if not self._supports_organisation():
            return
        try:
            inventory = await self._application.describe_chat_deletion(chat_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        dialog = DeleteChatConfirmationDialog(inventory, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            await self._application.delete_chat(chat_id)
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        if self._current_chat_id is None:
            self.transcript.render((), self._generation_busy)
            self._sync_chat_header(None)
        else:
            self._schedule(self._refresh_transcript(self._current_chat_id))
        self.statusBar().showMessage("Chat deleted", 2500)

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
                # Phase 11 M0a: clamp the restored position into the union of
                # available screens while PRESERVING the stored width/height.
                self.setGeometry(*clamp_geometry_to_available_screens(geometry))
            if self._window_state.maximized:
                # Phase 11 M4b: the persisted maximized state wins over the
                # restored geometry when the window is shown.
                self.showMaximized()
            if self._window_state.transcript_scroll_position is not None:
                # Phase 11 M4b: restore-aware transcript scrolling; the pending
                # position is applied by the next render/range change.
                self.transcript.set_restore_scroll_position(
                    self._window_state.transcript_scroll_position
                )
            self._historical_leaf_message_id = self._window_state.inspector_leaf_message_id
        chats = await self._application.list_chats()
        if not chats:
            await self._application.create_chat()
            chats = await self._application.list_chats()
        await self._refresh_folders()
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
            if (
                self._historical_leaf_message_id is not None
                and (
                    not self._current_messages
                    or self._current_messages[-1].id
                    != self._historical_leaf_message_id
                )
            ):
                # Restored presentation identity is advisory.  It must never
                # keep a stale/deleted historical leaf selected.
                self._set_historical_leaf(None)
            if self._window_state is not None and self._window_state.inspector_open:
                self.inspector_dock.show()
                if self._window_state.inspector_message_id is not None:
                    self._selected_message = next(
                        (item for item in self._current_messages if item.id == self._window_state.inspector_message_id),
                        None,
                    )
                await self._refresh_inspector()
            await self._sync_current_activity()
        # Phase 11 M4b/M6: restore the persisted workspace/settings plane.
        await self._restore_dock_layout()
        await self._restore_search_state()
        await self._apply_saved_keybinding_overrides()
        await self._apply_persisted_font_scale()
        if chats:
            await self._restore_chat_draft(self._current_chat_id)
        await self._refresh_phase5_state()
        await self._save_workspace()

    # ------------------------------------------------------------------
    # Phase 11 M4b/M6: workspace/settings persistence plane (R-10 SQLite).
    # Drafts, dock layout blob, keybinding overrides and font/scale settings
    # are advisory restoration state: a read failure or incompatible payload
    # degrades to the default behaviour and must never block the window.
    # ------------------------------------------------------------------

    def _on_dock_layout_changed(self, *_args) -> None:
        if self._applying_dock_layout:
            return
        self._schedule(self._persist_dock_layout())

    async def _persist_dock_layout(self) -> None:
        if self._window_id is None:
            return
        saver = getattr(self._application, "save_dock_layout", None)
        if saver is None:
            return
        try:
            await saver(self._window_id, bytes(self.saveState()))
        except Exception as exc:
            self.statusBar().showMessage(f"Dock layout not saved: {exc}")

    async def _restore_dock_layout(self) -> None:
        if self._window_id is None:
            return
        getter = getattr(self._application, "get_dock_layout", None)
        if getter is None:
            return
        try:
            blob = await getter(self._window_id)
        except Exception:
            return
        if not blob:
            return
        self._applying_dock_layout = True
        try:
            try:
                restored = self.restoreState(bytes(blob))
            except Exception:
                restored = False
        finally:
            self._applying_dock_layout = False
            # Advisory Qt layout state may include a hidden global toolbar.
            # It cannot suppress navigation or the explicit runtime warning.
            self.console_toolbar.show()
        if not restored:
            # The blob is an advisory, Qt-version-tied opaque payload: a
            # malformed or incompatible layout falls back to the default
            # layout (mirroring the malformed-row fallback in the store)
            # instead of raising or leaving a half-applied layout.
            self._apply_default_dock_layout()
            self.statusBar().showMessage(
                "Stored dock layout was incompatible; using the default layout.",
                5000,
            )

    def _apply_default_dock_layout(self) -> None:
        """Re-apply the constructed default dock layout (all docks hidden)."""
        self._applying_dock_layout = True
        try:
            for dock in self._dock_widgets():
                dock.setFloating(False)
                dock.hide()
        finally:
            self._applying_dock_layout = False

    # ------------------------------------------------------------------
    # Phase 11 fork R-15: faithful search-state restore.  The persisted
    # filter set is the authoritative last search of this window; the panel
    # widgets are a projection of it wherever the UI can represent a field.
    # ------------------------------------------------------------------

    async def _restore_search_state(self) -> None:
        state = self._window_state
        if state is None:
            return
        if state.search_open:
            # Applied after the dock-layout blob so the structured flag wins
            # over the opaque Qt payload for the search dock specifically.
            self.search_dock.show()
        if state.search_query is None and state.search_filters is None:
            return
        self._last_search_query = state.search_query
        self._last_search_filters = state.search_filters
        self._next_search_cursor = state.search_cursor
        self._project_search_filters_into_panel(state.search_filters, state.search_query)

    def _project_search_filters_into_panel(
        self,
        filters: SearchFilters | None,
        query: str | None,
    ) -> None:
        """Reflect the restored search state into the panel widgets.

        Advisory only: pagination always re-runs the restored filter object
        itself, so a field the panel cannot represent (backend/provider/model/
        connection/model-entry attributions, a second role or state, either
        time bound) still round-trips faithfully through
        ``_last_search_filters``.
        """
        if query is not None:
            self.search_panel.query_edit.setText(query)
        if filters is None:
            return
        kinds = set(filters.document_kinds)
        self.search_panel.chat_kind.setChecked(
            not kinds or SearchDocumentKind.CHAT in kinds
        )
        self.search_panel.message_kind.setChecked(
            not kinds or SearchDocumentKind.MESSAGE in kinds
        )
        self.search_panel.attachment_kind.setChecked(
            not kinds or SearchDocumentKind.ATTACHMENT in kinds
        )
        if len(filters.roles) == 1:
            self.search_panel.role_combo.setCurrentIndex(
                self.search_panel.role_combo.findData(filters.roles[0].value)
            )
        if len(filters.message_states) == 1:
            self.search_panel.state_combo.setCurrentIndex(
                self.search_panel.state_combo.findData(filters.message_states[0].value)
            )
        self.search_panel.active_only.setChecked(filters.active_branch_only)
        self.search_panel.include_archived.setChecked(filters.include_archived)

    def _dock_widgets(self) -> tuple[QDockWidget, ...]:
        docks = [self.inspector_dock, self.search_dock, self.import_queue_dock]
        if self._campaign_dock is not None:
            docks.append(self._campaign_dock)
        return tuple(docks)

    async def _apply_saved_keybinding_overrides(self) -> None:
        """Apply every persisted keybinding override at startup."""
        getter = getattr(self._application, "get_keybinding_override", None)
        if getter is None:
            return
        for action in self._action_registry.get_all_actions():
            try:
                shortcut = await getter(action.action_id)
            except Exception:
                continue
            if shortcut:
                # Startup application never re-persists what it just read.
                self.apply_keybinding_override(action.action_id, shortcut, persist=False)

    def apply_keybinding_override(
        self, action_id: str, shortcut: str, *, persist: bool = True
    ) -> bool:
        """Apply one keybinding override; report whether it conflicts.

        The override updates the action registry's effective shortcut and the
        real QAction chord, and (unless ``persist`` is false) is saved to the
        workspace settings plane with the detected conflict flag.
        """
        conflict = self._action_registry.apply_override(action_id, shortcut)
        qaction = self._qaction_by_action_id.get(action_id)
        if qaction is not None:
            qaction.setShortcut(QKeySequence(shortcut))
        if persist:
            self._schedule(
                self._persist_keybinding_override(action_id, shortcut, conflict)
            )
        return conflict

    async def _persist_keybinding_override(
        self, action_id: str, shortcut: str, conflict_detected: bool
    ) -> None:
        saver = getattr(self._application, "save_keybinding_override", None)
        if saver is None:
            return
        try:
            await saver(action_id, shortcut, conflict_detected)
        except Exception as exc:
            self.statusBar().showMessage(f"Keybinding override not saved: {exc}")

    def reset_keybinding_overrides(self) -> None:
        """Reset every keybinding override and persist the reset."""
        self._action_registry.reset_overrides()
        for action_id, qaction in self._qaction_by_action_id.items():
            default = self._default_shortcut_by_action_id.get(action_id, "")
            qaction.setShortcut(QKeySequence(default))
        self._schedule(self._persist_keybinding_reset())

    async def _persist_keybinding_reset(self) -> None:
        reset = getattr(self._application, "reset_keybinding_overrides", None)
        if reset is None:
            return
        try:
            await reset()
        except Exception as exc:
            self.statusBar().showMessage(f"Keybinding overrides not reset: {exc}")

    async def _apply_persisted_font_scale(self) -> None:
        """Apply the persisted font/scale settings (Phase 11 F9/M6)."""
        getter = getattr(self._application, "get_font_scale_settings", None)
        if getter is None:
            return
        try:
            scale_factor, ui_font, transcript_font, code_font, size_pt = await getter()
        except Exception:
            return
        application_instance = self._qt_application()
        if application_instance is None:
            return
        if not scale_factor or scale_factor <= 0:
            scale_factor = 1.0
        application_instance.setStyleSheet(
            build_theme_stylesheet(
                scale=scale_factor,
                ui_font=ui_font,
                transcript_font=transcript_font,
                code_font=code_font,
                size_pt=size_pt,
            )
        )

    async def _persist_chat_draft(self, chat_id: str | None, text: str) -> None:
        if chat_id is None:
            return
        saver = getattr(self._application, "save_chat_draft", None)
        if saver is None:
            return
        try:
            await saver(chat_id, text)
        except Exception as exc:
            self.statusBar().showMessage(f"Draft not saved: {exc}")

    async def _restore_chat_draft(self, chat_id: str | None) -> None:
        if chat_id is None:
            return
        getter = getattr(self._application, "get_chat_draft", None)
        if getter is None:
            return
        try:
            draft = await getter(chat_id)
        except Exception:
            return
        if chat_id != self._current_chat_id:
            return
        if draft:
            # A stored draft replaces the composer text.  With no stored
            # draft the composer is left untouched, preserving the landed
            # Draft-1 behaviour where in-progress composer text survives a
            # chat switch (test_desktop_draft1 switching-chats contract).
            self.composer.setPlainText(draft)

    async def _switch_chat_draft(
        self, previous_chat_id: str | None, chat_id: str | None
    ) -> None:
        """Save the outgoing chat's composer text and restore the incoming one."""
        if previous_chat_id is not None:
            await self._persist_chat_draft(previous_chat_id, self.composer.toPlainText())
        await self._restore_chat_draft(chat_id)

    def _replace_chat_list(self, chats: tuple[Chat, ...] | list[Chat]) -> None:
        chats = tuple(chats)
        selected = self._current_chat_id
        self._chat_ids = [chat.id for chat in chats]
        self._rail_chats = chats
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
            previous_chat_id = self._current_chat_id
            if chat_id != self._current_chat_id:
                self._clear_editing()
                self._set_historical_leaf(None)
                self.continuation_banner.clear()
            self._current_chat_id = chat_id
            self._selected_message = None
            if previous_chat_id is not None and previous_chat_id != chat_id:
                # Phase 11 M4b: the composer text belongs to the outgoing
                # chat as its draft; the incoming chat's draft takes its
                # place (empty when none was stored).
                self._schedule(self._switch_chat_draft(previous_chat_id, chat_id))
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
        self.rail.set_collapsed(not self.rail.effective_collapsed)
        self.top_bar.set_rail_collapsed(self.rail.effective_collapsed)
        self._schedule(self._save_workspace())

    def _open_global_search(self, _checked: bool = False) -> None:
        self.search_panel.scope_combo.setCurrentIndex(
            self.search_panel.scope_combo.findData("global")
        )
        self.search_dock.show()
        self.search_dock.raise_()
        self.search_panel.focus_query()

    def _open_in_chat_search(self, _checked: bool = False) -> None:
        if self._current_chat_id is None:
            self.statusBar().showMessage("Select a chat before searching within it")
            return
        self.search_panel.set_in_chat_scope()
        self.search_dock.show()
        self.search_dock.raise_()
        self.search_panel.focus_query()

    def _toggle_search(self, visible: bool) -> None:
        self.search_dock.setVisible(visible)
        # Phase 11 fork R-15: the search panel open/closed state is part of
        # the persisted window state, not only of the opaque dock blob.
        self._schedule(self._save_workspace())
        if visible:
            self.search_panel.set_current_chat_available(self._current_chat_id is not None)
            self.search_panel.focus_query()
            self._schedule(self._refresh_search_status())

    def _sync_search_button(self, visible: bool) -> None:
        self._fit_workspace_panels()
        if self.top_bar.search_button.isChecked() != visible:
            self.top_bar.search_button.blockSignals(True)
            self.top_bar.search_button.setChecked(visible)
            self.top_bar.search_button.blockSignals(False)
        if visible:
            self.search_panel.set_current_chat_available(self._current_chat_id is not None)

    async def _refresh_search_status(self) -> None:
        try:
            status = await self._application.search_status()
        except SearchError as exc:
            self._show_search_error(exc)
            return
        except Exception as exc:
            self.search_panel.show_error("ERROR", str(exc))
            return
        self.search_panel.show_status(status)

    def _on_search_requested(self, payload: object) -> None:
        if isinstance(payload, dict):
            self._schedule(self._run_search(payload, append=False))

    def _on_load_more_search(self) -> None:
        if (
            not self._search_busy
            and self._last_search_query is not None
            and self._last_search_filters is not None
            and self._next_search_cursor is not None
        ):
            self._schedule(self._load_more_search())

    async def _run_search(self, payload: dict[str, object], *, append: bool) -> None:
        if self._search_busy:
            return
        query = str(payload.get("query", ""))
        chat_id = self._current_chat_id if payload.get("scope") == "chat" else None
        if payload.get("scope") == "chat" and chat_id is None:
            self.search_panel.show_error("INVALID QUERY", "Select a chat for in-chat search")
            return
        try:
            filters = SearchFilters(
                chat_id=chat_id,
                document_kinds=tuple(
                    SearchDocumentKind(str(value))
                    for value in payload.get("document_kinds", ())
                ),
                roles=(MessageRole(str(payload["role"])),) if payload.get("role") else (),
                message_states=(
                    MessageState(str(payload["message_state"])),
                )
                if payload.get("message_state")
                else (),
                backend_ids=(),
                provider_ids=(),
                models=(),
                connection_ids=(),
                model_entry_ids=(),
                active_branch_only=bool(payload.get("active_branch_only", False)),
                include_archived=bool(payload.get("include_archived", False)),
                after=None,
                before=None,
            )
        except (TypeError, ValueError) as exc:
            self.search_panel.show_error("INVALID QUERY", str(exc))
            return
        self._search_busy = True
        self.search_panel.set_busy(True)
        try:
            page = await self._application.search(
                query,
                filters=filters,
                limit=50,
                cursor=self._next_search_cursor if append else None,
            )
        except SearchError as exc:
            self._show_search_error(exc)
            return
        except Exception as exc:
            self.search_panel.show_error("ERROR", str(exc))
            return
        finally:
            self._search_busy = False
            self.search_panel.set_busy(False)
        self._last_search_query = query
        self._last_search_filters = filters
        self._next_search_cursor = page.next_cursor
        self.search_panel.show_page(page, append=append)
        # Phase 11 fork R-15: the last search (query, faithful filter set and
        # result cursor) is part of the persisted window state.
        self._schedule(self._save_workspace())

    async def _load_more_search(self) -> None:
        query = self._last_search_query
        filters = self._last_search_filters
        cursor = self._next_search_cursor
        if query is None or filters is None or cursor is None or self._search_busy:
            return
        self._search_busy = True
        self.search_panel.set_busy(True)
        try:
            page = await self._application.search(
                query,
                filters=filters,
                limit=50,
                cursor=cursor,
            )
        except SearchError as exc:
            self._show_search_error(exc)
            return
        except Exception as exc:
            self.search_panel.show_error("ERROR", str(exc))
            return
        finally:
            self._search_busy = False
            self.search_panel.set_busy(False)
        self._next_search_cursor = page.next_cursor
        self.search_panel.show_page(page, append=True)
        # Phase 11 fork R-15: pagination moved the result cursor; persist it.
        self._schedule(self._save_workspace())

    def _on_search_result_requested(self, result: object, location_index: int) -> None:
        if isinstance(result, SearchResult):
            self._schedule(self._navigate_search_result(result, location_index))

    async def _navigate_search_result(
        self,
        result: SearchResult,
        location_index: int,
    ) -> None:
        try:
            navigation = await self._application.resolve_search_result(
                result,
                location_index=location_index,
            )
        except SearchError as exc:
            self._show_search_error(exc, clear_results=False)
            return
        except Exception as exc:
            self.search_panel.show_error("ERROR", str(exc), clear_results=False)
            return

        chat_id = navigation.chat.id
        if chat_id not in self._chat_ids:
            chats = await self._application.list_chats()
            self._replace_chat_list(chats)
        if chat_id not in self._chat_ids:
            self.search_panel.show_error(
                "GONE",
                "The result chat no longer exists",
                clear_results=False,
            )
            return
        self._clear_editing()
        self._current_chat_id = chat_id
        self.rail.chat_list.blockSignals(True)
        try:
            self._select_chat_row(chat_id)
        finally:
            self.rail.chat_list.blockSignals(False)
        if self._window_id is not None:
            self._workspace.set_selected_chat(self._window_id, chat_id)
        self.rail.set_activity(self._activity, chat_id)
        self._set_historical_leaf(navigation.historical_leaf_message_id)
        self._render_transcript_projection(
            navigation.chat,
            navigation.messages,
            focus_message_id=navigation.focus_message_id,
        )
        if navigation.focus_message_id is not None and not self.transcript.focus_message(
            navigation.focus_message_id
        ):
            self.search_panel.show_error(
                "GONE",
                "The exact message is no longer present",
                clear_results=False,
            )
            return
        await self._save_workspace()

    def _show_search_error(self, error: SearchError, *, clear_results: bool = True) -> None:
        if isinstance(error, SearchCursorStale):
            self._next_search_cursor = None
            self.search_panel.show_pagination_error(str(error))
            self.statusBar().showMessage(f"Search pagination expired: {error}")
            # Phase 11 fork R-15: the expired cursor is gone from the window
            # state too, so a restart does not restore a dead cursor.
            self._schedule(self._save_workspace())
            return
        if isinstance(error, SearchResultGone):
            condition = "GONE"
        elif isinstance(error, SearchUnavailable):
            condition = "UNAVAILABLE"
        elif isinstance(error, SearchRebuilding):
            condition = "REBUILDING"
        elif isinstance(error, SearchStaleIndex):
            condition = "STALE"
        elif isinstance(error, SearchIndexInvalid):
            condition = "INVALID"
        elif isinstance(error, SearchInvalidQuery):
            condition = "INVALID QUERY"
        else:
            condition = "SEARCH ERROR"
        self.search_panel.show_error(condition, str(error), clear_results=clear_results)
        self.statusBar().showMessage(f"Search {condition.lower()}: {error}")

    def _on_rebuild_search(self) -> None:
        if not self._search_busy:
            self._schedule(self._rebuild_search())

    async def _rebuild_search(self) -> None:
        self._search_busy = True
        self.search_panel.set_busy(True)
        self.search_panel.show_error("REBUILDING", "Rebuilding the derived search index")
        await asyncio.sleep(0)
        try:
            status = await self._application.rebuild_search_index()
        except SearchError as exc:
            self._show_search_error(exc)
            return
        except Exception as exc:
            self.search_panel.show_error("ERROR", str(exc))
            return
        finally:
            self._search_busy = False
            self.search_panel.set_busy(False)
        self._last_search_query = None
        self._last_search_filters = None
        self._next_search_cursor = None
        self.search_panel.results.clear()
        self._schedule(self._save_workspace())
        self.search_panel.load_more_button.setVisible(False)
        self.search_panel.show_status(status)

    def _on_new_chat(self) -> None:
        self._clear_editing()
        self._set_historical_leaf(None)
        self._schedule(self._create_chat())

    def _on_archive_chat(self) -> None:
        if self._current_chat_id is not None:
            self._schedule(self._toggle_current_chat_archive())

    def _on_palette_pin_current_chat(self, checked: bool = False) -> None:
        chat = self._current_chat
        if chat is None:
            return
        self._on_pin_chat_requested(chat.id, not bool(chat.is_pinned))

    def _on_palette_delete_current_chat(self, checked: bool = False) -> None:
        if self._current_chat_id is not None:
            self._on_delete_chat_requested(self._current_chat_id)

    async def _toggle_current_chat_archive(self) -> None:
        chat = self._current_chat
        if chat is None or chat.id != self._current_chat_id:
            return
        try:
            if chat.archived_at is None:
                updated = await self._application.archive_chat(chat.id)
                message = "Chat archived"
            else:
                updated = await self._application.unarchive_chat(chat.id)
                message = "Chat unarchived"
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        self._current_chat = updated
        self._sync_chat_header(updated)
        chats = await self._application.list_chats()
        self._replace_chat_list(chats)
        self.statusBar().showMessage(message, 2500)

    def _on_return_active_branch(self) -> None:
        if self._current_chat_id is None:
            return
        self._set_historical_leaf(None)
        self._schedule(self._refresh_transcript(self._current_chat_id))

    def _set_historical_leaf(self, message_id: str | None) -> None:
        self._historical_leaf_message_id = message_id
        historical = message_id is not None
        self.historical_banner.setVisible(historical)
        if historical:
            self._clear_editing()
        for row in self.transcript.message_rows.values():
            row.set_historical_view(
                historical,
                exact_result=row.message.id == message_id,
            )
        self._update_controls()

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
                credential_statuses = {}
                for connection in connections:
                    credential = await self._application.provider_credential_status(connection.id)
                    credential_statuses[connection.id] = "unknown" if credential is None else credential.status
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
            records = []
            selected_model = None
            for model in models:
                connection = connection_by_id.get(model.connection_id)
                connection_name = connection.name if connection is not None else "missing connection"
                state = model.availability.value
                label = f"{connection_name} / {model.provider_model_id} [{state}]"
                entries.append((label, model.id))
                # Phase 11 F8: rich presentation record — display_name is the
                # primary line; connection + context window is the secondary
                # line; the connection health pill and detail card derive from
                # the same landed catalogue data.  Presentation only.
                metadata = dict(model.metadata) if isinstance(model.metadata, dict) else {}
                context_value = metadata.get("context_length")
                records.append(
                    ModelSelectorEntry(
                        model_entry_id=model.id,
                        display_name=model.display_name,
                        provider_model_id=model.provider_model_id,
                        connection_id=model.connection_id,
                        connection_name=connection_name,
                        availability=state,
                        connection_available=bool(connection is not None and connection.available),
                        connection_refresh_status=(
                            connection.catalogue_refresh_status.value
                            if connection is not None
                            else "never"
                        ),
                        connection_backend_type=(
                            connection.backend_type.value if connection is not None else ""
                        ),
                        context_tokens=(
                            context_value
                            if isinstance(context_value, int) and not isinstance(context_value, bool)
                            else None
                        ),
                        metadata=metadata,
                        connection_retired=bool(connection is not None and connection.retired),
                        connection_enabled=bool(connection is not None and connection.enabled),
                        context_accounting=("provider-managed" if connection is not None and connection.backend_type is BackendType.OPENAI_COMPATIBLE_HTTP and connection.profile is ProviderProfile.OPENROUTER and not self._developer_provider_test_mode else ""),
                        credential_status=credential_statuses.get(model.connection_id, "unknown"),
                        credential_required=bool(connection is not None and connection.backend_type is not BackendType.FAKE and (connection.profile is ProviderProfile.OPENROUTER or connection.credential_source is not CredentialSource.NONE)),
                        catalogue_freshness=("never refreshed" if connection is None or connection.catalogue_refresh_at is None else f"state recorded {connection.catalogue_refresh_at.isoformat()}"),
                    )
                )
                if selection is not None and model.id == selection.model_entry_id:
                    selected_model = (connection, model)
            self._model_selector_records = records
            self._model_selector_selected_id = None if selection is None else selection.model_entry_id
            self.top_bar.set_models(entries, None if selection is None or selection.selection_required else self._model_selector_selected_id, records=records)
            if selection is None or selection.selection_required or selected_model is None:
                self.top_bar.model_pill.setText("Selection required")
                self.top_bar.model_pill.setToolTip("This chat has no durable current model selection.")
            else:
                connection, model = selected_model
                self.top_bar.model_pill.setText(model.provider_model_id)
                self.top_bar.model_pill.setToolTip(
                    f"{connection.name} / {model.provider_model_id} — {model.availability.value}"
                )
            selected_entry = next((entry for entry in records if entry.model_entry_id == self._model_selector_selected_id), None)
            self._present_model_readiness(selected_entry, selection_required=selection is None or selection.selection_required)
            await self._refresh_settings_dialog_unlocked()
            await self._refresh_tune_dialog_unlocked()

    def _on_model_selected(self, model_entry_id: str) -> None:
        if self._current_chat_id is not None:
            self._schedule(self._select_model(self._current_chat_id, model_entry_id))

    def _open_model_selector(self) -> None:
        """Phase 11 F8: host the searchable, grouped selector popup.

        Non-modal like the other desktop dialogs; the popup emits
        ``model_entry_chosen`` and the landed ``_select_model`` command path
        performs the durable selection.  Session-only: nothing here persists.
        """

        if not self._phase5 or not self.top_bar.model_selector.has_entries():
            return
        if self._model_selector_popup is not None:
            self._model_selector_popup.raise_()
            self._model_selector_popup.activateWindow()
            return
        dialog = ModelSelectorPopup(self)
        dialog.set_entries(self._model_selector_records, self._model_selector_selected_id)
        dialog.model_entry_chosen.connect(self._on_model_selected)
        dialog.view_connection_requested.connect(self._on_view_model_connection)
        dialog.finished.connect(lambda _result: self._clear_model_selector_popup(dialog))
        anchor = self.top_bar.model_selector
        anchor_point = anchor.mapToGlobal(QPoint(0, anchor.height()))
        self._model_selector_popup = dialog
        dialog.show()
        position_dialog_at_anchor(dialog, anchor_point)

    def _present_model_readiness(self, selected_entry: ModelSelectorEntry | None, *, selection_required: bool = False) -> None:
        self.top_bar.tune_button.setEnabled(self._phase5 and selected_entry is not None and not selection_required)
        if selected_entry is None or selection_required:
            self._model_known_blocked = True
            self.model_readiness_label.setText("Choose a model")
            self.top_bar.readiness_label.setText("Choose a model")
            self.top_bar.readiness_label.setToolTip("This chat has no current model selection.")
            self.model_recovery_button.setText("Choose model")
            self.model_recovery_button.setVisible(True)
            self.transcript.empty_label.setText("Start a conversation\n\nChoose a model, then write a message below.")
        else:
            heading, reason = model_readiness(selected_entry)
            self._model_known_blocked = heading.startswith("Unavailable")
            if selected_entry.context_accounting == "provider-managed":
                reason += "\nB.O.T.S. selects context deterministically. Exact upstream tokenization is unavailable; the provider performs final context admission."
                heading += " · Context accounting: Provider-managed"
            self.model_readiness_label.setText(heading)
            self.top_bar.readiness_label.setText(heading)
            self.top_bar.readiness_label.setToolTip(reason + "\n" + readiness_details(selected_entry))
            self.model_recovery_button.setText("View connection")
            self.model_recovery_button.setVisible(self._model_known_blocked)
            self.model_readiness_label.setToolTip(reason + "\n" + readiness_details(selected_entry))
            self.transcript.empty_label.setText("Start a conversation\n\n" + (reason if self._model_known_blocked else "Write a message below to start this chat."))
        self.send_button.setToolTip(self.model_readiness_label.text() if self._model_known_blocked else "Send message (Enter)")
        self._update_controls()

    def _on_model_recovery(self) -> None:
        if self.model_recovery_button.text() == "Choose model":
            if self.top_bar.model_selector.has_entries():
                self._open_model_selector()
            else:
                self._on_settings_requested()
        else:
            selected = next((entry for entry in self._model_selector_records if entry.model_entry_id == self._model_selector_selected_id), None)
            if selected is not None:
                self._on_view_model_connection(selected.connection_id)

    def _on_view_model_connection(self, connection_id: str) -> None:
        self._on_settings_requested()
        self._schedule(self._show_model_connection(connection_id))

    async def _show_model_connection(self, connection_id: str) -> None:
        await self._refresh_settings_dialog()
        if self._settings_dialog is not None:
            self._settings_dialog.show_connection(connection_id)
            self._settings_dialog.raise_()

    def _clear_model_selector_popup(self, dialog: ModelSelectorPopup) -> None:
        if self._model_selector_popup is dialog:
            self._model_selector_popup = None

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
        extra_override, extra_revision = await self._application.chat_generation_settings_extra_override(chat_id)
        capabilities = await self._application.generation_setting_capabilities(selection.model_entry_id)
        model_summary = await self._tune_model_summary(selection.model_entry_id)
        dialog.set_settings(
            settings.as_full_dict(),
            settings.provenance,
            timeout_override=None if override is None else override.timeout_seconds,
            override_revision=0 if override_revision is None else override_revision,
            extra_revision=extra_revision,
            model_entry_id=selection.model_entry_id,
            extra_values=extra_override or {},
            capabilities=capabilities,
            model_summary=model_summary,
        )
        dialog.save_requested.connect(lambda values: self._schedule(self._save_tune(chat_id, values)))
        dialog.inherit_requested.connect(lambda values: self._schedule(self._save_tune(chat_id, values)))
        self._tune_dialog = dialog
        self._tune_chat_id = chat_id
        dialog.show()

    async def _tune_model_summary(self, model_entry_id: str) -> str:
        try:
            models = await self._application.list_model_catalogue()
            for model in models:
                if model.id == model_entry_id:
                    return model.provider_model_id
        except Exception:
            pass
        return ""

    async def _save_tune(self, chat_id: str, values: dict[str, object]) -> None:
        try:
            expected_revision = values.pop("expected_revision", None)
            expected_extra_revision = values.pop("expected_extra_revision", None)
            expected_model_entry_id = values.pop("expected_model_entry_id", None)
            extra = values.pop("extra", {}) or {}
            if values.pop("extra_invalid", False):
                raise StateError("extended settings contain invalid values")
            await self._application.set_chat_generation_settings(
                chat_id,
                GenerationSettings(**values, extra=extra),
                expected_revision=expected_revision,
                expected_extra_revision=expected_extra_revision,
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
        # Real instrumentation only: live provider/model catalogue counts.
        self._settings_dialog.instrument_strip.set_instruments([
            ("connections", str(len(connections))),
            ("models", str(len(models))),
            ("available", str(sum(1 for model in models if model.availability.value == "available"))),
        ])
        current = self._settings_dialog.connection_list.currentItem()
        if current is not None:
            connection = next((item for item in connections if item.id == current.data(Qt.ItemDataRole.UserRole)), None)
            self._settings_dialog.set_connection_fields(connection)
        model_item = self._settings_dialog.model_list.currentItem()
        if model_item is not None:
            model_entry_id = str(model_item.data(Qt.ItemDataRole.UserRole))
            await self._load_settings_model_detail(model_entry_id)
            overrides = list(await self._application.capability_overrides(model_entry_id))
            overrides.extend(await self._application.generation_setting_capability_overrides(model_entry_id))
            self._settings_dialog.set_capability_overrides(overrides)
            capabilities = await self._application.model_capabilities(model_entry_id)
            self._settings_dialog.set_capabilities(capabilities)
        application_config = await self._application.application_generation_config()
        if application_config is not None:
            application_defaults, default_model_id, revision = application_config
            application_extra, application_extra_revision = (
                await self._application.application_generation_settings_extra_with_revision()
            )
            self._settings_dialog.set_application_defaults(
                application_defaults, revision, default_model_id,
                extra_values=application_extra,
                extra_revision=application_extra_revision,
            )

    async def _load_settings_model_detail(self, model_entry_id: str) -> None:
        """Load model-scope defaults plus the registry-driven editor state."""
        defaults, revision = await self._application.model_defaults_with_revision(model_entry_id)
        model_extra, model_extra_revision = await self._application.model_generation_settings_extra(model_entry_id)
        application_extra = await self._application.application_generation_settings_extra()
        capabilities = await self._application.generation_setting_capabilities(model_entry_id)
        # Effective (application -> model) for inherited-row display.
        effective: dict[str, object] = dict(application_extra)
        effective.update(model_extra or {})
        provenance = {key: ("application" if key in application_extra and key not in (model_extra or {}) else "model") for key in effective}
        if defaults is not None:
            effective_legacy = {
                "temperature": defaults.temperature,
                "max_output_tokens": defaults.max_output_tokens,
                "reasoning_effort": defaults.reasoning_effort,
                "timeout_seconds": defaults.timeout_seconds,
            }
        else:
            app_defaults = await self._application.application_generation_settings()
            effective_legacy = {
                "temperature": getattr(app_defaults, "temperature", None),
                "max_output_tokens": getattr(app_defaults, "max_output_tokens", None),
                "reasoning_effort": getattr(app_defaults, "reasoning_effort", None),
                "timeout_seconds": getattr(app_defaults, "timeout_seconds", None),
            }
            provenance.update({key: "application" for key in effective_legacy})
        effective_legacy.update(effective)
        self._settings_dialog.set_model_defaults(
            defaults,
            revision,
            extra_values=model_extra or {},
            extra_revision=model_extra_revision,
            effective=effective_legacy,
            effective_provenance=provenance,
            capabilities=capabilities,
        )

    async def _refresh_settings_model(self, model_entry_id: str) -> None:
        async with self._phase5_refresh_lock:
            if self._settings_dialog is None:
                return
            current = self._settings_dialog.model_list.currentItem()
            if current is None or str(current.data(Qt.ItemDataRole.UserRole)) != model_entry_id:
                return
            await self._load_settings_model_detail(model_entry_id)
            overrides = list(await self._application.capability_overrides(model_entry_id))
            overrides.extend(await self._application.generation_setting_capability_overrides(model_entry_id))
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
            extra_override, extra_revision = await self._application.chat_generation_settings_extra_override(self._tune_chat_id)
            capabilities = await self._application.generation_setting_capabilities(selection.model_entry_id)
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
            settings.as_full_dict(),
            settings.provenance,
            timeout_override=None if override is None else override.timeout_seconds,
            override_revision=0 if override_revision is None else override_revision,
            extra_revision=extra_revision,
            model_entry_id=selection.model_entry_id,
            extra_values=extra_override or {},
            capabilities=capabilities,
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
            expected_extra_revision = values.pop("expected_extra_revision", None)
            extra = values.pop("extra", {}) or {}
            await self._application.set_model_defaults(
                str(values.pop("model_entry_id")),
                GenerationSettings(**values, extra=extra),
                expected_revision=expected_revision,
                expected_extra_revision=expected_extra_revision,
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
            expected_extra_revision = values.pop("expected_extra_revision", None)
            extra = values.pop("extra", {}) or {}
            await self._application.set_application_generation_settings(
                GenerationSettings(**values, extra=extra), expected_revision=expected_revision,
                expected_extra_revision=expected_extra_revision,
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
        if self._developer_provider_test_mode:
            return
        if (
            self._generation_busy
            or self._historical_leaf_message_id is not None
            or self._current_chat_id is None
        ):
            return
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Attach file",
            "",
            "Text or data files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if path:
            self._schedule(self._attach_file(path))

    async def _attach_file(self, path: str) -> None:
        if self._developer_provider_test_mode:
            self.statusBar().showMessage("Attachments are unavailable in INEXACT / PROVIDER TEST MODE")
            return
        chat_id = self._current_chat_id
        if chat_id is None or self._historical_leaf_message_id is not None:
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
        if self._developer_provider_test_mode:
            self.attachment_button.setToolTip("Attachments are unavailable in INEXACT / PROVIDER TEST MODE")
            return
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
        if self._model_known_blocked:
            self.statusBar().showMessage(self.model_readiness_label.text())
            return
        self._schedule(self._send_message())

    async def _send_message(self) -> None:
        if (
            self._generation_busy
            or self._historical_leaf_message_id is not None
            or self._current_chat_id is None
        ):
            return
        text = self.composer.toPlainText()
        if not text.strip():
            return
        chat_id = self._current_chat_id
        editing_message_id = self._valid_edit_target(chat_id)
        self._set_generation_busy(True)
        try:
            if editing_message_id is None:
                # Additive workflow-5 intercept (send only): an unresolved
                # imported continuation opens the resolution dialog instead of
                # surfacing the landed readiness StateError.  A user edit must
                # never be intercepted and takes the direct landed path below.
                if not await self._phase9.ensure_imported_continuation_ready(
                    self, chat_id, None
                ):
                    self._set_generation_busy(False)
                    return
                attempt = await self._application.send_message(chat_id, text)
            else:
                attempt = await self._application.edit_message(chat_id, editing_message_id, text)
        except Exception as exc:
            self._set_generation_busy(False)
            self.statusBar().showMessage(str(exc))
            return
        self.composer.clear()
        self._clear_editing()
        # Phase 11 M4b: the sent text is no longer a draft.
        self._schedule(self._persist_chat_draft(chat_id, ""))
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
            or self._historical_leaf_message_id is not None
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
        if self._historical_leaf_message_id is not None:
            self._clear_editing()
            return None
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
        if (
            self._current_chat_id is not None
            and self._historical_leaf_message_id is None
        ):
            self._schedule(self._regenerate_message(message))

    async def _regenerate_message(self, message: Message) -> None:
        if self._current_chat_id is None or self._historical_leaf_message_id is not None:
            return
        chat_id = self._current_chat_id
        self._set_generation_busy(True)
        try:
            # Additive workflow-5 intercept (regenerate only): the same
            # readiness predicate as the send path, with the regenerated
            # assistant message as the base key the core gate uses.
            if not await self._phase9.ensure_imported_continuation_ready(
                self, chat_id, message.id
            ):
                self._set_generation_busy(False)
                return
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
        historical = self._historical_leaf_message_id is not None
        self.generation_indicator.setVisible(busy)
        self.composer.setReadOnly(busy or historical)
        self.chat_list.setEnabled(True)
        self.new_chat_button.setEnabled(True)
        self.send_button.setEnabled(
            not busy and not historical and not self._model_known_blocked and bool(self.composer.toPlainText().strip())
        )
        self.attachment_button.setEnabled(
            not self._developer_provider_test_mode
            and not busy and not historical and self._current_chat_id is not None
        )
        self.cancel_button.setEnabled(busy and self._active_attempt_id is not None)
        self.rail.chat_button.setEnabled(True)
        for row in self.transcript.message_rows.values():
            row.set_generation_busy(busy)

    def _update_controls(self) -> None:
        historical = self._historical_leaf_message_id is not None
        self.composer.setReadOnly(self._generation_busy or historical)
        self.send_button.setEnabled(
            not self._generation_busy
            and not historical
            and not self._model_known_blocked
            and bool(self.composer.toPlainText().strip())
        )
        self.attachment_button.setEnabled(
            not self._developer_provider_test_mode
            and not self._generation_busy
            and not historical
            and self._current_chat_id is not None
        )
        self.cancel_button.setEnabled(self._generation_busy and self._active_attempt_id is not None)
        self.archive_button.setEnabled(self._current_chat_id is not None)
        self.search_panel.set_current_chat_available(self._current_chat_id is not None)

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
        if event.kind in {
            "chat_created",
            "chat_archived",
            "chat_unarchived",
            "chat_title_changed",
            "chat_duplicated",
            # Phase 11 M3 (F4/F5/F7): organisation and deletion events also
            # re-render the rail from the authoritative listing.
            "chat_folder_changed",
            "chat_pin_changed",
            "chat_deleted",
        }:
            chats = await self._application.list_chats()
            self._replace_chat_list(chats)
            if chat_id == self._current_chat_id:
                self._current_chat = next(
                    (chat for chat in chats if chat.id == self._current_chat_id),
                    self._current_chat,
                )
                self._sync_chat_header(self._current_chat)
        if event.kind in {"folder_created", "folder_renamed", "folder_deleted"}:
            await self._refresh_folders()
            if event.kind == "folder_deleted":
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
        if event.kind in _SEARCH_SOURCE_EVENT_KINDS and self.search_dock.isVisible():
            await self._refresh_search_status()

    async def _refresh_transcript(self, chat_id: str) -> None:
        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            if self._historical_leaf_message_id is None:
                chat, messages = await self._application.open_chat(chat_id)
            else:
                chat, messages = await self._application.open_chat(
                    chat_id,
                    head_message_id=self._historical_leaf_message_id,
                )
        except Exception:
            if self._historical_leaf_message_id is None:
                return
            # Persisted historical selection is advisory.  If its exact leaf
            # no longer exists, reopen the authoritative active head now;
            # leaving the previous transcript empty would make a valid chat
            # unusable until a later unrelated refresh.
            self._set_historical_leaf(None)
            try:
                chat, messages = await self._application.open_chat(chat_id)
            except Exception:
                return
        if generation != self._refresh_generation or chat_id != self._current_chat_id:
            return
        self._render_transcript_projection(chat, messages)
        await self._refresh_message_model_identities(chat_id, generation)
        await self._refresh_message_errors(chat_id, generation)
        if self.inspector_dock.isVisible():
            await self._refresh_inspector()

    async def _refresh_message_model_identities(self, chat_id: str, generation: int) -> None:
        """Show only identity belonging to each message's persisted attempt."""
        list_attempts = getattr(self._application, "list_generation_attempts", None)
        if not callable(list_attempts):
            return
        try:
            attempts = await list_attempts(chat_id)
        except Exception:
            return
        if generation != self._refresh_generation or chat_id != self._current_chat_id:
            return
        identities: dict[str, set[str]] = {}
        for attempt in attempts:
            identity = getattr(attempt, "returned_model", None) or getattr(attempt, "model", None)
            if identity:
                identities.setdefault(attempt.assistant_message_id, set()).add(identity)
        for message_id, values in identities.items():
            row = self.transcript.message_rows.get(message_id)
            if row is not None and row.message.role is MessageRole.ASSISTANT and len(values) == 1:
                identity = next(iter(values))
                row.model_identity_label.setText(identity)
                row.model_identity_label.setToolTip(identity)
                row.model_identity_label.setVisible(True)

    async def _refresh_message_errors(self, chat_id: str, generation: int) -> None:
        """Give failed turns the same display-safe reason as Inspect details."""
        inspect = getattr(self._application, "inspect_chat", None)
        if not callable(inspect):
            return
        failed = tuple(row.message.id for row in self.transcript.message_rows.values() if row.message.state is MessageState.FAILED)
        for message_id in failed:
            try:
                projection = await inspect(chat_id, message_id=message_id, historical_leaf_message_id=self._historical_leaf_message_id)
            except Exception:
                # The existing Inspect action remains available if the
                # optional presentation refresh cannot obtain its projection.
                continue
            if generation != self._refresh_generation or chat_id != self._current_chat_id:
                return
            row = self.transcript.message_rows.get(message_id)
            if row is not None and projection.selected_message_id == message_id:
                reasons = [field.value for field in projection.fields if field.name.startswith("Attempt ") and field.name.endswith(" error") and field.value != "none"]
                row.show_error_detail("\n".join(reasons))

    def _render_transcript_projection(
        self,
        chat: Chat,
        messages: tuple[Message, ...] | list[Message],
        *,
        focus_message_id: str | None = None,
    ) -> None:
        self._current_chat = chat
        self._current_messages = tuple(messages)
        self._sync_chat_header(chat)
        self.transcript.render(
            messages,
            self._generation_busy,
            historical_leaf_message_id=self._historical_leaf_message_id,
        )
        for row in self.transcript.message_rows.values():
            row.copy_requested.connect(self._on_copy_message)
            row.edit_requested.connect(self._on_edit_message)
            row.inspect_requested.connect(self._on_inspect_message)
            row.regenerate_requested.connect(self._on_regenerate_message)
        if focus_message_id is not None:
            self.transcript.focus_message(focus_message_id)
        if self._selected_message is not None:
            self._selected_message = next(
                (message for message in self._current_messages if message.id == self._selected_message.id),
                None,
            )
        self._update_controls()

    def _sync_chat_header(self, chat: Chat | None) -> None:
        if chat is None:
            self.chat_title.setText("New chat")
            self.archived_badge.setVisible(False)
            self.archive_button.setText("Archive")
            self.archive_button.setToolTip("Archive this chat")
            return
        self.chat_title.setText(chat.title)
        archived = chat.archived_at is not None
        self.archived_badge.setVisible(archived)
        self.archive_button.setText("Unarchive" if archived else "Archive")
        self.archive_button.setToolTip(
            "Return this chat to active status" if archived else "Archive this chat"
        )

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
            inspector_open=self.inspector_dock.isVisible(),
            inspector_message_id=(None if self._selected_message is None else self._selected_message.id),
            inspector_leaf_message_id=self._historical_leaf_message_id,
            maximized=self.isMaximized(),
            transcript_scroll_position=self.transcript.scroll_position,
            # Phase 11 fork R-15: the search presentation plane.  isHidden()
            # (not isVisible()) so the open flag survives saving while the
            # top-level window itself is still hidden, e.g. during restore.
            search_open=not self.search_dock.isHidden(),
            search_query=self._last_search_query,
            search_filters=self._last_search_filters,
            search_cursor=self._next_search_cursor,
        )

    async def _refresh_inspector(self) -> None:
        if self._current_chat is None:
            return
        projection = await self._application.inspect_chat(
            self._current_chat.id,
            message_id=(None if self._selected_message is None else self._selected_message.id),
            historical_leaf_message_id=self._historical_leaf_message_id,
        )
        self.inspector.show_projection(projection)

    def _toggle_inspector(self, visible: bool) -> None:
        self.inspector_dock.setVisible(visible)
        self._schedule(self._save_workspace())
        if visible:
            self._schedule(self._refresh_inspector())

    def _sync_inspector_button(self, visible: bool) -> None:
        self._fit_workspace_panels()
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
        phase9 = getattr(self, "_phase9", None)
        if phase9 is not None:
            phase9.close()

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
        # Phase 10 M2.0b: drain the campaign dock if present
        if self._campaign_dock is not None:
            await self._campaign_dock.drain()
        # v0.2: drain the control plane dock if present
        if self._control_dock is not None:
            await self._control_dock.drain()
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
                    inspector_open=self.inspector_dock.isVisible(),
                    inspector_message_id=(None if self._selected_message is None else self._selected_message.id),
                    inspector_leaf_message_id=self._historical_leaf_message_id,
                    maximized=self.isMaximized(),
                    transcript_scroll_position=self.transcript.scroll_position,
                    search_open=not self.search_dock.isHidden(),
                    search_query=self._last_search_query,
                    search_filters=self._last_search_filters,
                    search_cursor=self._next_search_cursor,
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
                inspector_open=self.inspector_dock.isVisible(),
                inspector_message_id=(None if self._selected_message is None else self._selected_message.id),
                inspector_leaf_message_id=self._historical_leaf_message_id,
                maximized=self.isMaximized(),
                transcript_scroll_position=self.transcript.scroll_position,
                search_open=not self.search_dock.isHidden(),
                search_query=self._last_search_query,
                search_filters=self._last_search_filters,
                search_cursor=self._next_search_cursor,
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
            answer = normalized_question(
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
        # Phase 11 M4b: close is a checkpoint — persist the active chat's
        # draft and the final dock layout before scheduled tasks are torn
        # down, so nothing still in the composer or docks is lost.
        if self._current_chat_id is not None:
            await self._persist_chat_draft(
                self._current_chat_id, self.composer.toPlainText()
            )
        if self._window_id is not None:
            await self._persist_dock_layout()
        self.stop_bridge()
        tasks = tuple(self._refresh_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Phase 10 M2.0b: drain the campaign dock if present
        if self._campaign_dock is not None:
            await self._campaign_dock.drain()
        # v0.2: drain the control plane dock if present
        if self._control_dock is not None:
            await self._control_dock.drain()
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
                inspector_open=self.inspector_dock.isVisible(),
                inspector_message_id=(None if self._selected_message is None else self._selected_message.id),
                inspector_leaf_message_id=self._historical_leaf_message_id,
                maximized=self.isMaximized(),
                transcript_scroll_position=self.transcript.scroll_position,
                search_open=not self.search_dock.isHidden(),
                search_query=self._last_search_query,
                search_filters=self._last_search_filters,
                search_cursor=self._next_search_cursor,
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
                inspector_open=self.inspector_dock.isVisible(),
                inspector_message_id=(None if self._selected_message is None else self._selected_message.id),
                inspector_leaf_message_id=self._historical_leaf_message_id,
                maximized=self.isMaximized(),
                transcript_scroll_position=self.transcript.scroll_position,
                search_open=not self.search_dock.isHidden(),
                search_query=self._last_search_query,
                search_filters=self._last_search_filters,
                search_cursor=self._next_search_cursor,
            )
            self._window_id = None
            await self._workspace.close()
        self.closed.emit()
