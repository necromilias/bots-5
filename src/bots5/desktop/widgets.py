from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bots5.core.inspection import InspectionProjection
from bots5.domain.models import (
    Chat,
    ChatActivity,
    ChatDeletionInventory,
    Folder,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)
from bots5.domain.provider import (
    CredentialRequirement,
    CredentialSource,
    builtin_connection_definitions,
    connection_definition,
)
from bots5.domain.generation_settings_registry import SETTING_CAPABILITY_KEYS

from .dialog_primitives import (
    ChamferedPanel,
    DialogInstrumentStrip,
    GenerationSettingsEditor,
    SectionHeader,
    StateBadge,
    fit_dialog_to_screen,
    scrollable,
)
from .markdown import MarkdownRenderer, SafeAttachmentResolver
from .model_selector import ModelSelectorButton, ModelSelectorEntry
from .profile import DesktopSessionInfo


def continuation_readiness_needs_resolution(readiness: object) -> bool:
    """Workflow 5 intercept predicate: exactly the landed core gate mirrored.

    The landed send/regenerate gate (``application.py:1954-1955`` and
    ``:2213-2214``) refuses a continuation unless ``choice_revision >= 1``,
    ``local_model_entry_id is not None`` and ``resolution != "UNAVAILABLE"``.
    Readiness usable without a prompt is therefore only ``None`` (native
    history or no anchor) or a readiness that passes that same predicate.
    This is a presentation-side mirror only: the core gate stays the
    authority and is never bypassed.
    """

    return readiness is not None and (
        readiness.choice_revision < 1
        or readiness.local_model_entry_id is None
        or readiness.resolution == "UNAVAILABLE"
    )


