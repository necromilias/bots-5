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
from bots5.domain.provider import (
    CredentialRequirement,
    CredentialSource,
    builtin_connection_definitions,
    connection_definition,
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

        self.model_selector = QComboBox(self)
        self.model_selector.setObjectName("modelSelector")
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
        model_entry_id = self.model_selector.itemData(index)
        if isinstance(model_entry_id, str) and model_entry_id:
            self.model_selected.emit(model_entry_id)

    def set_models(self, entries: Iterable[tuple[str, str]], selected_model_entry_id: str | None) -> None:
        self.model_selector.blockSignals(True)
        try:
            self.model_selector.clear()
            selected_index = -1
            for index, (label, model_entry_id) in enumerate(entries):
                self.model_selector.addItem(label, model_entry_id)
                if model_entry_id == selected_model_entry_id:
                    selected_index = index
            # A selection-required chat must remain visibly unselected. Qt
            # otherwise auto-selects index 0 while signals are blocked, which
            # leaves the user unable to select the only available entry.
            self.model_selector.setCurrentIndex(selected_index)
        finally:
            self.model_selector.blockSignals(False)


class TuneDialog(QDialog):
    save_requested = Signal(object)
    inherit_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tune")
        self.setObjectName("tuneDialog")
        layout = QVBoxLayout(self)
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
        self._timeout_override: float | None = None
        form.addRow("Temperature", self.temperature)
        form.addRow("", self.temperature_inherited)
        form.addRow("Max output tokens", self.max_output_tokens)
        form.addRow("", self.max_output_inherited)
        form.addRow("Reasoning", self.reasoning_none)
        form.addRow("", self.reasoning_inherited)
        form.addRow("Resolved from", self.provenance_label)
        layout.addLayout(form)
        self.temperature_inherited.toggled.connect(self.temperature.setDisabled)
        self.max_output_inherited.toggled.connect(self.max_output_tokens.setDisabled)
        self.reasoning_inherited.toggled.connect(self.reasoning_none.setDisabled)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        self.use_inherited_button = buttons.addButton("Use inherited", QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.use_inherited_button.clicked.connect(self._use_inherited)
        layout.addWidget(buttons)

    def set_settings(
        self,
        settings: Mapping[str, object],
        provenance: Mapping[str, str],
        *,
        timeout_override: float | None = None,
        override_revision: int | None = None,
        model_entry_id: str | None = None,
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
        self.provenance_label.setText(", ".join(f"{key}: {value}" for key, value in provenance.items()) or "defaults")
        self.temperature_inherited.setChecked(provenance.get("temperature") != "chat_model")
        self.max_output_inherited.setChecked(provenance.get("max_output_tokens") != "chat_model")
        self.reasoning_inherited.setChecked(provenance.get("reasoning_effort") != "chat_model")

    def _save(self) -> None:
        self.save_requested.emit({
            "temperature": None if self.temperature_inherited.isChecked() else self.temperature.value(),
            "max_output_tokens": None if self.max_output_inherited.isChecked() else self.max_output_tokens.value(),
            "reasoning_effort": None if self.reasoning_inherited.isChecked() else ("none" if self.reasoning_none.isChecked() else None),
            "timeout_seconds": self._timeout_override,
            "expected_revision": self._override_revision,
            "expected_model_entry_id": self._model_entry_id,
        })
        self.accept()

    def _use_inherited(self) -> None:
        self.inherit_requested.emit({
            "temperature": None,
            "max_output_tokens": None,
            "reasoning_effort": None,
            "timeout_seconds": None,
            "expected_revision": self._override_revision,
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Provider and model settings")
        self.setObjectName("settingsDialog")
        self.resize(680, 520)
        layout = QVBoxLayout(self)
        self.connection_list = QListWidget(self)
        self.connection_list.setObjectName("connectionList")
        self.connection_list.currentItemChanged.connect(self._connection_changed)
        layout.addWidget(self.connection_list, 1)
        connection_actions = QHBoxLayout()
        self.add_connection_button = QPushButton("+ Add Connection", self)
        self.add_connection_button.setObjectName("addConnectionButton")
        self.add_connection_button.setToolTip("Add a supported provider connection")
        connection_actions.addWidget(self.add_connection_button)
        connection_actions.addStretch(1)
        layout.addLayout(connection_actions)
        self.model_list = QListWidget(self)
        self.model_list.setObjectName("modelCatalogueList")
        self.model_list.setMaximumHeight(130)
        self.model_list.currentItemChanged.connect(self._model_changed)
        layout.addWidget(self.model_list)
        form = QFormLayout()
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
        form.addRow("Name", self.connection_name)
        form.addRow("Backend", self.connection_backend)
        form.addRow("Profile", self.connection_profile)
        form.addRow("Endpoint", self.connection_endpoint)
        form.addRow("Credential source", self.connection_credential_source)
        form.addRow("Credential reference", self.connection_credential_reference)
        form.addRow("Credential value", self.connection_credential_value)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.save_refresh_button = QPushButton("Save & Refresh", self)
        self.refresh_button = QPushButton("Refresh", self)
        self.enable_connection_button = QPushButton("Enable/disable", self)
        self.retire_connection_button = QPushButton("Retire", self)
        self.retirement_replacement_model = QComboBox(self)
        self.retirement_replacement_model.setObjectName("retirementReplacementModel")
        self.retirement_replacement_model.addItem("Choose replacement model", None)
        actions.addWidget(self.save_refresh_button)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.enable_connection_button)
        actions.addWidget(self.retire_connection_button)
        layout.addLayout(actions)
        layout.addWidget(QLabel("Retirement replacement", self))
        layout.addWidget(self.retirement_replacement_model)
        manual = QHBoxLayout()
        self.manual_model_id = QLineEdit(self)
        self.manual_model_id.setObjectName("manualModelId")
        self.manual_model_id.setPlaceholderText("Exact provider model ID")
        self.add_manual_model_button = QPushButton("Add manual model", self)
        self.save_credential_button = QPushButton("Save credential", self)
        self.delete_credential_button = QPushButton("Delete credential", self)
        manual.addWidget(self.manual_model_id, 1)
        manual.addWidget(self.add_manual_model_button)
        manual.addWidget(self.save_credential_button)
        manual.addWidget(self.delete_credential_button)
        layout.addLayout(manual)
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
        layout.addLayout(model_form)
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
        layout.addLayout(capability_form)
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
        layout.addLayout(application_form)
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
        self._model_default_timeouts: dict[str, float | None] = {}
        self._capability_override_revisions: dict[tuple[str, str], int] = {}
        self._capability_override_values: dict[tuple[str, str], int | None] = {}
        self._capability_override_states: dict[tuple[str, str], str] = {}
        self._capability_override_reasons: dict[tuple[str, str], str | None] = {}
        self._application_revision: int | None = None
        self._application_reasoning_effort: str | None = None
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

    def set_model_defaults(self, settings: object | None, revision: int | None = None) -> None:
        current = self.model_list.currentItem()
        model_entry_id = None if current is None else str(current.data(Qt.ItemDataRole.UserRole))
        if model_entry_id is not None:
            self._model_default_revisions[model_entry_id] = 0 if revision is None else revision
        self._model_defaults_loaded_id = model_entry_id
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

    def set_application_defaults(
        self,
        settings: object,
        revision: int | None = None,
        default_model_entry_id: str | None = None,
    ) -> None:
        self._application_revision = revision
        self._application_reasoning_effort = settings.reasoning_effort
        self.application_temperature.setValue(float(settings.temperature or 0.0))
        self.application_max_output_tokens.setValue(int(settings.max_output_tokens or 1024))
        self.application_timeout.setValue(0.0 if settings.timeout_seconds is None else float(settings.timeout_seconds))
        self.application_default_model.setCurrentIndex(
            self.application_default_model.findData(default_model_entry_id)
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
        self.model_defaults_requested.emit({
            "model_entry_id": model_entry_id,
            "temperature": None if self.model_temperature_inherited.isChecked() else self.model_temperature.value(),
            "max_output_tokens": None if self.model_max_output_inherited.isChecked() else self.model_max_output_tokens.value(),
            "reasoning_effort": None if self.model_reasoning_inherited.isChecked() else self.model_reasoning.currentData(),
            "timeout_seconds": self._model_default_timeouts.get(model_entry_id),
            "expected_revision": self._model_default_revisions.get(model_entry_id),
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
        timeout = self.application_timeout.value()
        self.application_defaults_requested.emit({
            "temperature": self.application_temperature.value(),
            "max_output_tokens": self.application_max_output_tokens.value(),
            "reasoning_effort": self._application_reasoning_effort,
            "timeout_seconds": None if timeout == 0.0 else timeout,
            "expected_revision": self._application_revision,
        })


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
            phase5 = self._phase5_snapshot(attempt.request_snapshot)
            if phase5 is not None:
                self._field(f"{prefix} connection", f"{phase5.get('connection_name', 'unknown')} ({phase5.get('connection_id', 'unknown')})")
                self._field(f"{prefix} model entry", phase5.get("model_entry_id", "unknown"))
                self._field(f"{prefix} effective settings", json.dumps(phase5.get("effective_settings", {}), sort_keys=True))
                self._field(f"{prefix} capability provenance", json.dumps(phase5.get("capability_provenance", {}), sort_keys=True))
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
            for key in ("backend_id", "provider_id", "connection_id", "model", "endpoint", "base_url", "prompt")
            if key in parsed
        ]
        return "present; fields=" + ", ".join(fields)

    @staticmethod
    def _phase5_snapshot(snapshot: str) -> dict[str, object] | None:
        try:
            parsed = json.loads(snapshot)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict) or parsed.get("snapshot_version") != 2:
            return None
        return parsed

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