class ContinuationBanner(QFrame):
    """Read-only banner shown while an imported continuation is unresolved.

    Presentation only (I1): the window shows it when the workflow-5 intercept
    opens the resolution dialog and hides it again when the chat changes, the
    choice is admitted, or the operator cancels.  It carries no commands.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("continuationBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 6, 9, 6)
        self.label = QLabel("", self)
        self.label.setObjectName("continuationBannerLabel")
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self._chat_id: str | None = None
        self.setVisible(False)

    def show_for(self, chat_id: str, message: str) -> None:
        self._chat_id = chat_id
        self.label.setText(message)
        self.setVisible(True)

    def clear_for(self, chat_id: str) -> None:
        if self._chat_id == chat_id:
            self._chat_id = None
            self.label.setText("")
            self.setVisible(False)

    def clear(self) -> None:
        self._chat_id = None
        self.label.setText("")
        self.setVisible(False)


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
    search_toggled = Signal(bool)
    details_toggled = Signal(bool)
    model_selected = Signal(str)
    tune_requested = Signal()
    settings_requested = Signal()

    def __init__(self, session: DesktopSessionInfo, parent: QWidget | None = None, *, phase5: bool = False) -> None:
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
            "Current durable chat model selection."
        )
        layout.addWidget(self.model_pill)

        self.model_selector = ModelSelectorButton(self)
        self.model_selector.setAccessibleName("Current model")
        self.model_selector.setMinimumWidth(250)
        self.model_selector.setVisible(phase5)
        self.model_selector.currentIndexChanged.connect(self._model_index_changed)
        layout.addWidget(self.model_selector)

        self.tune_button = self._action_button("Tune", "Tune settings for this chat and model") if phase5 else self._disabled_button(
            "Tune", "Model tuning is not configurable in the current legacy runtime."
        )
        if phase5:
            self.tune_button.clicked.connect(lambda: self.tune_requested.emit())
        layout.addWidget(self.tune_button)

        layout.addStretch(1)

        self.search_button = self._action_button(
            "Search",
            "Search chats, messages, and referenced attachments",
        )
        self.search_button.setObjectName("searchToggle")
        self.search_button.setCheckable(True)
        self.search_button.toggled.connect(self.search_toggled)
        layout.addWidget(self.search_button)

        self.settings_button = self._action_button("⚙", "Open provider and model settings") if phase5 else self._disabled_button(
            "⚙", "Provider and settings management are unavailable in the legacy runtime."
        )
        self.settings_button.setAccessibleName("Settings")
        if phase5:
            self.settings_button.clicked.connect(lambda: self.settings_requested.emit())
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

    @staticmethod
    def _action_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("actionAffordance")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    def _model_index_changed(self, index: int) -> None:
        model_entry_id = self.model_selector.model_entry_id_at(index)
        if isinstance(model_entry_id, str) and model_entry_id:
            self.model_selected.emit(model_entry_id)

    def set_models(
        self,
        entries: Iterable[tuple[str, str]],
        selected_model_entry_id: str | None,
        records: Iterable[ModelSelectorEntry] | None = None,
    ) -> None:
        """Populate the model selector.

        ``records`` carries the rich Phase 11 F8 presentation (display_name
        primary line, connection + context window secondary line, health,
        metadata).  When it is omitted the landed flat ``(label, id)`` shape
        is rendered as-is, preserving the pinned Phase 5 selector behaviour.
        """

        if records is None:
            selector_entries = [
                ModelSelectorEntry(model_entry_id=model_entry_id, display_name=label)
                for label, model_entry_id in entries
            ]
        else:
            selector_entries = list(records)
        self.model_selector.set_entries(selector_entries, selected_model_entry_id)


class SearchPanel(QWidget):
    """Reusable global/in-chat search controls and result presentation."""

    search_requested = Signal(object)
    load_more_requested = Signal()
    result_requested = Signal(object, int)
    rebuild_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Search", self)
        title.setObjectName("searchTitle")
        layout.addWidget(title)

        self.query_edit = QLineEdit(self)
        self.query_edit.setObjectName("searchQuery")
        self.query_edit.setAccessibleName("Literal search query")
        self.query_edit.setPlaceholderText("Search literal text")
        self.query_edit.setClearButtonEnabled(True)
        self.query_edit.returnPressed.connect(self._submit)
        self.query_edit.textChanged.connect(self._sync_search_enabled)
        layout.addWidget(self.query_edit)

        form = QFormLayout()
        self.scope_combo = QComboBox(self)
        self.scope_combo.setObjectName("searchScope")
        self.scope_combo.setAccessibleName("Search scope")
        self.scope_combo.addItem("Every chat", "global")
        self.scope_combo.addItem("Current chat", "chat")
        form.addRow("Scope", self.scope_combo)

        kind_widget = QWidget(self)
        kind_layout = QHBoxLayout(kind_widget)
        kind_layout.setContentsMargins(0, 0, 0, 0)
        kind_layout.setSpacing(5)
        self.chat_kind = QCheckBox("Chats", kind_widget)
        self.chat_kind.setObjectName("searchKindChats")
        self.message_kind = QCheckBox("Messages", kind_widget)
        self.message_kind.setObjectName("searchKindMessages")
        self.attachment_kind = QCheckBox("Attachments", kind_widget)
        self.attachment_kind.setObjectName("searchKindAttachments")
        for checkbox in (self.chat_kind, self.message_kind, self.attachment_kind):
            checkbox.setChecked(True)
            kind_layout.addWidget(checkbox)
        form.addRow("Kinds", kind_widget)

        self.role_combo = QComboBox(self)
        self.role_combo.setObjectName("searchRole")
        self.role_combo.setAccessibleName("Message role filter")
        self.role_combo.addItem("Any role", None)
        self.role_combo.addItem("User", MessageRole.USER.value)
        self.role_combo.addItem("Assistant", MessageRole.ASSISTANT.value)
        form.addRow("Role", self.role_combo)

        self.state_combo = QComboBox(self)
        self.state_combo.setObjectName("searchState")
        self.state_combo.setAccessibleName("Message state filter")
        self.state_combo.addItem("Any durable state", None)
        for state in MessageState:
            self.state_combo.addItem(state.value.replace("_", " ").title(), state.value)
        form.addRow("State", self.state_combo)
        layout.addLayout(form)

        self.active_only = QCheckBox("Active branch only", self)
        self.active_only.setObjectName("searchActiveBranchOnly")
        self.active_only.setToolTip("Exclude surviving revisions and regeneration siblings outside the current branch")
        layout.addWidget(self.active_only)

        self.include_archived = QCheckBox("Include archived", self)
        self.include_archived.setObjectName("searchIncludeArchived")
        self.include_archived.setToolTip("Archived chats are excluded unless this filter is selected")
        layout.addWidget(self.include_archived)

        action_row = QHBoxLayout()
        self.search_button = QPushButton("Search", self)
        self.search_button.setObjectName("searchButton")
        self.search_button.clicked.connect(self._submit)
        action_row.addWidget(self.search_button)
        self.rebuild_button = QPushButton("Rebuild index", self)
        self.rebuild_button.setObjectName("searchRebuildButton")
        self.rebuild_button.setToolTip("Deterministically rebuild the derived search index")
        self.rebuild_button.clicked.connect(lambda: self.rebuild_requested.emit())
        action_row.addWidget(self.rebuild_button)
        layout.addLayout(action_row)

        self.status_label = QLabel("Search status has not been checked", self)
        self.status_label.setObjectName("searchStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        self.results = QListWidget(self)
        self.results.setObjectName("searchResults")
        self.results.setAccessibleName("Search results")
        self.results.itemActivated.connect(self._activate_item)
        layout.addWidget(self.results, 1)

        self.load_more_button = QPushButton("Load more", self)
        self.load_more_button.setObjectName("searchLoadMoreButton")
        self.load_more_button.clicked.connect(lambda: self.load_more_requested.emit())
        self.load_more_button.setVisible(False)
        layout.addWidget(self.load_more_button)
        self._condition = "UNKNOWN"
        self._sync_search_enabled()

    def request_payload(self) -> dict[str, object]:
        kinds = []
        if self.chat_kind.isChecked():
            kinds.append("chat")
        if self.message_kind.isChecked():
            kinds.append("message")
        if self.attachment_kind.isChecked():
            kinds.append("attachment")
        return {
            "query": self.query_edit.text(),
            "scope": self.scope_combo.currentData(),
            "document_kinds": tuple(kinds),
            "role": self.role_combo.currentData(),
            "message_state": self.state_combo.currentData(),
            "active_branch_only": self.active_only.isChecked(),
            "include_archived": self.include_archived.isChecked(),
        }

    def set_in_chat_scope(self) -> None:
        self.scope_combo.setCurrentIndex(self.scope_combo.findData("chat"))

    def set_current_chat_available(self, available: bool) -> None:
        model = self.scope_combo.model()
        item = model.item(self.scope_combo.findData("chat")) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(available)
        if not available and self.scope_combo.currentData() == "chat":
            self.scope_combo.setCurrentIndex(self.scope_combo.findData("global"))

    def set_busy(self, busy: bool) -> None:
        self.query_edit.setEnabled(not busy)
        self.scope_combo.setEnabled(not busy)
        self.chat_kind.setEnabled(not busy)
        self.message_kind.setEnabled(not busy)
        self.attachment_kind.setEnabled(not busy)
        self.role_combo.setEnabled(not busy)
        self.state_combo.setEnabled(not busy)
        self.active_only.setEnabled(not busy)
        self.include_archived.setEnabled(not busy)
        self.search_button.setEnabled(
            not busy
            and self._search_allowed()
            and bool(self.query_edit.text().strip())
        )
        self.load_more_button.setEnabled(not busy)
        self.rebuild_button.setEnabled(
            not busy and self._condition not in {"REBUILDING", "UNAVAILABLE"}
        )
        if busy:
            self.status_label.setText("Searching…")

    def show_status(self, status: object) -> None:
        condition = getattr(getattr(status, "condition", None), "value", "UNKNOWN")
        source_revision = getattr(status, "source_revision", None)
        checkpoint_revision = getattr(status, "checkpoint_revision", None)
        detail = getattr(status, "detail", None)
        summary = f"{condition} — source {source_revision}, checkpoint {checkpoint_revision}"
        if detail:
            summary += f" — {detail}"
        self._condition = condition
        self.status_label.setText(summary)
        self.status_label.setProperty("condition", condition)
        self._refresh_status_style()
        self._sync_search_enabled()
        self.rebuild_button.setEnabled(condition not in {"REBUILDING", "UNAVAILABLE"})

    def show_error(self, condition: str, detail: str, *, clear_results: bool = True) -> None:
        self._condition = condition
        self.status_label.setText(f"{condition} — {detail}")
        self.status_label.setProperty("condition", condition)
        self._refresh_status_style()
        self._sync_search_enabled()
        if clear_results:
            self.results.clear()
            self.load_more_button.setVisible(False)
        self.rebuild_button.setEnabled(condition not in {"REBUILDING", "UNAVAILABLE"})

    def show_pagination_error(self, detail: str) -> None:
        """Report an expired result page without changing index validity."""

        self.status_label.setText(f"{self._condition} — pagination expired — {detail}")
        self.status_label.setProperty("condition", self._condition)
        self._refresh_status_style()
        self._sync_search_enabled()
        self.load_more_button.setVisible(False)
        self.rebuild_button.setEnabled(
            self._condition not in {"REBUILDING", "UNAVAILABLE"}
        )

    def show_page(self, page: object, *, append: bool = False) -> None:
        if not append:
            self.results.clear()
        for result in getattr(page, "results", ()):
            locations = tuple(getattr(result, "locations", ())) or (None,)
            for location_index, location in enumerate(locations):
                item = QListWidgetItem(self._result_text(result, location))
                item.setData(Qt.ItemDataRole.UserRole, (result, location_index))
                item.setToolTip("Open this exact authoritative result")
                self.results.addItem(item)
        self.load_more_button.setVisible(bool(getattr(page, "next_cursor", None)))
        self.show_status(getattr(page, "status", None))
        if self.results.count() == 0:
            self.status_label.setText(f"{self.status_label.text()} — no results")

    def focus_query(self) -> None:
        self.query_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query_edit.selectAll()

    def _submit(self) -> None:
        if not self.query_edit.text().strip():
            self.show_error("INVALID QUERY", "Enter non-empty literal text")
            return
        if not any(
            checkbox.isChecked()
            for checkbox in (self.chat_kind, self.message_kind, self.attachment_kind)
        ):
            self.show_error("INVALID QUERY", "Select at least one document kind")
            return
        self.search_requested.emit(self.request_payload())

    def _sync_search_enabled(self, *_args) -> None:
        self.search_button.setEnabled(
            self.query_edit.isEnabled()
            and self._search_allowed()
            and bool(self.query_edit.text().strip())
        )

    def _search_allowed(self) -> bool:
        return self._condition not in {"STALE", "REBUILDING", "UNAVAILABLE", "INVALID"}

    def _activate_item(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(payload, tuple) and len(payload) == 2:
            result, location_index = payload
            self.result_requested.emit(result, int(location_index))

    def _refresh_status_style(self) -> None:
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    @staticmethod
    def _result_text(result: object, location: object | None) -> str:
        kind = getattr(getattr(result, "document_kind", None), "value", "result")
        title = str(getattr(result, "title", "") or getattr(result, "document_id", ""))
        badges = [kind.title()]
        branch_state = getattr(getattr(location, "branch_state", None), "value", None)
        if branch_state == "historical":
            badges.append("Historical")
        archived_at = getattr(location, "archived_at", None)
        if archived_at is not None:
            badges.append("Archived")
        revision = getattr(result, "revision", None)
        if revision is not None:
            badges.append(f"revision {revision}")
        snippet = str(getattr(result, "snippet", "") or "").strip()
        prefix = " ".join(f"[{badge}]" for badge in badges)
        return f"{prefix} {title}\n{snippet}" if snippet else f"{prefix} {title}"


class TuneDialog(QDialog):
    save_requested = Signal(object)
    inherit_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tune")
        self.setObjectName("tuneDialog")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        self._editor_panel = ChamferedPanel(self)
        editor_layout = QVBoxLayout(self._editor_panel)
        editor_layout.setContentsMargins(10, 8, 10, 8)
        editor_layout.setSpacing(4)
        self._instrument_strip = DialogInstrumentStrip(self._editor_panel)
        editor_layout.addWidget(self._instrument_strip)
        form = QFormLayout()
        self.temperature_inherited = QCheckBox("Use inherited temperature", self)
        self.temperature = QDoubleSpinBox(self)
        self.temperature.setObjectName("temperatureSetting")
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setDecimals(2)
        self.max_output_inherited = QCheckBox("Use inherited max output", self)
        self.max_output_tokens = QSpinBox(self)
        self.max_output_tokens.setObjectName("maxOutputTokensSetting")
        self.max_output_tokens.setRange(1, 1_000_000)
        self.reasoning_inherited = QCheckBox("Use inherited reasoning", self)
        self.reasoning_none = QCheckBox("Reasoning off (none)", self)
        self.provenance_label = QLabel("", self)
        self.provenance_label.setObjectName("settingsProvenance")
        self.provenance_label.setWordWrap(True)
        self._model_entry_id: str | None = None
        self._override_revision: int | None = None
        self._extra_revision: int | None = None
        self._timeout_override: float | None = None
        form.addRow("Temperature", self.temperature)
        form.addRow("", self.temperature_inherited)
        form.addRow("Max output tokens", self.max_output_tokens)
        form.addRow("", self.max_output_inherited)
        form.addRow("Reasoning", self.reasoning_none)
        form.addRow("", self.reasoning_inherited)
        form.addRow("Resolved from", self.provenance_label)
        editor_layout.addLayout(form)
        # Phase 11 scope amendment: registry-driven extended generation
        # settings, gated per capability.  Unsupported or unknown settings
        # stay visible and disabled with a reason; stored-but-inactive values
        # are preserved and shown as inactive.
        self.generation_settings_editor = GenerationSettingsEditor(self._editor_panel)
        self.generation_settings_editor.setObjectName("tuneGenerationSettings")
        editor_layout.addWidget(self.generation_settings_editor)
        # The settings/control region scrolls vertically; the dialog stays
        # inside the work area and the primary actions remain reachable.
        layout.addWidget(scrollable(self._editor_panel, self), 1)
        self.temperature_inherited.toggled.connect(self.temperature.setDisabled)
        self.max_output_inherited.toggled.connect(self.max_output_tokens.setDisabled)
        self.reasoning_inherited.toggled.connect(self.reasoning_none.setDisabled)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        self.use_inherited_button = buttons.addButton("Use inherited", QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.use_inherited_button.clicked.connect(self._use_inherited)
        layout.addWidget(buttons)
        fit_dialog_to_screen(self)

    def set_settings(
        self,
        settings: Mapping[str, object],
        provenance: Mapping[str, str],
        *,
        timeout_override: float | None = None,
        override_revision: int | None = None,
        extra_revision: int | None = None,
        model_entry_id: str | None = None,
        extra_values: Mapping[str, object] | None = None,
        capabilities: Mapping[str, object] | None = None,
        model_summary: str = "",
    ) -> None:
        self.temperature.setValue(float(settings.get("temperature", 0.0)))
        self.max_output_tokens.setValue(int(settings.get("max_output_tokens", 1024)))
        self.reasoning_none.setChecked(settings.get("reasoning_effort") == "none")
        # Timeout is deliberately outside this compact Tune surface. Preserve
        # an existing exact chat override when another Tune setting is saved;
        # the explicit inherit action clears it.
        self._timeout_override = timeout_override
        self._model_entry_id = model_entry_id
        self._override_revision = override_revision
        self._extra_revision = extra_revision
        self.provenance_label.setText(", ".join(f"{key}: {value}" for key, value in provenance.items()) or "defaults")
        self.temperature_inherited.setChecked(provenance.get("temperature") != "chat_model")
        self.max_output_inherited.setChecked(provenance.get("max_output_tokens") != "chat_model")
        self.reasoning_inherited.setChecked(provenance.get("reasoning_effort") != "chat_model")
        self.generation_settings_editor.set_state(
            values=extra_values or {},
            effective=settings,
            provenance=provenance,
            capabilities=capabilities,
        )
        self._instrument_strip.set_instruments(
            [("model", model_summary or (model_entry_id or "none"))]
        )

    def _save(self) -> None:
        extra = self.generation_settings_editor.override_payload()
        # Fail closed on unparseable override text: refuse the whole save.
        self.save_requested.emit({
            "temperature": None if self.temperature_inherited.isChecked() else self.temperature.value(),
            "max_output_tokens": None if self.max_output_inherited.isChecked() else self.max_output_tokens.value(),
            "reasoning_effort": None if self.reasoning_inherited.isChecked() else ("none" if self.reasoning_none.isChecked() else None),
            "timeout_seconds": self._timeout_override,
            "extra": {} if self.generation_settings_editor.has_invalid_values() else extra,
            "extra_invalid": self.generation_settings_editor.has_invalid_values(),
            "expected_revision": self._override_revision,
            "expected_extra_revision": self._extra_revision,
            "expected_model_entry_id": self._model_entry_id,
        })
        self.accept()

    def _use_inherited(self) -> None:
        self.inherit_requested.emit({
            "temperature": None,
            "max_output_tokens": None,
            "reasoning_effort": None,
            "timeout_seconds": None,
            "extra": {},
            "expected_revision": self._override_revision,
            "expected_extra_revision": self._extra_revision,
            "expected_model_entry_id": self._model_entry_id,
        })
        self.accept()


class AddConnectionDialog(QDialog):
    """Compact operator workflow for one of the closed built-in connection shapes."""

    save_requested = Signal(object)
    save_refresh_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Connection")
        self.setObjectName("addConnectionDialog")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose a built-in provider shape, enter its connection details, and save it. "
            "Saving does not contact the provider.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.connection_definition = QComboBox(self)
        self.connection_definition.setObjectName("connectionDefinition")
        form.addRow("Connection type", self.connection_definition)

        self.connection_backend_value = QLabel(self)
        self.connection_backend_value.setObjectName("connectionBackendValue")
        form.addRow("Backend", self.connection_backend_value)
        self.connection_profile_value = QLabel(self)
        self.connection_profile_value.setObjectName("connectionProfileValue")
        form.addRow("Profile", self.connection_profile_value)

        self.connection_name = QLineEdit(self)
        self.connection_name.setObjectName("addConnectionName")
        self.connection_name.setPlaceholderText("e.g. Local Ollama")
        form.addRow("Name", self.connection_name)

        self.connection_endpoint_label = QLabel("Base URL", self)
        self.connection_endpoint = QLineEdit(self)
        self.connection_endpoint.setObjectName("addConnectionEndpoint")
        self.connection_endpoint.setPlaceholderText("https://api.example.test/v1")
        form.addRow(self.connection_endpoint_label, self.connection_endpoint)

        self.connection_credential_source_label = QLabel("Authentication", self)
        self.connection_credential_source = QComboBox(self)
        self.connection_credential_source.setObjectName("addConnectionCredentialSource")
        form.addRow(self.connection_credential_source_label, self.connection_credential_source)

        self.connection_credential_reference_label = QLabel("Credential reference", self)
        self.connection_credential_reference = QLineEdit(self)
        self.connection_credential_reference.setObjectName("addConnectionCredentialReference")
        self.connection_credential_reference.setPlaceholderText("Environment variable or Secret Service reference")
        form.addRow(self.connection_credential_reference_label, self.connection_credential_reference)

        self.connection_credential_value_label = QLabel("Credential", self)
        self.connection_credential_value = QLineEdit(self)
        self.connection_credential_value.setObjectName("addConnectionCredentialValue")
        self.connection_credential_value.setEchoMode(QLineEdit.EchoMode.Password)
        self.connection_credential_value.setPlaceholderText("Stored without being shown again")
        form.addRow(self.connection_credential_value_label, self.connection_credential_value)
        layout.addLayout(form)

        self.requirement_label = QLabel(self)
        self.requirement_label.setObjectName("connectionDefinitionHelp")
        self.requirement_label.setWordWrap(True)
        layout.addWidget(self.requirement_label)
        self.validation_label = QLabel(self)
        self.validation_label.setObjectName("addConnectionValidation")
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setObjectName("saveConnectionButton")
        self.save_refresh_button = buttons.addButton("Save & Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        self.save_refresh_button.setObjectName("saveAndRefreshConnectionButton")
        buttons.accepted.connect(lambda: self._submit(False))
        buttons.rejected.connect(self.reject)
        self.save_refresh_button.clicked.connect(lambda: self._submit(True))
        layout.addWidget(buttons)

        self._default_endpoint: str | None = None
        self._default_name: str | None = None
        self._definitions = {}
        self.connection_definition.currentIndexChanged.connect(self._definition_changed)
        self.connection_credential_source.currentIndexChanged.connect(self._credential_source_changed)
        for definition in builtin_connection_definitions():
            self._definitions[definition.key] = definition
            self.connection_definition.addItem(definition.display_name, definition.key)
        if self.connection_definition.count():
            self.connection_definition.setCurrentIndex(0)

    def _selected_definition(self):
        key = self.connection_definition.currentData()
        return self._definitions.get(key) or connection_definition(str(key))

    @staticmethod
    def _credential_label(source: CredentialSource) -> str:
        return {
            CredentialSource.NONE: "None",
            CredentialSource.ENVIRONMENT: "Environment variable",
            CredentialSource.SECRET_SERVICE: "Secret Service",
        }[source]

    def _definition_changed(self, *_args) -> None:
        definition = self._selected_definition()
        endpoint = self.connection_endpoint.text().strip()
        if not endpoint or endpoint == (self._default_endpoint or ""):
            self.connection_endpoint.setText(definition.default_endpoint or "")
        self._default_endpoint = definition.default_endpoint
        name = self.connection_name.text().strip()
        previous_default_name = self._default_name
        if not name or name == (previous_default_name or ""):
            self.connection_name.setText(
                "Deterministic fake" if definition.key == "fake" else definition.display_name
            )
            self._default_name = self.connection_name.text()
        self.connection_backend_value.setText(definition.backend_type.value)
        self.connection_profile_value.setText(definition.profile.value)
        self.connection_endpoint_label.setVisible(definition.endpoint_required)
        self.connection_endpoint.setVisible(definition.endpoint_required)
        self._populate_credential_sources(definition)
        self.requirement_label.setText(self._definition_help(definition))

    def _definition_help(self, definition) -> str:
        if definition.credential_requirement is CredentialRequirement.NOT_USED:
            auth = "This connection does not use authentication."
        elif definition.credential_requirement is CredentialRequirement.REQUIRED:
            auth = "Authentication is required; only a reference and status are durable."
        else:
            auth = "Authentication is optional; environment names and Secret Service references are durable, never values."
        discovery = (
            "Catalogue discovery is available through the explicit Refresh action."
            if definition.discovery_kind.value != "none"
            else "Catalogue discovery is not available for this connection shape."
        )
        return f"{auth} {discovery}"

    def _populate_credential_sources(self, definition) -> None:
        current = self.connection_credential_source.currentData()
        self.connection_credential_source.blockSignals(True)
        try:
            self.connection_credential_source.clear()
            for source in definition.credential_sources:
                self.connection_credential_source.addItem(self._credential_label(source), source.value)
            index = self.connection_credential_source.findData(current)
            if index < 0:
                index = 0
            self.connection_credential_source.setCurrentIndex(index)
        finally:
            self.connection_credential_source.blockSignals(False)
        self._credential_source_changed()

    def _credential_source_changed(self, *_args) -> None:
        source = CredentialSource(str(self.connection_credential_source.currentData()))
        visible = source is not CredentialSource.NONE
        secret_value_visible = source is CredentialSource.SECRET_SERVICE
        self.connection_credential_reference_label.setVisible(visible)
        self.connection_credential_reference.setVisible(visible)
        self.connection_credential_value_label.setVisible(secret_value_visible)
        self.connection_credential_value.setVisible(secret_value_visible)
        if not secret_value_visible:
            self.connection_credential_value.clear()
        if not visible:
            self.connection_credential_reference.clear()
        self.connection_credential_source_label.setText(
            "Authentication" if source is not CredentialSource.NONE else "Authentication"
        )

    def _submit(self, refresh: bool) -> None:
        definition = self._selected_definition()
        name = self.connection_name.text().strip()
        endpoint = self.connection_endpoint.text().strip() or None
        source = CredentialSource(str(self.connection_credential_source.currentData()))
        reference = (
            self.connection_credential_reference.text().strip() or None
            if source is not CredentialSource.NONE
            else None
        )
        if not name:
            self.validation_label.setText("Connection name is required.")
            self.connection_name.setFocus()
            return
        if definition.endpoint_required and not endpoint:
            self.validation_label.setText("A base URL is required for this connection type.")
            self.connection_endpoint.setFocus()
            return
        if source not in definition.credential_sources:
            self.validation_label.setText("That authentication source is not supported by this connection type.")
            return
        if definition.credential_requirement is CredentialRequirement.REQUIRED and source is CredentialSource.NONE:
            self.validation_label.setText("This connection type requires authentication.")
            return
        if source is not CredentialSource.NONE and not reference:
            self.validation_label.setText("A credential reference is required for the selected authentication source.")
            self.connection_credential_reference.setFocus()
            return
        credential_value = self.connection_credential_value.text() if source is CredentialSource.SECRET_SERVICE else None
        if source is CredentialSource.SECRET_SERVICE and not credential_value:
            self.validation_label.setText("Enter a credential to store in Secret Service.")
            self.connection_credential_value.setFocus()
            return
        values = {
            "definition_key": definition.key,
            "name": name,
            "backend_type": definition.backend_type.value,
            "profile": definition.profile.value,
            "endpoint": endpoint,
            "credential_source": source.value,
            "credential_reference": reference,
            "credential_value": credential_value,
        }
        self.validation_label.clear()
        self.save_button.setEnabled(False)
        self.save_refresh_button.setEnabled(False)
        # The plaintext is emitted only to the immediate save operation and is
        # removed from Qt state before control returns to the event loop.
        self.connection_credential_value.clear()
        if refresh:
            self.save_refresh_requested.emit(values)
        else:
            self.save_requested.emit(values)

    def submission_failed(self, message: str) -> None:
        self.validation_label.setText(message)
        self.save_button.setEnabled(True)
        self.save_refresh_button.setEnabled(True)

    def submission_succeeded(self) -> None:
        self.accept()


class SettingsDialog(QDialog):
    add_connection_requested = Signal()
    create_connection_requested = Signal(object)
    edit_connection_requested = Signal(object)
    refresh_requested = Signal(str)
    add_manual_model_requested = Signal(object)
    model_defaults_refresh_requested = Signal(str)
    model_defaults_requested = Signal(object)
    capability_override_requested = Signal(object)
    connection_enabled_requested = Signal(object)
    connection_retire_requested = Signal(object)
    application_default_model_requested = Signal(object)
    application_defaults_requested = Signal(object)
    credential_save_requested = Signal(object)
    credential_delete_requested = Signal(object)

    # Deliberate information architecture for the settings surface.  Each
    # section owns one bounded concern; the selected-object/detail
    # relationship is explicit (the left navigation selects the section, the
    # in-section lists select the object whose detail form is shown).
    SECTION_GENERAL = 0
    SECTION_PROVIDERS = 1
    SECTION_MODELS = 2
    SECTION_CREDENTIALS = 3
    SECTION_ADVANCED = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setObjectName("settingsDialog")
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        self.instrument_strip = DialogInstrumentStrip(self)
        layout.addWidget(self.instrument_strip)
        body = QHBoxLayout()
        layout.addLayout(body, 1)

        self.section_nav = QListWidget(self)
        self.section_nav.setObjectName("botsSectionNav")
        for section in ("General", "Providers", "Models", "Credentials", "Advanced"):
            self.section_nav.addItem(QListWidgetItem(section))
        self.section_nav.setFixedWidth(132)
        body.addWidget(self.section_nav)

        self.section_stack = QStackedWidget(self)
        body.addWidget(self.section_stack, 1)
        general_page = QWidget(self)
        providers_page = QWidget(self)
        models_page = QWidget(self)
        credentials_page = QWidget(self)
        advanced_page = QWidget(self)
        # Each detail pane scrolls independently; the navigation rail stays
        # outside the scroll areas, so it remains accessible while the pane
        # scrolls, and the dialog never grows past the available work area.
        self.section_stack.addWidget(scrollable(general_page, self.section_stack))
        self.section_stack.addWidget(scrollable(providers_page, self.section_stack))
        self.section_stack.addWidget(scrollable(models_page, self.section_stack))
        self.section_stack.addWidget(scrollable(credentials_page, self.section_stack))
        self.section_stack.addWidget(scrollable(advanced_page, self.section_stack))
        self.section_nav.currentRowChanged.connect(self.section_stack.setCurrentIndex)
        self.section_nav.setCurrentRow(self.SECTION_PROVIDERS)

        # --- Providers page ------------------------------------------------
        providers_layout = QVBoxLayout(providers_page)
        providers_layout.addWidget(SectionHeader(
            "Providers",
            "Connections and their model catalogues. Saving never contacts the provider.",
            providers_page,
        ))
        self.connection_list = QListWidget(providers_page)
        self.connection_list.setObjectName("connectionList")
        self.connection_list.currentItemChanged.connect(self._connection_changed)
        providers_layout.addWidget(self.connection_list, 1)
        connection_actions = QHBoxLayout()
        self.add_connection_button = QPushButton("+ Add Connection", self)
        self.add_connection_button.setObjectName("addConnectionButton")
        self.add_connection_button.setToolTip("Add a supported provider connection")
        connection_actions.addWidget(self.add_connection_button)
        connection_actions.addStretch(1)
        providers_layout.addLayout(connection_actions)
        connection_form = QFormLayout()
        self.connection_name = QLineEdit(self)
        self.connection_name.setObjectName("connectionName")
        self.connection_backend = QComboBox(self)
        self.connection_backend.setObjectName("connectionBackend")
        self.connection_backend.addItem("Fake", "fake")
        self.connection_backend.addItem("OpenAI-compatible HTTP", "openai_compatible_http")
        self.connection_profile = QComboBox(self)
        self.connection_profile.setObjectName("connectionProfile")
        self.connection_profile.addItem("Generic", "generic")
        self.connection_profile.addItem("OpenRouter", "openrouter")
        self.connection_endpoint = QLineEdit(self)
        self.connection_endpoint.setObjectName("connectionEndpoint")
        connection_form.addRow("Name", self.connection_name)
        connection_form.addRow("Backend", self.connection_backend)
        connection_form.addRow("Profile", self.connection_profile)
        connection_form.addRow("Endpoint", self.connection_endpoint)
        providers_layout.addLayout(connection_form)
        actions = QHBoxLayout()
        self.save_refresh_button = QPushButton("Save & Refresh", self)
        self.refresh_button = QPushButton("Refresh", self)
        self.enable_connection_button = QPushButton("Enable/disable", self)
        actions.addWidget(self.save_refresh_button)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.enable_connection_button)
        providers_layout.addLayout(actions)
        # Destructive / retirement actions are deliberately separated from the
        # routine actions above and require an explicit replacement model.
        self.retirement_panel = ChamferedPanel(providers_page, card=True, chamfer=4)
        retirement_layout = QVBoxLayout(self.retirement_panel)
        retirement_layout.setContentsMargins(8, 6, 8, 6)
        retirement_layout.setSpacing(2)
        self.retirement_label = QLabel("Retirement (destructive)", self.retirement_panel)
        self.retirement_label.setObjectName("botsSectionSubtitle")
        retirement_layout.addWidget(self.retirement_label)
        self.retirement_replacement_model = QComboBox(self)
        self.retirement_replacement_model.setObjectName("retirementReplacementModel")
        self.retirement_replacement_model.addItem("Choose replacement model", None)
        retirement_layout.addWidget(self.retirement_replacement_model)
        self.retire_connection_button = QPushButton("Retire connection", self)
        retirement_layout.addWidget(self.retire_connection_button)
        providers_layout.addWidget(self.retirement_panel)

        # --- Models page -----------------------------------------------------
        models_layout = QVBoxLayout(models_page)
        models_layout.addWidget(SectionHeader(
            "Models",
            "Model defaults inherit from the application defaults; chat overrides inherit from both.",
            models_page,
        ))
        self.model_list = QListWidget(models_page)
        self.model_list.setObjectName("modelCatalogueList")
        self.model_list.setMaximumHeight(110)
        self.model_list.currentItemChanged.connect(self._model_changed)
        models_layout.addWidget(self.model_list)
        manual = QHBoxLayout()
        self.manual_model_id = QLineEdit(self)
        self.manual_model_id.setObjectName("manualModelId")
        self.manual_model_id.setPlaceholderText("Exact provider model ID")
        self.add_manual_model_button = QPushButton("Add manual model", self)
        manual.addWidget(self.manual_model_id, 1)
        manual.addWidget(self.add_manual_model_button)
        models_layout.addLayout(manual)
        model_form = QFormLayout()
        self.model_defaults_inherited = QCheckBox("Use inherited model defaults", self)
        self.model_temperature_inherited = QCheckBox("Use inherited temperature", self)
        self.model_temperature = QDoubleSpinBox(self)
        self.model_temperature.setObjectName("modelDefaultTemperature")
        self.model_temperature.setRange(0.0, 2.0)
        self.model_temperature.setSingleStep(0.1)
        self.model_temperature.setDecimals(2)
        self.model_max_output_tokens = QSpinBox(self)
        self.model_max_output_tokens.setObjectName("modelDefaultMaxOutputTokens")
        self.model_max_output_tokens.setRange(1, 1_000_000)
        self.model_max_output_inherited = QCheckBox("Use inherited max output", self)
        self.model_reasoning = QComboBox(self)
        self.model_reasoning.setObjectName("modelDefaultReasoning")
        self.model_reasoning.addItem("Unset", None)
        self.model_reasoning.addItem("None", "none")
        self.model_reasoning_inherited = QCheckBox("Use inherited reasoning", self)
        model_form.addRow("Model defaults", self.model_defaults_inherited)
        model_form.addRow("Temperature", self.model_temperature)
        model_form.addRow("", self.model_temperature_inherited)
        model_form.addRow("Max output tokens", self.model_max_output_tokens)
        model_form.addRow("", self.model_max_output_inherited)
        model_form.addRow("Reasoning", self.model_reasoning)
        model_form.addRow("", self.model_reasoning_inherited)
        self.save_model_defaults_button = QPushButton("Save model defaults", self)
        model_form.addRow("", self.save_model_defaults_button)
        models_layout.addLayout(model_form)
        models_layout.addWidget(SectionHeader(
            "Extended generation settings",
            "Registry-driven defaults for this model. Gated controls show why they are inactive.",
            models_page,
        ))
        self.model_settings_editor = GenerationSettingsEditor(models_page)
        self.model_settings_editor.setObjectName("modelGenerationSettings")
        models_layout.addWidget(self.model_settings_editor, 1)
        models_layout.addStretch(0)

        # --- Credentials page ------------------------------------------------
        credentials_layout = QVBoxLayout(credentials_page)
        credentials_layout.addWidget(SectionHeader(
            "Credentials",
            "Credential material for the selected connection. Values never leave the secret store.",
            credentials_page,
        ))
        credential_form = QFormLayout()
        self.connection_credential_source = QComboBox(self)
        self.connection_credential_source.setObjectName("credentialSource")
        self.connection_credential_source.addItem("None", "none")
        self.connection_credential_source.addItem("Environment variable", "environment")
        self.connection_credential_source.addItem("Secret Service", "secret_service")
        self.connection_credential_reference = QLineEdit(self)
        self.connection_credential_reference.setObjectName("credentialReference")
        self.connection_credential_value = QLineEdit(self)
        self.connection_credential_value.setObjectName("credentialValue")
        self.connection_credential_value.setEchoMode(QLineEdit.EchoMode.Password)
        credential_form.addRow("Credential source", self.connection_credential_source)
        credential_form.addRow("Credential reference", self.connection_credential_reference)
        credential_form.addRow("Credential value", self.connection_credential_value)
        credentials_layout.addLayout(credential_form)
        credential_actions = QHBoxLayout()
        self.save_credential_button = QPushButton("Save credential", self)
        self.delete_credential_button = QPushButton("Delete credential", self)
        credential_actions.addWidget(self.save_credential_button)
        credential_actions.addWidget(self.delete_credential_button)
        credential_actions.addStretch(1)
        credentials_layout.addLayout(credential_actions)
        credentials_layout.addStretch(1)

        # --- Advanced page -----------------------------------------------------
        advanced_layout = QVBoxLayout(advanced_page)
        advanced_layout.addWidget(SectionHeader(
            "Advanced",
            "Capability overrides follow the existing precedence: manual over confirmed endpoint over provider metadata.",
            advanced_page,
        ))
        capability_form = QFormLayout()
        self.capability_key = QComboBox(self)
        self.capability_key.setObjectName("capabilityKey")
        for key in (
            "generation.streaming", "request.temperature", "request.max_output_tokens",
            "request.reasoning_effort.none", "limits.context_tokens", "limits.output_tokens",
            "telemetry.usage", "telemetry.reasoning_tokens", "telemetry.cost",
            "telemetry.request_id", "telemetry.returned_model",
        ):
            self.capability_key.addItem(key, key)
        # Registry-driven per-setting capability keys extend the closed
        # Phase 5 catalogue; they gate individual Tune controls.
        self._registry_capability_offset = self.capability_key.count()
        for key in sorted(SETTING_CAPABILITY_KEYS):
            self.capability_key.addItem(key, key)
        self.capability_state = QComboBox(self)
        self.capability_state.setObjectName("capabilityState")
        for state in ("supported", "unsupported", "unknown"):
            self.capability_state.addItem(state.title(), state)
        self.capability_value_set = QCheckBox("Set numeric value", self)
        self.capability_value_set.setObjectName("capabilityValueSet")
        self.capability_value = QSpinBox(self)
        self.capability_value.setObjectName("capabilityValue")
        self.capability_value.setRange(0, 2_147_483_647)
        self.capability_reason = QLineEdit(self)
        self.capability_reason.setObjectName("capabilityReason")
        self.set_capability_override_button = QPushButton("Save capability override", self)
        capability_form.addRow("Capability", self.capability_key)
        capability_form.addRow("State", self.capability_state)
        capability_form.addRow("Numeric limit", self.capability_value)
        capability_form.addRow("", self.capability_value_set)
        capability_form.addRow("Reason", self.capability_reason)
        capability_form.addRow("", self.set_capability_override_button)
        self.capability_summary_label = QLabel("Effective capabilities: not loaded", self)
        self.capability_summary_label.setObjectName("effectiveCapabilities")
        self.capability_summary_label.setWordWrap(True)
        self.capability_provenance_label = QLabel("Capability provenance: not loaded", self)
        self.capability_provenance_label.setObjectName("capabilityProvenance")
        self.capability_provenance_label.setWordWrap(True)
        capability_form.addRow("Effective", self.capability_summary_label)
        capability_form.addRow("Provenance", self.capability_provenance_label)
        advanced_layout.addLayout(capability_form)
        advanced_layout.addStretch(1)

        # --- General page ------------------------------------------------------
        general_layout = QVBoxLayout(general_page)
        general_layout.addWidget(SectionHeader(
            "General",
            "Application-wide defaults. Every model inherits these unless it overrides them.",
            general_page,
        ))
        application_form = QFormLayout()
        self.application_temperature = QDoubleSpinBox(self)
        self.application_temperature.setObjectName("applicationTemperature")
        self.application_temperature.setRange(0.0, 2.0)
        self.application_temperature.setSingleStep(0.1)
        self.application_temperature.setDecimals(2)
        self.application_max_output_tokens = QSpinBox(self)
        self.application_max_output_tokens.setObjectName("applicationMaxOutputTokens")
        self.application_max_output_tokens.setRange(1, 1_000_000)
        self.application_timeout = QDoubleSpinBox(self)
        self.application_timeout.setObjectName("applicationTimeout")
        self.application_timeout.setRange(0.0, 86_400.0)
        self.application_timeout.setSingleStep(1.0)
        self.application_timeout.setDecimals(2)
        self.application_timeout.setSpecialValueText("Unset")
        self.application_default_model = QComboBox(self)
        self.application_default_model.setObjectName("applicationDefaultModel")
        self.save_application_default_model_button = QPushButton("Save application default model", self)
        self.save_application_defaults_button = QPushButton("Save application defaults", self)
        application_form.addRow("Application default model", self.application_default_model)
        application_form.addRow("", self.save_application_default_model_button)
        application_form.addRow("Application temperature", self.application_temperature)
        application_form.addRow("Application max output", self.application_max_output_tokens)
        application_form.addRow("Advanced generation timeout", self.application_timeout)
        application_form.addRow("", self.save_application_defaults_button)
        general_layout.addLayout(application_form)
        general_layout.addWidget(SectionHeader(
            "Extended generation defaults",
            "Apply to every request unless the model or the chat overrides them and supports them.",
            general_page,
        ))
        self.application_settings_editor = GenerationSettingsEditor(general_page)
        self.application_settings_editor.setObjectName("applicationGenerationSettings")
        general_layout.addWidget(self.application_settings_editor, 1)
        general_layout.addStretch(0)

        self.status_label = QLabel("No connection selected", self)
        self.status_label.setObjectName("settingsStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.add_connection_button.clicked.connect(self.add_connection_requested)
        self.refresh_button.clicked.connect(self._refresh)
        self.save_refresh_button.clicked.connect(self._save_refresh)
        self.add_manual_model_button.clicked.connect(self._add_manual)
        self.save_credential_button.clicked.connect(self._save_credential)
        self.delete_credential_button.clicked.connect(self._delete_credential)
        self.save_model_defaults_button.clicked.connect(self._save_model_defaults)
        self.set_capability_override_button.clicked.connect(self._set_capability_override)
        self.save_application_defaults_button.clicked.connect(self._save_application_defaults)
        self.enable_connection_button.clicked.connect(self._toggle_enabled)
        self.retire_connection_button.clicked.connect(self._retire)
        self.save_application_default_model_button.clicked.connect(self._save_application_default_model)
        self._connection_values: dict[str, object] = {}
        self._credential_form_revision: int | None = None
        self._credential_form_reference: str | None = None
        self._model_default_revisions: dict[str, int | None] = {}
        self._model_extra_revisions: dict[str, int | None] = {}
        self._model_default_timeouts: dict[str, float | None] = {}
        self._capability_override_revisions: dict[tuple[str, str], int] = {}
        self._capability_override_values: dict[tuple[str, str], int | None] = {}
        self._capability_override_states: dict[tuple[str, str], str] = {}
        self._capability_override_reasons: dict[tuple[str, str], str | None] = {}
        self._application_revision: int | None = None
        self._application_extra_revision: int | None = None
        self._application_reasoning_effort: str | None = None
        self._application_extra_values: dict[str, object] = {}
        self._model_settings_editor_state: dict[str, object] = {}
        self._model_defaults_loaded_id: str | None = None
        for checkbox in (
            self.model_temperature_inherited,
            self.model_max_output_inherited,
            self.model_reasoning_inherited,
        ):
            checkbox.stateChanged.connect(self._sync_model_defaults_inherited)
        self.model_defaults_inherited.toggled.connect(self._set_all_model_defaults_inherited)
        self.capability_key.currentIndexChanged.connect(self._capability_key_changed)
        self.capability_state.currentIndexChanged.connect(self._sync_capability_override_editor)
        self._sync_capability_override_editor(reset_missing=True)
        fit_dialog_to_screen(self)

    def set_connections(self, connections: Iterable[object], credential_statuses: Mapping[str, str] | None = None) -> None:
        connections = tuple(connections)
        credential_statuses = credential_statuses or {}
        self._connection_values = {connection.id: connection for connection in connections}
        selected = self.connection_list.currentItem()
        selected_id = None if selected is None else selected.data(Qt.ItemDataRole.UserRole)
        self.connection_list.blockSignals(True)
        try:
            self.connection_list.clear()
            for connection in connections:
                status = "available" if connection.available else ("retired" if connection.retired else "disabled")
                credential = credential_statuses.get(connection.id, "unknown")
                refresh_state = getattr(connection, "catalogue_refresh_status", None)
                refresh_value = "unknown" if refresh_state is None else refresh_state.value
                refresh = refresh_value
                refresh_failure = getattr(connection, "catalogue_refresh_failure_class", None)
                if refresh_value == "failed" and refresh_failure is not None:
                    refresh += f" ({refresh_failure.value})"
                item = QListWidgetItem(
                    f"{connection.name} — {connection.profile.value} — {status} — "
                    f"credential {credential} — catalogue {refresh}"
                )
                item.setData(Qt.ItemDataRole.UserRole, connection.id)
                self.connection_list.addItem(item)
            for row in range(self.connection_list.count()):
                if self.connection_list.item(row).data(Qt.ItemDataRole.UserRole) == selected_id:
                    self.connection_list.setCurrentRow(row)
                    break
            if self.connection_list.currentRow() < 0 and self.connection_list.count():
                self.connection_list.setCurrentRow(0)
        finally:
            self.connection_list.blockSignals(False)

    def _connection_changed(self, current, previous) -> None:
        if current is not None:
            self.status_label.setText(current.text())
            self.set_connection_fields(
                self._connection_values.get(str(current.data(Qt.ItemDataRole.UserRole)))
            )

    def set_models(self, models: Iterable[object], connections: Mapping[str, object] | None = None) -> None:
        connections = connections or {}
        models = tuple(models)
        current = self.model_list.currentItem()
        selected_id = None if current is None else current.data(Qt.ItemDataRole.UserRole)
        self.model_list.blockSignals(True)
        try:
            self.model_list.clear()
            for model in models:
                connection = connections.get(model.connection_id)
                connection_name = model.connection_id if connection is None else connection.name
                item = QListWidgetItem(
                    f"{connection_name} / {model.provider_model_id} — {model.availability.value}"
                )
                item.setData(Qt.ItemDataRole.UserRole, model.id)
                self.model_list.addItem(item)
            for row in range(self.model_list.count()):
                if self.model_list.item(row).data(Qt.ItemDataRole.UserRole) == selected_id:
                    self.model_list.setCurrentRow(row)
                    break
            if self.model_list.currentRow() < 0 and self.model_list.count():
                self.model_list.setCurrentRow(0)
        finally:
            self.model_list.blockSignals(False)
        self._sync_capability_override_editor(reset_missing=True)
        replacement_id = self.retirement_replacement_model.currentData()
        current_connection = self.connection_list.currentItem()
        retiring_connection_id = (
            None
            if current_connection is None
            else str(current_connection.data(Qt.ItemDataRole.UserRole))
        )
        self.retirement_replacement_model.blockSignals(True)
        try:
            self.retirement_replacement_model.clear()
            self.retirement_replacement_model.addItem("Choose replacement model", None)
            for model in models:
                if model.availability.value != "available" or model.connection_id == retiring_connection_id:
                    continue
                connection = connections.get(model.connection_id)
                connection_name = model.connection_id if connection is None else connection.name
                self.retirement_replacement_model.addItem(
                    f"{connection_name} / {model.provider_model_id}", model.id
                )
            replacement_index = self.retirement_replacement_model.findData(replacement_id)
            self.retirement_replacement_model.setCurrentIndex(max(0, replacement_index))
        finally:
            self.retirement_replacement_model.blockSignals(False)
        default_id = self.application_default_model.currentData()
        self.application_default_model.blockSignals(True)
        try:
            self.application_default_model.clear()
            for model in models:
                connection = connections.get(model.connection_id)
                connection_name = model.connection_id if connection is None else connection.name
                self.application_default_model.addItem(
                    f"{connection_name} / {model.provider_model_id} — {model.availability.value}",
                    model.id,
                )
            default_index = self.application_default_model.findData(default_id)
            self.application_default_model.setCurrentIndex(default_index)
        finally:
            self.application_default_model.blockSignals(False)

    def set_model_defaults(
        self,
        settings: object | None,
        revision: int | None = None,
        *,
        extra_values: Mapping[str, object] | None = None,
        extra_revision: int | None = None,
        effective: Mapping[str, object] | None = None,
        effective_provenance: Mapping[str, str] | None = None,
        capabilities: Mapping[str, object] | None = None,
    ) -> None:
        current = self.model_list.currentItem()
        model_entry_id = None if current is None else str(current.data(Qt.ItemDataRole.UserRole))
        if model_entry_id is not None:
            self._model_default_revisions[model_entry_id] = 0 if revision is None else revision
            self._model_extra_revisions[model_entry_id] = extra_revision
        self._model_defaults_loaded_id = model_entry_id
        self._model_settings_editor_state = dict(extra_values or {})
        if settings is None:
            self._model_default_timeouts[model_entry_id] = None
            self.model_temperature.setValue(0.0)
            self.model_max_output_tokens.setValue(1024)
            self.model_reasoning.setCurrentIndex(0)
            self.model_temperature_inherited.setChecked(True)
            self.model_max_output_inherited.setChecked(True)
            self.model_reasoning_inherited.setChecked(True)
            self.model_defaults_inherited.blockSignals(True)
            try:
                self.model_defaults_inherited.setChecked(True)
            finally:
                self.model_defaults_inherited.blockSignals(False)
            self.model_settings_editor.set_state(
                values=extra_values or {},
                effective=effective or {},
                provenance=effective_provenance or {},
                capabilities=capabilities,
            )
            return
        self._model_default_timeouts[model_entry_id] = settings.timeout_seconds
        self.model_temperature.setValue(0.0 if settings.temperature is None else float(settings.temperature))
        self.model_max_output_tokens.setValue(1024 if settings.max_output_tokens is None else int(settings.max_output_tokens))
        self.model_reasoning.setCurrentIndex(max(0, self.model_reasoning.findData(settings.reasoning_effort)))
        self.model_temperature_inherited.setChecked(settings.temperature is None)
        self.model_max_output_inherited.setChecked(settings.max_output_tokens is None)
        self.model_reasoning_inherited.setChecked(settings.reasoning_effort is None)
        self.model_defaults_inherited.blockSignals(True)
        try:
            self.model_defaults_inherited.setChecked(
                self.model_temperature_inherited.isChecked()
                and self.model_max_output_inherited.isChecked()
                and self.model_reasoning_inherited.isChecked()
            )
        finally:
            self.model_defaults_inherited.blockSignals(False)
        self.model_settings_editor.set_state(
            values=extra_values or {},
            effective=effective if effective is not None else dict(getattr(settings, "extra", {}) or {}),
            provenance=effective_provenance or {},
            capabilities=capabilities,
        )

    def set_application_defaults(
        self,
        settings: object,
        revision: int | None = None,
        default_model_entry_id: str | None = None,
        *,
        extra_values: Mapping[str, object] | None = None,
        extra_revision: int | None = None,
    ) -> None:
        self._application_revision = revision
        self._application_extra_revision = extra_revision
        self._application_reasoning_effort = settings.reasoning_effort
        self._application_extra_values = dict(extra_values or {})
        self.application_temperature.setValue(float(settings.temperature or 0.0))
        self.application_max_output_tokens.setValue(int(settings.max_output_tokens or 1024))
        self.application_timeout.setValue(0.0 if settings.timeout_seconds is None else float(settings.timeout_seconds))
        self.application_default_model.setCurrentIndex(
            self.application_default_model.findData(default_model_entry_id)
        )
        self.application_settings_editor.set_state(
            values=extra_values or {},
            effective={
                "temperature": getattr(settings, "temperature", None),
                "max_output_tokens": getattr(settings, "max_output_tokens", None),
                "reasoning_effort": getattr(settings, "reasoning_effort", None),
                "timeout_seconds": getattr(settings, "timeout_seconds", None),
                **(dict(getattr(settings, "extra", {}) or {})),
            },
            provenance={key: "application" for key in (extra_values or {})},
            capabilities=None,
        )

    def set_capability_overrides(self, overrides: Iterable[object]) -> None:
        for override in overrides:
            identity = (override.model_entry_id, override.key)
            self._capability_override_revisions[identity] = override.revision
            self._capability_override_values[identity] = override.value
            self._capability_override_states[identity] = override.state.value
            self._capability_override_reasons[identity] = override.reason
        self._sync_capability_override_editor(reset_missing=True)

    def set_capabilities(self, capabilities: Iterable[object]) -> None:
        capabilities = tuple(capabilities)
        by_key = {capability.key: capability for capability in capabilities}
        relevant = (
            ("limits.context_tokens", "Context limit"),
            ("limits.output_tokens", "Output limit"),
        )
        limits = []
        for key, label in relevant:
            capability = by_key.get(key)
            if capability is None or capability.value is None:
                value = "unknown"
            else:
                value = str(capability.value)
            limits.append(f"{label}: {value} ({'unknown' if capability is None else capability.state.value})")
        self.capability_summary_label.setText("; ".join(limits))
        provenance = "; ".join(
            f"{capability.key}={capability.source.value}"
            for capability in capabilities
        )
        self.capability_provenance_label.setText(provenance or "none")

    def _model_changed(self, current, previous) -> None:
        if current is not None:
            self.status_label.setText(current.text())
            model_entry_id = str(current.data(Qt.ItemDataRole.UserRole))
            self._model_defaults_loaded_id = None
            self._sync_capability_override_editor(reset_missing=True)
            self.model_defaults_refresh_requested.emit(model_entry_id)

    def _set_all_model_defaults_inherited(self, inherited: bool) -> None:
        for checkbox in (
            self.model_temperature_inherited,
            self.model_max_output_inherited,
            self.model_reasoning_inherited,
        ):
            checkbox.setChecked(inherited)

    def _sync_model_defaults_inherited(self) -> None:
        inherited = all(
            checkbox.isChecked()
            for checkbox in (
                self.model_temperature_inherited,
                self.model_max_output_inherited,
                self.model_reasoning_inherited,
            )
        )
        self.model_defaults_inherited.blockSignals(True)
        try:
            self.model_defaults_inherited.setChecked(inherited)
        finally:
            self.model_defaults_inherited.blockSignals(False)

    def set_connection_fields(self, connection: object | None) -> None:
        if connection is None:
            return
        self._credential_form_revision = getattr(connection, "revision", None)
        self._credential_form_reference = getattr(connection, "credential_reference", None)
        self.connection_name.setText(connection.name)
        backend_index = self.connection_backend.findData(connection.backend_type.value)
        if backend_index >= 0:
            self.connection_backend.setCurrentIndex(backend_index)
        profile_index = self.connection_profile.findData(connection.profile.value)
        if profile_index >= 0:
            self.connection_profile.setCurrentIndex(profile_index)
        self.connection_endpoint.setText(connection.endpoint or "")
        source_index = self.connection_credential_source.findData(connection.credential_source.value)
        if source_index >= 0:
            self.connection_credential_source.setCurrentIndex(source_index)
        self.connection_credential_reference.setText(connection.credential_reference or "")
        self.connection_credential_value.clear()

    def _create(self) -> None:
        self.create_connection_requested.emit({
            "name": self.connection_name.text(),
            "backend_type": self.connection_backend.currentData(),
            "profile": self.connection_profile.currentData(),
            "endpoint": self.connection_endpoint.text() or None,
            "credential_source": self.connection_credential_source.currentData(),
            "credential_reference": self.connection_credential_reference.text() or None,
        })

    def _refresh(self) -> None:
        item = self.connection_list.currentItem()
        if item is not None:
            self.refresh_requested.emit(str(item.data(Qt.ItemDataRole.UserRole)))

    def _toggle_enabled(self) -> None:
        item = self.connection_list.currentItem()
        if item is None:
            return
        connection = self._connection_values.get(str(item.data(Qt.ItemDataRole.UserRole)))
        if connection is not None:
            self.connection_enabled_requested.emit({
                "connection_id": connection.id,
                "enabled": not connection.enabled,
                "revision": connection.revision,
            })

    def _retire(self) -> None:
        item = self.connection_list.currentItem()
        if item is not None:
            connection = self._connection_values.get(str(item.data(Qt.ItemDataRole.UserRole)))
            if connection is not None:
                self.connection_retire_requested.emit({
                    "connection_id": connection.id,
                    "revision": connection.revision,
                    "replacement_model_entry_id": self.retirement_replacement_model.currentData(),
                })

    def _save_refresh(self) -> None:
        item = self.connection_list.currentItem()
        if item is None:
            self._create()
            return
        connection = self._connection_values.get(str(item.data(Qt.ItemDataRole.UserRole)))
        if connection is None:
            return
        self.edit_connection_requested.emit({
            "connection_id": connection.id,
            "revision": connection.revision,
            "name": self.connection_name.text(),
            "backend_type": self.connection_backend.currentData(),
            "profile": self.connection_profile.currentData(),
            "endpoint": self.connection_endpoint.text() or None,
            "credential_source": self.connection_credential_source.currentData(),
            "credential_reference": self.connection_credential_reference.text() or None,
        })

    def _add_manual(self) -> None:
        item = self.connection_list.currentItem()
        if item is not None and self.manual_model_id.text().strip():
            self.add_manual_model_requested.emit({
                "connection_id": str(item.data(Qt.ItemDataRole.UserRole)),
                "provider_model_id": self.manual_model_id.text().strip(),
            })

    def _save_credential(self) -> None:
        item = self.connection_list.currentItem()
        value = self.connection_credential_value.text()
        if item is not None and value:
            self.connection_credential_value.clear()
            self.credential_save_requested.emit({
                "connection_id": str(item.data(Qt.ItemDataRole.UserRole)),
                "value": value,
                "expected_revision": self._credential_form_revision,
                "expected_credential_reference": self._credential_form_reference,
            })

    def _delete_credential(self) -> None:
        item = self.connection_list.currentItem()
        if item is not None:
            self.credential_delete_requested.emit({
                "connection_id": str(item.data(Qt.ItemDataRole.UserRole)),
                "expected_revision": self._credential_form_revision,
                "expected_credential_reference": self._credential_form_reference,
            })

    def _save_model_defaults(self) -> None:
        item = self.model_list.currentItem()
        if item is None:
            return
        model_entry_id = str(item.data(Qt.ItemDataRole.UserRole))
        if self._model_defaults_loaded_id != model_entry_id:
            self.status_label.setText("Model defaults are still loading; save again after refresh")
            return
        extra = self.model_settings_editor.override_payload()
        if self.model_settings_editor.has_invalid_values():
            self.status_label.setText("Extended settings contain invalid values; fix them before saving")
            return
        self.model_defaults_requested.emit({
            "model_entry_id": model_entry_id,
            "temperature": None if self.model_temperature_inherited.isChecked() else self.model_temperature.value(),
            "max_output_tokens": None if self.model_max_output_inherited.isChecked() else self.model_max_output_tokens.value(),
            "reasoning_effort": None if self.model_reasoning_inherited.isChecked() else self.model_reasoning.currentData(),
            "timeout_seconds": self._model_default_timeouts.get(model_entry_id),
            "extra": extra,
            "expected_revision": self._model_default_revisions.get(model_entry_id),
            "expected_extra_revision": self._model_extra_revisions.get(model_entry_id),
        })

    def _save_application_default_model(self) -> None:
        model_entry_id = self.application_default_model.currentData()
        if isinstance(model_entry_id, str) and model_entry_id:
            self.application_default_model_requested.emit({
                "model_entry_id": model_entry_id,
                "expected_revision": self._application_revision,
            })

    def _set_capability_override(self) -> None:
        item = self.model_list.currentItem()
        if item is None:
            return
        self.capability_override_requested.emit({
            "model_entry_id": str(item.data(Qt.ItemDataRole.UserRole)),
            "key": str(self.capability_key.currentData()),
            "state": str(self.capability_state.currentData()),
            "value": self.capability_value.value() if self.capability_value_set.isChecked() else None,
            "reason": self.capability_reason.text() or None,
            "expected_revision": self._capability_override_revisions.get(
                (str(item.data(Qt.ItemDataRole.UserRole)), str(self.capability_key.currentData())),
                0,
            ),
        })

    def _capability_key_changed(self, *_args) -> None:
        self._sync_capability_override_editor(reset_missing=True)

    def _sync_capability_override_editor(self, *_args, reset_missing: bool = False) -> None:
        item = self.model_list.currentItem()
        model_entry_id = None if item is None else str(item.data(Qt.ItemDataRole.UserRole))
        key = self.capability_key.currentData()
        identity = (model_entry_id, str(key)) if model_entry_id is not None else None
        if identity is not None and identity in self._capability_override_states:
            state_index = self.capability_state.findData(self._capability_override_states[identity])
            if state_index >= 0 and self.capability_state.currentIndex() != state_index:
                self.capability_state.blockSignals(True)
                try:
                    self.capability_state.setCurrentIndex(state_index)
                finally:
                    self.capability_state.blockSignals(False)
            self.capability_reason.setText(self._capability_override_reasons[identity] or "")
            value = self._capability_override_values[identity]
            self.capability_value_set.setChecked(value is not None)
            if value is not None:
                self.capability_value.setValue(value)
        elif reset_missing:
            unknown_index = self.capability_state.findData("unknown")
            self.capability_state.blockSignals(True)
            try:
                if unknown_index >= 0:
                    self.capability_state.setCurrentIndex(unknown_index)
                self.capability_reason.clear()
                self.capability_value_set.setChecked(False)
                self.capability_value.setValue(0)
            finally:
                self.capability_state.blockSignals(False)
        numeric_key = key in {
            "request.max_output_tokens",
            "limits.context_tokens",
            "limits.output_tokens",
        }
        supported = self.capability_state.currentData() == "supported"
        self.capability_value.setEnabled(numeric_key and supported)
        self.capability_value_set.setEnabled(numeric_key and supported)
        if not numeric_key or not supported:
            self.capability_value_set.setChecked(False)

    def _save_application_defaults(self) -> None:
        if self.application_settings_editor.has_invalid_values():
            self.status_label.setText("Extended defaults contain invalid values; fix them before saving")
            return
        timeout = self.application_timeout.value()
        self.application_defaults_requested.emit({
            "temperature": self.application_temperature.value(),
            "max_output_tokens": self.application_max_output_tokens.value(),
            "reasoning_effort": self._application_reasoning_effort,
            "timeout_seconds": None if timeout == 0.0 else timeout,
            "extra": self.application_settings_editor.override_payload(),
            "expected_revision": self._application_revision,
            "expected_extra_revision": self._application_extra_revision,
        })


class LeftRail(QFrame):
    new_chat_requested = Signal()
    chat_indicator_requested = Signal(str)
    # Additive Phase 9 chat-rail context actions (delegation only; the window
    # forwards these to Phase9DesktopController).
    export_transcript_requested = Signal(str)
    export_archive_requested = Signal(str)
    # Additive Phase 11 A-1: rename and duplicate.
    rename_chat_requested = Signal(str)
    duplicate_chat_requested = Signal(str)
    # Additive Phase 11 A-1c: sort selector.
    sort_changed = Signal(str)
    # Phase 11 M3 (F4/F5/F7): folders, pins and deletion.
    folder_filter_changed = Signal(object)  # folder id or None (All chats)
    pin_chat_requested = Signal(str, bool)
    move_chat_to_folder_requested = Signal(str, object)  # chat id, folder id or None
    open_move_dialog_requested = Signal(str)  # open the move-to-folder dialog
    delete_chat_requested = Signal(str)

    _FOLDER_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("leftRail")
        self._collapsed = False
        self._expanded_width = 220
        self._collapsed_width = 48
        self._folders: tuple[Folder, ...] = ()
        self._folder_filter: str | None = None

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

        # Additive Phase 11 A-1c: sort selector
        self.sort_combo = QComboBox(self)
        self.sort_combo.setObjectName("chatSort")
        self.sort_combo.addItem("Activity (default)", "recent")
        self.sort_combo.addItem("Creation", "creation")
        self.sort_combo.addItem("Title", "title")
        layout.addWidget(self.sort_combo)

        # Phase 11 M3 (F4): folder area — the rail groups chats by folder by
        # filtering the (store-ordered, pins-first) list to one folder.
        self.folder_combo = QComboBox(self)
        self.folder_combo.setObjectName("folderCombo")
        self.folder_combo.setAccessibleName("Folder filter")
        self.folder_filter_model = self.folder_combo.model()
        layout.addWidget(self.folder_combo)

        self.chat_list = QListWidget(self)
        self.chat_list.setObjectName("chatList")
        self.chat_list.setAccessibleName("Chats")
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self._show_chat_context_menu)
        layout.addWidget(self.chat_list, 1)

        self.activity_column = QVBoxLayout()
        self.activity_column.setContentsMargins(0, 2, 0, 0)
        self.activity_column.setSpacing(4)
        layout.addLayout(self.activity_column)
        self._chat_buttons: dict[str, QToolButton] = {}
        self._chat_titles: dict[str, str] = {}
        self._chat_pin_state: dict[str, bool] = {}
        self._chat_folder_by_id: dict[str, str | None] = {}

        # Connect sort selector
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        # Connect the Phase 11 M3 folder area (F4)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_filter_changed)

    def _on_sort_changed(self, index: int) -> None:
        """Emit the selected sort option."""
        sort_key = self.sort_combo.itemData(index)
        if isinstance(sort_key, str):
            self.sort_changed.emit(sort_key)

    def _on_folder_filter_changed(self, index: int) -> None:
        """Apply the F4 folder grouping selection to the chat list."""
        folder_id = self.folder_combo.itemData(index)
        self._folder_filter = folder_id if isinstance(folder_id, str) else None
        self._apply_folder_visibility()
        self.folder_filter_changed.emit(self._folder_filter)

    def set_folders(self, folders: Iterable[Folder]) -> None:
        """Populate the folder area with one entry per folder (F4)."""
        self._folders = tuple(folders)
        current = self._folder_filter
        self.folder_combo.blockSignals(True)
        try:
            self.folder_combo.clear()
            self.folder_combo.addItem("All chats", None)
            for folder in self._folders:
                self.folder_combo.addItem(folder.name, folder.id)
            if current is not None:
                index = self.folder_combo.findData(current)
                if index >= 0:
                    self.folder_combo.setCurrentIndex(index)
        finally:
            self.folder_combo.blockSignals(False)
        self._apply_folder_visibility()

    def _apply_folder_visibility(self) -> None:
        """Hide chat rows outside the selected folder, keeping row order.

        Rows are hidden rather than removed so the window's row-to-chat
        mapping (``_chat_ids``) stays exact while the rail still groups by
        folder (F4).
        """
        for row in range(self.chat_list.count()):
            item = self.chat_list.item(row)
            folder_id = item.data(self._FOLDER_ROLE)
            visible = (
                self._folder_filter is None
                or folder_id == self._folder_filter
            )
            item.setHidden(not visible)

    def _focus_chat_list(self) -> None:
        if self._collapsed:
            self.set_collapsed(False)
        self.chat_list.setFocus()

    def _show_chat_context_menu(self, pos) -> None:
        """Phase 9/11 context actions for one chat under the cursor.

        Pure selection + presentation: the chosen entry is emitted and the
        window delegates it to the application commands.  The menu is built
        by :meth:`_build_chat_menu` so tests can inspect the entries without
        running a blocking ``exec``.
        """

        item = self.chat_list.itemAt(pos)
        if item is None:
            return
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(chat_id, str) or not chat_id:
            return
        menu = self._build_chat_menu(chat_id, self.chat_list)
        chosen = menu.exec(self.chat_list.mapToGlobal(pos))
        self._dispatch_chat_menu_choice(chat_id, chosen)

    def _build_chat_menu(self, chat_id: str, parent: QWidget) -> QMenu:
        """Build the Phase 9/11 context menu for one chat (F4/F5/F7).

        The menu stays FLAT: the frozen Phase 9 surface contract pins the
        chat list to exactly one constructed QMenu, so "Move to folder…"
        opens a dialog instead of nesting a submenu.
        """
        menu = QMenu(parent)
        export_transcript_action = menu.addAction("Export Transcript…")
        export_archive_action = menu.addAction("Export Archive…")
        menu.addSeparator()
        rename_action = menu.addAction("Rename Title…")
        duplicate_action = menu.addAction("Duplicate Chat…")
        menu.addSeparator()
        # Phase 11 M3 (F5): the pin toggle reflects the chat's current state.
        is_pinned = self._chat_pin_state.get(chat_id, False)
        pin_action = menu.addAction("Unpin chat" if is_pinned else "Pin chat")
        pin_action.setObjectName("pinChatAction")
        # Phase 11 M3 (F4): the move dialog resolves folders and inline
        # folder creation through the application commands.
        move_action = menu.addAction("Move to folder…")
        move_action.setObjectName("moveToFolderAction")
        menu.addSeparator()
        delete_action = menu.addAction("Delete chat…")
        delete_action.setObjectName("deleteChatAction")
        self._pending_menu_actions = {
            "export_transcript": export_transcript_action,
            "export_archive": export_archive_action,
            "rename": rename_action,
            "duplicate": duplicate_action,
            "pin": pin_action,
            "move": move_action,
            "delete": delete_action,
        }
        return menu

    def _dispatch_chat_menu_choice(self, chat_id: str, chosen) -> None:
        """Emit the signal for one chosen context-menu entry."""
        if chosen is None:
            return
        pending = getattr(self, "_pending_menu_actions", {})
        if chosen is pending.get("export_transcript"):
            self.export_transcript_requested.emit(chat_id)
        elif chosen is pending.get("export_archive"):
            self.export_archive_requested.emit(chat_id)
        elif chosen is pending.get("rename"):
            self.rename_chat_requested.emit(chat_id)
        elif chosen is pending.get("duplicate"):
            self.duplicate_chat_requested.emit(chat_id)
        elif chosen is pending.get("pin"):
            self.pin_chat_requested.emit(
                chat_id, not self._chat_pin_state.get(chat_id, False)
            )
        elif chosen is pending.get("move"):
            self.open_move_dialog_requested.emit(chat_id)
        elif chosen is pending.get("delete"):
            self.delete_chat_requested.emit(chat_id)

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
        self._chat_titles = {
            chat.id: (
                f"{chat.title} [Archived]"
                if getattr(chat, "archived_at", None) is not None
                else chat.title
            )
            for chat in chats
        }
        # Phase 11 M3 (F4/F5): remember the organisation state so the context
        # menu can label the pin toggle and the folder filter can group.
        self._chat_pin_state = {
            chat.id: bool(getattr(chat, "is_pinned", False)) for chat in chats
        }
        self._chat_folder_by_id = {
            chat.id: getattr(chat, "folder_id", None) for chat in chats
        }
        for chat in chats:
            title = self._chat_titles[chat.id]
            button = self._icon_button("·", title)
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
                # Phase 11 M3 (F5): the floating pin is visible in the rail.
                display_title = self._chat_titles[chat.id]
                if self._chat_pin_state[chat.id]:
                    display_title = f"📌 {display_title}"
                item = QListWidgetItem(display_title)
                item.setData(Qt.ItemDataRole.UserRole, chat.id)
                item.setData(
                    Qt.ItemDataRole.AccessibleDescriptionRole,
                    "Archived chat" if getattr(chat, "archived_at", None) is not None else "Active chat",
                )
                item.setData(self._FOLDER_ROLE, self._chat_folder_by_id[chat.id])
                tooltip = display_title
                folder_id = self._chat_folder_by_id[chat.id]
                if folder_id is not None:
                    folder_name = next(
                        (f.name for f in self._folders if f.id == folder_id),
                        None,
                    )
                    if folder_name is not None:
                        tooltip = f"{display_title} — in folder: {folder_name}"
                item.setToolTip(tooltip)
                self.chat_list.addItem(item)
                if chat.id == selected_chat_id:
                    selected_row = row
            if selected_row >= 0:
                self.chat_list.setCurrentRow(selected_row)
            elif chats:
                self.chat_list.setCurrentRow(0)
        finally:
            self.chat_list.blockSignals(False)
        self._apply_folder_visibility()
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

    def __init__(
        self,
        message: Message,
        parent: QWidget | None = None,
        *,
        application: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.message = message
        self._generation_busy = False
        self._historical_view = False
        # Phase 11 M3 (F7): a tombstoned message keeps its lineage and
        # sequence but gives up its content, so the transcript renders an
        # explicit tombstone instead of a blank body.
        self._tombstone = message.state is MessageState.DELETED
        self.setProperty("messageRole", message.role.value)

        # Create markdown renderer with safe attachment resolver
        self._markdown_renderer: MarkdownRenderer | None = None
        if application is not None and not self._tombstone:
            # Try to get the authority from the application's store
            store = getattr(application, "_store", None)
            if store is not None:
                authority = getattr(store, "_authority", None)
                if authority is not None:
                    resolver = SafeAttachmentResolver(authority)
                    self._markdown_renderer = MarkdownRenderer(attachment_resolver=resolver)

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

        # Render markdown content (never for a tombstone: its body is gone)
        markdown_text = message.content
        if self._tombstone:
            html_content = self._escape_html_for_rich_text("This message was deleted")
        elif self._markdown_renderer is not None:
            html_content = self._markdown_renderer.render(markdown_text)
        else:
            # Fallback: escape HTML and wrap in paragraphs
            html_content = self._escape_html_for_rich_text(markdown_text)

        self.body = QLabel(html_content, self.bubble)
        self.body.setObjectName("messageBody" if not self._tombstone else "messageTombstone")
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        # Allow links to be opened via QDesktopServices when explicitly clicked
        self.body.setOpenExternalLinks(False)
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        bubble_layout.addWidget(self.body)

        self.historical_badge = QLabel("Historical result", self.bubble)
        self.historical_badge.setObjectName("historicalBadge")
        self.historical_badge.setVisible(False)
        bubble_layout.addWidget(self.historical_badge)

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

        if self._tombstone:
            # A tombstone is a record of removal: no message actions exist.
            self.copy_button.setVisible(False)
            self.edit_button.setVisible(False)
            self.branch_button.setVisible(False)
            self.more_button.setVisible(False)

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

    @staticmethod
    def _escape_html_for_rich_text(text: str) -> str:
        """Escape HTML special characters for safe rich text display."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )

    def set_generation_busy(self, busy: bool) -> None:
        self._generation_busy = busy
        self._sync_mutating_actions()
        self.set_generation_active(busy)

    def set_historical_view(self, historical: bool, *, exact_result: bool = False) -> None:
        self._historical_view = historical
        self.historical_badge.setVisible(historical and exact_result)
        self._sync_mutating_actions()

    def set_search_focus(self, focused: bool) -> None:
        self.bubble.setProperty("searchFocus", focused)
        self.bubble.style().unpolish(self.bubble)
        self.bubble.style().polish(self.bubble)

    def _sync_mutating_actions(self) -> None:
        if self._tombstone:
            self.edit_button.setEnabled(False)
            self.regenerate_action.setEnabled(False)
            return
        self.edit_button.setEnabled(
            not self._generation_busy
            and not self._historical_view
            and self.message.role is MessageRole.USER
            and self.message.state is MessageState.SENT
        )
        self.regenerate_action.setEnabled(
            not self._generation_busy
            and not self._historical_view
            and self.message.role is MessageRole.ASSISTANT
            and self.message.state is not MessageState.STREAMING
        )

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
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        application: object | None = None,
    ) -> None:
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
        self._application = application
        # Phase 11 M4b/M0a: restore-aware scrolling.  When a persisted
        # transcript scroll position is pending, renders scroll to that
        # position (clamped to the live range) instead of unconditionally
        # jumping to the bottom.  The pending value is consumed as soon as
        # the scroll range can actually represent it — including on a later
        # rangeChanged after the window becomes visible and lays out.
        self._pending_restore_position: int | None = None
        # True while the render in flight is the one carrying a pending
        # restore, so its own final layout pass must not yank the view to
        # the bottom after the restore was just applied.
        self._render_restoring = False
        self.verticalScrollBar().rangeChanged.connect(self._on_scroll_range_changed)

    def set_restore_scroll_position(self, position: int | None) -> None:
        """Queue a persisted transcript scroll position for restoration."""
        position = None if position is None else max(0, int(position))
        self._pending_restore_position = position

    @property
    def pending_restore_position(self) -> int | None:
        """The restore position not yet applied to a live scroll range."""
        return self._pending_restore_position

    @property
    def scroll_position(self) -> int:
        """The position worth persisting: pending restore, else current value.

        While a restore is still pending (the range cannot represent it yet),
        the intended position is reported so a save must never clobber the
        persisted value with the pre-layout ``0``.
        """
        if self._pending_restore_position is not None:
            return self._pending_restore_position
        return int(self.verticalScrollBar().value())

    def _apply_pending_restore(self) -> None:
        """Apply the pending restore position, consuming it only once the
        scroll range can fully represent it.  Until then every render and
        range change keeps the view parked at the closest reachable offset
        so a slow layout never silently degrades the restore to the bottom."""
        if self._pending_restore_position is None:
            return
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(min(self._pending_restore_position, scrollbar.maximum()))
        if scrollbar.maximum() >= self._pending_restore_position:
            self._pending_restore_position = None

    def _on_scroll_range_changed(self, _minimum: int, _maximum: int) -> None:
        self._apply_pending_restore()

    def render(
        self,
        messages: Iterable[Message],
        generation_busy: bool = False,
        *,
        historical_leaf_message_id: str | None = None,
    ) -> None:
        for row in tuple(self.message_rows.values()):
            self._layout.removeWidget(row)
            row.deleteLater()
        self.message_rows.clear()
        messages = tuple(messages)
        self.empty_label.setVisible(not messages)
        for message in messages:
            row = MessageRow(message, self.content, application=self._application)
            row.set_generation_busy(generation_busy)
            message_id = getattr(message, "id", f"message-{id(message)}")
            row.set_historical_view(
                historical_leaf_message_id is not None,
                exact_result=message_id == historical_leaf_message_id,
            )
            self.message_rows[message_id] = row
            self._layout.insertWidget(self._layout.count() - 1, row)
        self._resize_bubbles()
        # Phase 11 M0a/M4b: restore-aware scrolling.  A pending persisted
        # position wins over the old unconditional jump to the bottom; with
        # nothing to restore, chat-style bottom-following behaviour is kept.
        # A render that consumed a restore must not immediately undo it.
        self._render_restoring = self._render_restoring or (
            self._pending_restore_position is not None
        )
        if self._pending_restore_position is not None:
            self._apply_pending_restore()
        elif not self._render_restoring:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self._render_restoring = False

    def focus_message(self, message_id: str | None) -> bool:
        for row in self.message_rows.values():
            row.set_search_focus(False)
        if message_id is None:
            return True
        row = self.message_rows.get(message_id)
        if row is None:
            return False
        row.set_search_focus(True)
        row.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row.setFocus(Qt.FocusReason.OtherFocusReason)
        self.ensureWidgetVisible(row, 18, 18)
        return True

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
        self._field("Selection", "No chat selected")

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

    def show_projection(self, projection: InspectionProjection) -> None:
        """Present a core-owned projection; no persisted-schema interpretation here."""
        self._clear()
        self._field("Inspection", projection.status)
        for field in projection.fields:
            self._field(field.name, field.value)


class MoveToFolderDialog(QDialog):
    """Phase 11 M3 (F4): move one chat into exactly one folder.

    Flat folders only: the dialog offers the existing folders plus an inline
    new-folder field.  Choosing OK resolves to ``(folder_id_or_None,
    new_folder_name_or_None)``; the WINDOW performs the create-then-move
    through the application commands, so this dialog never touches state.
    """

    def __init__(
        self,
        chat_title: str,
        folders: Iterable[Folder],
        current_folder_id: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move to folder")
        self.setObjectName("moveToFolderDialog")
        self._folder_ids: tuple[str, ...] = tuple(folder.id for folder in folders)

        layout = QVBoxLayout(self)
        heading = QLabel(f"Move “{chat_title}” to:", self)
        heading.setObjectName("moveToFolderHeading")
        layout.addWidget(heading)

        self.folder_combo = QComboBox(self)
        self.folder_combo.setObjectName("moveToFolderCombo")
        self.folder_combo.addItem("No folder", None)
        for folder in folders:
            self.folder_combo.addItem(folder.name, folder.id)
        if current_folder_id is not None:
            index = self.folder_combo.findData(current_folder_id)
            if index >= 0:
                self.folder_combo.setCurrentIndex(index)
        layout.addWidget(self.folder_combo)

        self.new_folder_edit = QLineEdit(self)
        self.new_folder_edit.setObjectName("newFolderEdit")
        self.new_folder_edit.setPlaceholderText("…or create a new folder")
        self.new_folder_edit.setClearButtonEnabled(True)
        layout.addWidget(self.new_folder_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.setObjectName("moveToFolderButtons")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def outcome(self) -> tuple[str | None, str | None]:
        """Resolve the dialog to (folder_id, new_folder_name)."""
        new_name = self.new_folder_edit.text().strip()
        if new_name:
            return (None, new_name)
        folder_id = self.folder_combo.currentData()
        return (folder_id if isinstance(folder_id, str) else None, None)


class DeleteChatConfirmationDialog(QDialog):
    """Phase 11 M3 (F7): the deliberate whole-chat deletion confirmation.

    Presents the LOSS INVENTORY computed by the store
    (``describe_chat_deletion``) so the operator sees exactly what is lost
    before the destructive command is admitted.  The dialog itself never
    deletes; it only resolves to accepted/rejected.
    """

    def __init__(
        self,
        inventory: ChatDeletionInventory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.inventory = inventory
        self.setWindowTitle("Delete chat?")
        self.setObjectName("deleteChatConfirmationDialog")

        layout = QVBoxLayout(self)
        heading = QLabel(
            f"Delete “{inventory.title}” permanently?", self
        )
        heading.setObjectName("deleteChatHeading")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        lines = [
            f"Chats deleted: 1",
            f"Messages deleted: {inventory.message_count}",
            f"Attachments detached: {inventory.attachment_count}",
            f"Generation attempts deleted: {inventory.generation_attempt_count}",
        ]
        self.inventory_label = QLabel("\n".join(lines), self)
        self.inventory_label.setObjectName("deleteChatInventory")
        layout.addWidget(self.inventory_label)

        warning = QLabel(
            "This cannot be undone. Attachment files are kept for cleanup "
            "and may still be used by other chats.",
            self,
        )
        warning.setObjectName("deleteChatWarning")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.setObjectName("deleteChatButtons")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Delete chat")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "deleteChatConfirmButton"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
