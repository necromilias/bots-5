"""Phase 9 dialogs: transcript export, archive export, archive import admission,
imported-continuation resolution, backup creation, backup verification, restore
handoff.

Every dialog is selection + presentation only (SLICE_E_DESIGN.md §2 ownership
rule): it captures operator intent, emits it to
:class:`~bots5.desktop.phase9.Phase9DesktopController`, and presents the landed
typed outcome it is given.  No dialog performs a filesystem probe — export and
admission truth come from the landed application commands (I4, F-03), so a slow
or missing source can never freeze the qasync loop from here.

``.botsarchive`` (chat interchange) and ``.botsbackup`` (restore) are different
containers; each picker filters only its own extension (ARCH §15.1(3)).

Backup dialogs (workflows 6 and 7): the creation dialog owns the
``threading.Event`` whose ``is_set`` bound method is the landed cancellation
callable, and presents the landed outcome truthfully — success (backup id,
size, destination), typed failure, clean cancellation, and
``BackupUncertainPublication`` as an explicitly UNCERTAIN outcome that is
never success and never a clean cancellation.  The verification dialog is
deliberately separate from creation and presents the verification receipt
truthfully.  Progress widgets are only ever mutated from GUI-thread slots fed
by the queued :class:`~bots5.desktop.phase9.Phase9ProgressBridge` signal; the
worker thread never touches a widget.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from bots5.core.export import AttachmentPolicy, TranscriptScope


_MD_FILTER = "Markdown transcript (*.md)"
_ARCHIVE_FILTER = "B.O.T.S. chat archive (*.botsarchive)"
_BACKUP_FILTER = "B.O.T.S. full backup (*.botsbackup)"

# The landed BackupProgressState values (core/backup.py), shown with their raw
# hyphenated value kept available truthfully.
_BACKUP_PROGRESS_LABELS = {
    "acquiring-fence": "Acquiring the recovery fence",
    "holding-recovery-point-fence": "Capturing under the recovery-point fence",
    "finalizing": "Finalizing the staged package",
    "verifying": "Independently verifying the staged package",
    "publishing": "Publishing the backup",
    "completed": "Backup completed",
}


class _Phase9Dialog(QDialog):
    """Shared truthful outcome surface: typed errors are shown, never collapsed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._error_label = QLabel("", self)
        self._error_label.setObjectName("phase9DialogError")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)

    def _add_error_label(self, layout: QVBoxLayout) -> None:
        layout.addWidget(self._error_label)

    def show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(True)

    def clear_error(self) -> None:
        self._error_label.setText("")
        self._error_label.setVisible(False)

    def submission_failed(self, message: str) -> None:
        """A landed typed refusal: surfaced verbatim, the dialog stays open."""

        self.show_error(message)

    def submission_succeeded(self, _result: object = None) -> None:
        self.accept()


class TranscriptExportDialog(_Phase9Dialog):
    """Workflow 1 selection surface: scope radios + destination picker (.md)."""

    confirm_requested = Signal(str, object)  # (destination, TranscriptScope)

    def __init__(self, parent: QWidget | None = None, *, chat_title: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Transcript")
        self.setObjectName("transcriptExportDialog")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        if chat_title:
            heading = QLabel(f"Export the transcript of \u201c{chat_title}\u201d.", self)
            heading.setWordWrap(True)
            layout.addWidget(heading)

        self.scope_active_path = QRadioButton("Active path only", self)
        self.scope_active_path.setObjectName("transcriptScopeActivePath")
        self.scope_active_path.setChecked(True)
        self.scope_full_lineage = QRadioButton("Full lineage", self)
        self.scope_full_lineage.setObjectName("transcriptScopeFullLineage")
        layout.addWidget(self.scope_active_path)
        layout.addWidget(self.scope_full_lineage)

        destination_row = QHBoxLayout()
        self.destination_edit = QLineEdit(self)
        self.destination_edit.setObjectName("transcriptExportDestination")
        self.destination_edit.setPlaceholderText("Choose a .md destination")
        destination_row.addWidget(self.destination_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setObjectName("transcriptExportBrowse")
        self.browse_button.clicked.connect(self.pick_destination)
        destination_row.addWidget(self.browse_button)
        layout.addLayout(destination_row)
        self._add_error_label(layout)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.export_button = QPushButton("Export", self)
        self.export_button.setObjectName("transcriptExportButton")
        self.export_button.clicked.connect(self.confirm)
        actions.addWidget(self.export_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("transcriptExportCancelButton")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    def scope(self) -> TranscriptScope:
        if self.scope_full_lineage.isChecked():
            return TranscriptScope.FULL_LINEAGE
        return TranscriptScope.ACTIVE_PATH

    def destination(self) -> str:
        return self.destination_edit.text().strip()

    def pick_destination(self) -> None:
        """Native picker seam; tests patch QFileDialog.getSaveFileName."""

        path, _filter = QFileDialog.getSaveFileName(
            self, "Export transcript", self.destination(), _MD_FILTER
        )
        if path:
            self.destination_edit.setText(path)
            self.clear_error()

    def confirm(self) -> None:
        destination = self.destination()
        if not destination:
            self.show_error("Choose a destination for the transcript export.")
            return
        self.confirm_requested.emit(destination, self.scope())


class ArchiveExportDialog(_Phase9Dialog):
    """Workflow 2 selection surface: destination, attachment policy, version display.

    The dialog only selects inputs the landed API already accepts; the landed
    version-selection rules stay in core.  An ``ArchiveVersionRequired(2)``
    refusal is answered with an explicit "switch to v2?" operator prompt —
    never an automatic retry.
    """

    confirm_requested = Signal(str, object, object)  # (destination, AttachmentPolicy, int|None)

    _AUTOMATIC_VERSION = None

    def __init__(self, parent: QWidget | None = None, *, chat_title: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Archive")
        self.setObjectName("archiveExportDialog")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        if chat_title:
            heading = QLabel(f"Export the archive of \u201c{chat_title}\u201d.", self)
            heading.setWordWrap(True)
            layout.addWidget(heading)

        self.attachment_policy = QComboBox(self)
        self.attachment_policy.setObjectName("archiveExportAttachmentPolicy")
        self.attachment_policy.addItem("Embedded payloads", AttachmentPolicy.EMBEDDED.value)
        self.attachment_policy.addItem(
            "External references", AttachmentPolicy.EXTERNAL_REFERENCE.value
        )
        layout.addWidget(QLabel("Attachment policy", self))
        layout.addWidget(self.attachment_policy)

        self.archive_version = QComboBox(self)
        self.archive_version.setObjectName("archiveExportVersion")
        self.archive_version.addItem("Automatic (landed losslessness rules)", None)
        self.archive_version.addItem("Archive v1 (wholly native graphs)", 1)
        self.archive_version.addItem("Archive v2 (full fidelity, import provenance)", 2)
        layout.addWidget(QLabel("Archive version", self))
        layout.addWidget(self.archive_version)

        destination_row = QHBoxLayout()
        self.destination_edit = QLineEdit(self)
        self.destination_edit.setObjectName("archiveExportDestination")
        self.destination_edit.setPlaceholderText("Choose a .botsarchive destination")
        destination_row.addWidget(self.destination_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setObjectName("archiveExportBrowse")
        self.browse_button.clicked.connect(self.pick_destination)
        destination_row.addWidget(self.browse_button)
        layout.addLayout(destination_row)
        self._add_error_label(layout)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.export_button = QPushButton("Export", self)
        self.export_button.setObjectName("archiveExportButton")
        self.export_button.clicked.connect(self.confirm)
        actions.addWidget(self.export_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("archiveExportCancelButton")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    def policy(self) -> AttachmentPolicy:
        return AttachmentPolicy(self.attachment_policy.currentData())

    def version(self) -> int | None:
        data = self.archive_version.currentData()
        return None if data is None else int(data)

    def destination(self) -> str:
        return self.destination_edit.text().strip()

    def pick_destination(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export archive", self.destination(), _ARCHIVE_FILTER
        )
        if path:
            self.destination_edit.setText(path)
            self.clear_error()

    def confirm_switch_to_v2(self) -> bool:
        """Explicit operator consent for the landed v2 requirement (workflow 2)."""

        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self,
            "Archive v2 required",
            "A lossless export of this chat requires Archive v2 because it "
            "carries import provenance.\nSwitch to v2?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        index = self.archive_version.findData(2)
        if index >= 0:
            self.archive_version.setCurrentIndex(index)
        return True

    def confirm(self) -> None:
        destination = self.destination()
        if not destination:
            self.show_error("Choose a destination for the archive export.")
            return
        self.confirm_requested.emit(destination, self.policy(), self.version())


class ArchiveImportAdmissionDialog(_Phase9Dialog):
    """Workflow 3 admission surface: one-shot source, optional resolver roots.

    The source and the resolver roots are one-shot intake arguments only; they
    are never chat provenance and never become watched roots.  There is
    deliberately no dialog-side ``exists/readable/regular`` probe: admission
    truth is the landed store result, surfaced through the queue row (F-03).
    """

    admit_requested = Signal(str, tuple, bool)  # (source_path, resolver_roots, import_as_archived)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Archive")
        self.setObjectName("archiveImportAdmissionDialog")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Import one chat archive into this workspace.  The source and the "
            "resolver roots are used once for this import only.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        source_row = QHBoxLayout()
        self.source_edit = QLineEdit(self)
        self.source_edit.setObjectName("archiveImportSource")
        self.source_edit.setPlaceholderText("Choose a .botsarchive source")
        source_row.addWidget(self.source_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setObjectName("archiveImportBrowse")
        self.browse_button.clicked.connect(self.pick_source)
        source_row.addWidget(self.browse_button)
        layout.addLayout(source_row)

        layout.addWidget(QLabel("Resolver roots (optional, one-shot)", self))
        self.roots_list = QListWidget(self)
        self.roots_list.setObjectName("archiveImportResolverRoots")
        layout.addWidget(self.roots_list)
        roots_actions = QHBoxLayout()
        self.add_root_button = QPushButton("Add root…", self)
        self.add_root_button.setObjectName("archiveImportAddRoot")
        self.add_root_button.clicked.connect(self.add_resolver_root)
        roots_actions.addWidget(self.add_root_button)
        self.remove_root_button = QPushButton("Remove selected", self)
        self.remove_root_button.setObjectName("archiveImportRemoveRoot")
        self.remove_root_button.clicked.connect(self.remove_selected_resolver_root)
        roots_actions.addWidget(self.remove_root_button)
        roots_actions.addStretch(1)
        layout.addLayout(roots_actions)

        self.import_as_archived = QCheckBox("Import as archived", self)
        self.import_as_archived.setObjectName("archiveImportAsArchived")
        self.import_as_archived.setToolTip(
            "Admit the imported chat in its archived state instead of the active rail."
        )
        layout.addWidget(self.import_as_archived)
        self._add_error_label(layout)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.import_button = QPushButton("Import", self)
        self.import_button.setObjectName("archiveImportButton")
        self.import_button.clicked.connect(self.confirm)
        actions.addWidget(self.import_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("archiveImportCancelButton")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    def source_path(self) -> str:
        return self.source_edit.text().strip()

    def resolver_roots(self) -> tuple[str, ...]:
        return tuple(
            self.roots_list.item(row).text()
            for row in range(self.roots_list.count())
        )

    def pick_source(self) -> None:
        """Native picker seam; tests patch QFileDialog.getOpenFileName."""

        path, _filter = QFileDialog.getOpenFileName(
            self, "Import archive", self.source_path(), _ARCHIVE_FILTER
        )
        if path:
            self.source_edit.setText(path)
            self.clear_error()

    def add_resolver_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Add resolver root", "", QFileDialog.Option.ShowDirsOnly
        )
        if not directory:
            return
        existing = self.resolver_roots()
        if directory in existing:
            return
        self.roots_list.addItem(directory)

    def remove_selected_resolver_root(self) -> None:
        for item in self.roots_list.selectedItems():
            self.roots_list.takeItem(self.roots_list.row(item))

    def confirm(self) -> None:
        source = self.source_path()
        if not source:
            self.show_error("Choose a .botsarchive source to import.")
            return
        self.admit_requested.emit(
            source, self.resolver_roots(), self.import_as_archived.isChecked()
        )


# ---------------------------------------------------------------------------
# Workflow 5 — imported-continuation resolution (send/regenerate intercept)
# ---------------------------------------------------------------------------


def _descriptor_field(descriptor: object, key: str) -> str | None:
    """One portable descriptor field, honoring its recorded availability status.

    The import sealed each descriptor value together with a ``<key>_status``
    marker; a value that was not recorded as available is presented as absent
    rather than guessed.
    """

    if not isinstance(descriptor, dict):
        return None
    if descriptor.get(f"{key}_status") != "available":
        return None
    value = descriptor.get(key)
    return value if isinstance(value, str) and value else None


def _format_source_configuration(source_configuration: object) -> tuple[str, str, str]:
    """Read-only historical truth: (model line, provider line, sampling line).

    The values come only from the immutable import-recorded
    ``source_configuration``; nothing here is editable or inferred.
    """

    configuration = source_configuration if isinstance(source_configuration, dict) else {}
    selection = configuration.get("selection")
    model = selection.get("model") if isinstance(selection, dict) else None
    overrides = configuration.get("overrides")
    overrides = [item for item in overrides if isinstance(item, dict)] if isinstance(overrides, list) else []

    model_entry = _descriptor_field(model, "source_model_entry_id")
    display_name = _descriptor_field(model, "display_name")
    provider_model = _descriptor_field(model, "provider_model_id")
    if model is None:
        model_line = "Source model: not recorded"
    else:
        parts = [
            display_name or "unnamed",
            provider_model or "model id not recorded",
        ]
        if model_entry is not None:
            parts.append(f"model entry {model_entry}")
        model_line = "Source model: " + " · ".join(parts)

    backend = _descriptor_field(model, "backend_type")
    profile = _descriptor_field(model, "provider_profile")
    connection = _descriptor_field(model, "source_connection_id")
    if backend is None and profile is None and connection is None:
        provider_line = "Source provider: not recorded"
    else:
        provider_line = "Source provider: " + " · ".join(
            part for part in (
                backend or "backend not recorded",
                profile or "profile not recorded",
                None if connection is None else f"connection {connection}",
            ) if part is not None
        )

    source_entry = model_entry
    override = None
    if source_entry is not None:
        for item in overrides:
            candidate = item.get("model")
            if isinstance(candidate, dict) and _descriptor_field(
                candidate, "source_model_entry_id"
            ) == source_entry:
                override = item
                break
    elif len(overrides) == 1:
        override = overrides[0]
    if override is None:
        sampling_line = "Original sampling parameters: not recorded"
    else:
        sampling_line = "Original sampling parameters: " + ", ".join(
            f"{key}={override[key]}"
            for key in (
                "temperature", "max_output_tokens",
                "reasoning_effort", "timeout_seconds",
            )
            if override.get(key) is not None
        ) or "Original sampling parameters: not recorded (all parameters inherited)"
    return model_line, provider_line, sampling_line


def _missing_external_requirements(readiness: object) -> tuple:
    """The blocked MISSING_EXTERNAL requirements, in recorded ordinal order."""

    requirements = getattr(readiness, "requirements", ()) or ()
    return tuple(
        requirement for requirement in requirements
        if requirement.blocked_reason == "missing-external"
    )


class ContinuationResolutionDialog(_Phase9Dialog):
    """Workflow 5 selection surface (IMPORT_QUEUE_AND_CONTINUATION_UX.md §3.2).

    Read-only historical truth (source model/provider and the original
    sampling parameters) is presented above an explicit local choice of one
    currently-active provider and one of its available models, plus explicit
    generation settings.  ``MISSING_EXTERNAL`` requirements are listed
    truthfully and require an explicit acknowledgement before Continue is
    enabled.  Nothing is guessed: the dialog only emits
    :attr:`commit_requested`; the controller owns the landed
    ``choose_import_continuation`` command and the replay.
    """

    commit_requested = Signal(str, str, object, tuple)  # connection_id, model_entry_id, settings, excluded_refs

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        readiness: object,
        connections: tuple = (),
        models: tuple = (),
        credential_statuses: dict[str, str] | None = None,
        resolved_settings: object = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resolve Imported Continuation")
        self.setObjectName("continuationResolutionDialog")
        self.setMinimumWidth(520)
        self._readiness = readiness
        self._connections = tuple(connections)
        self._models = tuple(models)
        self._credential_statuses = dict(credential_statuses or {})
        self._missing = _missing_external_requirements(readiness)

        layout = QVBoxLayout(self)
        heading = QLabel(
            "This chat continues imported history that needs an explicit "
            "local choice before it can be continued.  Choose a currently "
            "active provider and model; the imported history itself stays "
            "unchanged.",
            self,
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        # Read-only historical truth — immutable, never editable here.
        self.source_truth_label = QLabel("\n".join(_format_source_configuration(
            getattr(readiness, "source_configuration", None)
        )), self)
        self.source_truth_label.setObjectName("continuationSourceTruth")
        self.source_truth_label.setWordWrap(True)
        layout.addWidget(self.source_truth_label)
        self.resolution_truth_label = QLabel(
            f"Recorded resolution: {getattr(readiness, 'resolution', 'UNKNOWN')} · "
            f"choice revision: {getattr(readiness, 'choice_revision', 0)}",
            self,
        )
        self.resolution_truth_label.setObjectName("continuationResolutionTruth")
        self.resolution_truth_label.setWordWrap(True)
        layout.addWidget(self.resolution_truth_label)

        layout.addWidget(QLabel("Active provider", self))
        self.provider_combo = QComboBox(self)
        self.provider_combo.setObjectName("continuationProviderCombo")
        self.provider_combo.addItem("Choose an active provider…", None)
        for connection in self._connections:
            credential = self._credential_statuses.get(
                getattr(connection, "id", ""), "unknown"
            )
            self.provider_combo.addItem(
                f"{connection.name} — {connection.backend_type.value}/"
                f"{connection.profile.value} (credential: {credential})",
                connection.id,
            )
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        layout.addWidget(self.provider_combo)

        layout.addWidget(QLabel("Model", self))
        self.model_combo = QComboBox(self)
        self.model_combo.setObjectName("continuationModelCombo")
        self._reset_model_combo()
        self.model_combo.currentIndexChanged.connect(lambda _index: self._sync_controls())
        layout.addWidget(self.model_combo)

        settings_form = QFormLayout()
        settings_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.temperature_edit = QLineEdit(self)
        self.temperature_edit.setObjectName("continuationTemperature")
        self.temperature_edit.setPlaceholderText("not set")
        settings_form.addRow("Temperature", self.temperature_edit)
        self.max_output_edit = QLineEdit(self)
        self.max_output_edit.setObjectName("continuationMaxOutputTokens")
        self.max_output_edit.setPlaceholderText("not set")
        settings_form.addRow("Max output tokens", self.max_output_edit)
        self.reasoning_combo = QComboBox(self)
        self.reasoning_combo.setObjectName("continuationReasoningEffort")
        self.reasoning_combo.addItem("not set", None)
        self.reasoning_combo.addItem("none", "none")
        settings_form.addRow("Reasoning effort", self.reasoning_combo)
        self.timeout_edit = QLineEdit(self)
        self.timeout_edit.setObjectName("continuationTimeoutSeconds")
        self.timeout_edit.setPlaceholderText("not set")
        settings_form.addRow("Timeout seconds", self.timeout_edit)
        settings_row = QWidget(self)
        settings_row.setLayout(settings_form)
        layout.addWidget(QLabel(
            "Explicit generation settings (a blank field means “not set”)",
            self,
        ))
        layout.addWidget(settings_row)
        self._set_resolved_settings(resolved_settings)

        if self._missing:
            self.missing_label = QLabel(
                "Missing external attachments — the following imported files "
                "are not present in this workspace:\n"
                + "\n".join(self._missing_line(requirement) for requirement in self._missing)
                + "\n\nThey cannot be restored from this dialog, and no provider "
                "or file is fetched automatically.  Continuing excludes them "
                "from the model context; the model will not see their content.",
                self,
            )
            self.missing_label.setObjectName("continuationMissingExternal")
            self.missing_label.setWordWrap(True)
            layout.addWidget(self.missing_label)
            self.missing_acknowledgement = QCheckBox(
                "Acknowledge the missing external attachments and continue without them",
                self,
            )
            self.missing_acknowledgement.setObjectName("continuationMissingAcknowledgement")
            self.missing_acknowledgement.toggled.connect(lambda _checked: self._sync_controls())
            layout.addWidget(self.missing_acknowledgement)

        self._add_error_label(layout)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.continue_button = QPushButton("Continue", self)
        self.continue_button.setObjectName("continuationContinueButton")
        self.continue_button.clicked.connect(self.confirm)
        actions.addWidget(self.continue_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("continuationCancelButton")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self._sync_controls()

    # ------------------------------------------------------------------
    # Presentation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _missing_line(requirement) -> str:
        digest = str(requirement.expected_digest)
        short_digest = digest if len(digest) <= 16 else digest[:16] + "…"
        return (
            f"• attachment slot {requirement.ordinal}: external file not "
            f"present (recorded identity: digest {short_digest}, "
            f"{requirement.expected_size} bytes)"
        )

    def _set_resolved_settings(self, resolved_settings: object) -> None:
        """Prefill the explicit settings from the chat's own resolved choice.

        The values are presented and remain editable; the commit sends exactly
        what the fields contain.  No value is invented here.
        """

        if resolved_settings is None:
            return
        def text(value: object) -> str:
            return "" if value is None else str(value)
        self.temperature_edit.setText(text(getattr(resolved_settings, "temperature", None)))
        self.max_output_edit.setText(text(getattr(resolved_settings, "max_output_tokens", None)))
        effort = getattr(resolved_settings, "reasoning_effort", None)
        index = self.reasoning_combo.findData(effort)
        self.reasoning_combo.setCurrentIndex(index if index >= 0 else 0)
        self.timeout_edit.setText(text(getattr(resolved_settings, "timeout_seconds", None)))

    def _reset_model_combo(self) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("Choose a model…", None)
        self.model_combo.blockSignals(False)

    @staticmethod
    def _is_available(model) -> bool:
        """Only currently-available catalogue entries are selectable."""

        availability = getattr(model, "availability", None)
        if availability is None:
            return False
        value = getattr(availability, "value", availability)
        return str(value).lower() == "available"

    def _provider_changed(self, _index: int) -> None:
        connection_id = self.provider_combo.currentData()
        self._reset_model_combo()
        if connection_id is not None:
            for model in self._models:
                if getattr(model, "connection_id", None) != connection_id:
                    continue
                if not self._is_available(model):
                    continue
                self.model_combo.addItem(
                    f"{model.display_name} ({model.provider_model_id})",
                    model.id,
                )
        self._sync_controls()

    def _sync_controls(self) -> None:
        enabled = (
            self.provider_combo.currentData() is not None
            and self.model_combo.currentData() is not None
            and (
                not self._missing
                or self.missing_acknowledgement.isChecked()
            )
        )
        self.continue_button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Operator intent
    # ------------------------------------------------------------------

    def selected_connection_id(self) -> str | None:
        return self.provider_combo.currentData()

    def selected_model_entry_id(self) -> str | None:
        return self.model_combo.currentData()

    def _explicit_settings(self) -> dict[str, object] | None:
        """The four landed setting keys, exactly as the operator left them."""

        try:
            raw_temperature = self.temperature_edit.text().strip()
            raw_max_output = self.max_output_edit.text().strip()
            raw_timeout = self.timeout_edit.text().strip()
            temperature = None if not raw_temperature else float(raw_temperature)
            max_output_tokens = None if not raw_max_output else int(raw_max_output)
            timeout_seconds = None if not raw_timeout else float(raw_timeout)
        except (TypeError, ValueError):
            self.show_error(
                "Generation settings are malformed: temperature and timeout "
                "must be numbers and max output tokens must be a whole number."
            )
            return None
        if temperature is not None and not 0 <= temperature <= 2:
            self.show_error("Temperature must be between 0 and 2.")
            return None
        if max_output_tokens is not None and max_output_tokens < 1:
            self.show_error("Max output tokens must be at least 1.")
            return None
        if timeout_seconds is not None and timeout_seconds <= 0:
            self.show_error("Timeout seconds must be greater than 0.")
            return None
        return {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "reasoning_effort": self.reasoning_combo.currentData(),
            "timeout_seconds": timeout_seconds,
        }

    def _excluded_refs(self) -> tuple[dict[str, object], ...]:
        """The acknowledged MISSING_EXTERNAL omissions, in ordinal order.

        A requirement with a bound imported reference excludes that recorded
        candidate identity; a digest-bound requirement excludes by identity
        evidence alone.  Only blocked requirements are ever excluded here.
        """

        excluded = []
        for requirement in self._missing:
            excluded.append({
                "requirement_ordinal": int(requirement.ordinal),
                "expected_digest": str(requirement.expected_digest),
                "attachment_id": requirement.imported_ref_id,
                "reason": "missing-external",
            })
        return tuple(excluded)

    def confirm(self) -> None:
        connection_id = self.selected_connection_id()
        model_entry_id = self.selected_model_entry_id()
        if connection_id is None or model_entry_id is None:
            self.show_error(
                "Choose an active provider and one of its available models "
                "for the continuation.  Nothing is chosen automatically."
            )
            return
        if self._missing and not self.missing_acknowledgement.isChecked():
            self.show_error(
                "Acknowledge the missing external attachments before "
                "continuing; they are never silently dropped."
            )
            return
        settings = self._explicit_settings()
        if settings is None:
            return
        self.clear_error()
        self.commit_requested.emit(
            connection_id, model_entry_id, settings, self._excluded_refs()
        )


# ---------------------------------------------------------------------------
# Workflow 6 — Backup v1 creation (Tools → "Create Full Backup…")
# ---------------------------------------------------------------------------


class BackupCreationDialog(_Phase9Dialog):
    """Workflow 6 selection + progress surface.

    Selection: a ``*.botsbackup`` destination and an explicit overwrite
    checkbox.  If the destination exists and overwrite is not ticked, an
    explicit overwrite confirmation is required before proceeding — never a
    silent clobber.

    Progress: the worker thread calls the controller's
    :class:`~bots5.desktop.phase9.Phase9ProgressBridge`, whose queued Qt
    signal delivers to :meth:`progress_update` on the GUI thread; only that
    GUI-thread slot touches widgets.  Cancel owns the ``threading.Event``
    whose ``is_set`` bound method is the landed cancellation callable polled
    by the worker — pressing Cancel requests cancellation, it never kills a
    thread.  The landed outcome is reported truthfully: success (backup id,
    size, destination), typed failure verbatim, clean cancellation, and
    ``BackupUncertainPublication`` as an explicitly UNCERTAIN outcome that is
    never success and never a clean cancellation.
    """

    confirm_requested = Signal(str, bool)  # (destination, overwrite)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Full Backup")
        self.setObjectName("backupCreationDialog")
        self.setMinimumWidth(480)

        # Landed cancellation shape (workflow 6): a threading.Event whose
        # .is_set bound method is handed to create_backup as `cancellation`.
        self.cancel_event = threading.Event()
        self.outcome: str | None = None
        self.result: object = None
        self._running = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Create one full Backup v1 package (*.botsbackup) of this "
            "workspace.  The package is written atomically and verified "
            "before publication.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        destination_row = QHBoxLayout()
        self.destination_edit = QLineEdit(self)
        self.destination_edit.setObjectName("backupCreationDestination")
        self.destination_edit.setPlaceholderText("Choose a .botsbackup destination")
        destination_row.addWidget(self.destination_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setObjectName("backupCreationBrowse")
        self.browse_button.clicked.connect(self.pick_destination)
        destination_row.addWidget(self.browse_button)
        layout.addLayout(destination_row)

        self.overwrite_checkbox = QCheckBox("Overwrite existing package", self)
        self.overwrite_checkbox.setObjectName("backupCreationOverwrite")
        self.overwrite_checkbox.setToolTip(
            "Allow the backup to replace an existing package at the destination"
        )
        layout.addWidget(self.overwrite_checkbox)
        self._add_error_label(layout)

        self.progress_label = QLabel("", self)
        self.progress_label.setObjectName("backupCreationProgress")
        self.progress_label.setWordWrap(True)
        self.progress_label.setVisible(False)
        layout.addWidget(self.progress_label)

        self.summary_label = QLabel("", self)
        self.summary_label.setObjectName("backupCreationSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setVisible(False)
        layout.addWidget(self.summary_label)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.create_button = QPushButton("Create Backup", self)
        self.create_button.setObjectName("backupCreationButton")
        self.create_button.clicked.connect(self.confirm)
        actions.addWidget(self.create_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("backupCreationCancelButton")
        self.cancel_button.clicked.connect(self.cancel)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def destination(self) -> str:
        return self.destination_edit.text().strip()

    def pick_destination(self) -> None:
        """Native picker seam; tests patch QFileDialog.getSaveFileName."""

        path, _filter = QFileDialog.getSaveFileName(
            self, "Create full backup", self.destination(), _BACKUP_FILTER
        )
        if path:
            self.destination_edit.setText(path)
            self.clear_error()

    def confirm(self) -> None:
        destination = self.destination()
        if not destination:
            self.show_error("Choose a .botsbackup destination for the backup.")
            return
        overwrite = self.overwrite_checkbox.isChecked()
        if not overwrite and Path(destination).exists():
            answer = QMessageBox.question(
                self,
                "Overwrite existing backup?",
                f"{destination}\nalready exists.  Creating the backup with "
                "overwrite will replace the existing package.\n"
                "Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.show_error(
                    "Kept the existing package.  Tick “Overwrite existing "
                    "package” or choose a different destination."
                )
                return
            overwrite = True
        self.clear_error()
        self.confirm_requested.emit(destination, overwrite)

    # ------------------------------------------------------------------
    # Progress and truthful outcomes (GUI-thread slots only)
    # ------------------------------------------------------------------

    def begin_run(self) -> None:
        self.cancel_event.clear()
        self._running = True
        self.outcome = None
        self.result = None
        self.create_button.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.overwrite_checkbox.setEnabled(False)
        self.clear_error()
        self.summary_label.setVisible(False)
        self.progress_label.setText("Preparing backup…")
        self.progress_label.setVisible(True)

    def progress_update(self, state: object, _operation_id: object) -> None:
        """Queued-connection slot: runs on the GUI thread, never on a worker."""

        value = str(getattr(state, "value", state))
        friendly = _BACKUP_PROGRESS_LABELS.get(value, value)
        self.progress_label.setText(f"{friendly} ({value})")

    def creation_succeeded(self, result: object) -> None:
        self._finish_run()
        self.outcome = "success"
        self.result = result
        receipt = getattr(result, "receipt", None)
        size = getattr(receipt, "artifact_size", None)
        self.summary_label.setText("\n".join([
            "Backup created.",
            f"Backup id: {getattr(result, 'backup_id', '')}",
            f"Size: {size if size is not None else 'unknown'} bytes",
            f"Destination: {getattr(result, 'destination', '')}",
        ]))
        self.summary_label.setVisible(True)
        self.progress_label.setText(
            f"{_BACKUP_PROGRESS_LABELS['completed']} (completed)"
        )
        self.accept()

    def publication_uncertain(self, message: str) -> None:
        """Landed ``BackupUncertainPublication``: UNCERTAIN — never success,
        never a clean cancellation, never silently swallowed."""

        self._finish_run()
        self.outcome = "uncertain"
        self.show_error(
            "Backup publication outcome is UNCERTAIN — the package may or "
            "may not have been published.  The owned staging path is "
            "preserved for inspection; treat the result as undetermined.\n"
            f"{message}"
        )
        self.progress_label.setText("Publication outcome uncertain (uncertain)")

    def report_cancelled(self, message: str) -> None:
        self._finish_run()
        self.outcome = "cancelled"
        self.show_error(f"Backup was cancelled before publication: {message}")
        self.progress_label.setText("Cancelled (cancelled)")

    def submission_failed(self, message: str) -> None:
        """A landed typed refusal: surfaced verbatim, the dialog stays open."""

        self._finish_run()
        self.outcome = "failed"
        super().submission_failed(message)

    def cancel(self) -> None:
        if self._running:
            # Landed cancellation: the worker polls this event; setting it
            # requests cancellation, it never kills the thread.
            self.cancel_event.set()
            self.progress_label.setText(
                "Cancellation requested — waiting for the backup worker…"
            )
        else:
            self.reject()

    def _finish_run(self) -> None:
        self._running = False
        self.create_button.setEnabled(True)
        self.browse_button.setEnabled(True)
        self.overwrite_checkbox.setEnabled(True)


# ---------------------------------------------------------------------------
# Workflow 7 — independent Backup v1 verification (Tools →
# "Verify Backup Package…"), deliberately separate from creation
# ---------------------------------------------------------------------------


class BackupVerificationDialog(_Phase9Dialog):
    """Workflow 7 selection + result surface.

    Selection: a ``*.botsbackup`` package and an optional expected backup id.
    Verification is a bounded single-shot operation, so there is no cancel
    affordance during the run (ASYNC_IO_AND_CANCELLATION.md §4).  On success
    the landed verification receipt is shown truthfully; typed refusals
    (``BackupArchiveInvalid`` / ``BackupUnsupported`` /
    ``BackupResourceLimit`` / expected-id mismatch) are shown verbatim and
    the dialog stays open.
    """

    verify_requested = Signal(str, object)  # (package_path, expected_backup_id | None)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Verify Backup Package")
        self.setObjectName("backupVerificationDialog")
        self.setMinimumWidth(520)

        self.outcome: str | None = None
        self.result: object = None
        self._running = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Independently verify one *.botsbackup package against its own "
            "manifest.  This never touches the live workspace.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        package_row = QHBoxLayout()
        self.package_edit = QLineEdit(self)
        self.package_edit.setObjectName("backupVerificationPackage")
        self.package_edit.setPlaceholderText("Choose a .botsbackup package")
        package_row.addWidget(self.package_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setObjectName("backupVerificationBrowse")
        self.browse_button.clicked.connect(self.pick_package)
        package_row.addWidget(self.browse_button)
        layout.addLayout(package_row)

        self.expected_edit = QLineEdit(self)
        self.expected_edit.setObjectName("backupVerificationExpectedId")
        self.expected_edit.setPlaceholderText(
            "Optional expected backup id (leave blank to skip the identity check)"
        )
        layout.addWidget(self.expected_edit)
        self._add_error_label(layout)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("backupVerificationStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        self.summary_label = QLabel("", self)
        self.summary_label.setObjectName("backupVerificationSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setVisible(False)
        layout.addWidget(self.summary_label)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.verify_button = QPushButton("Verify", self)
        self.verify_button.setObjectName("backupVerificationButton")
        self.verify_button.clicked.connect(self.confirm)
        actions.addWidget(self.verify_button)
        self.cancel_button = QPushButton("Close", self)
        self.cancel_button.setObjectName("backupVerificationCancelButton")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def package_path(self) -> str:
        return self.package_edit.text().strip()

    def expected_backup_id(self) -> str | None:
        expected = self.expected_edit.text().strip()
        return expected or None

    def pick_package(self) -> None:
        """Native picker seam; tests patch QFileDialog.getOpenFileName."""

        path, _filter = QFileDialog.getOpenFileName(
            self, "Verify backup package", self.package_path(), _BACKUP_FILTER
        )
        if path:
            self.package_edit.setText(path)
            self.clear_error()

    def confirm(self) -> None:
        package = self.package_path()
        if not package:
            self.show_error("Choose a .botsbackup package to verify.")
            return
        self.clear_error()
        self.verify_requested.emit(package, self.expected_backup_id())

    # ------------------------------------------------------------------
    # Truthful outcomes (GUI-thread slots only)
    # ------------------------------------------------------------------

    def begin_run(self) -> None:
        self._running = True
        self.outcome = None
        self.result = None
        self.verify_button.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.expected_edit.setEnabled(False)
        self.clear_error()
        self.summary_label.setVisible(False)
        self.status_label.setText("Verifying the package…")
        self.status_label.setVisible(True)

    def verification_succeeded(self, result: object) -> None:
        self._finish_run()
        self.outcome = "success"
        self.result = result
        receipt = getattr(result, "receipt", None)
        lines = ["Backup package verified."]
        if receipt is not None:
            lines.extend([
                f"Backup id: {receipt.backup_id}",
                f"Verified at: {receipt.verified_at}",
                f"Artifact size: {receipt.artifact_size} bytes",
                f"Artifact SHA-256: {receipt.artifact_sha256}",
                f"Checksum match: {receipt.outcome}",
                f"Logical content digest: {receipt.backup_logical_content_digest}",
                f"Source migration revision: {receipt.source_db_migration_revision}",
                "Passed checks: " + ", ".join(receipt.passed_checks),
            ])
            if receipt.failed_check_ids:
                lines.append(
                    "Failed checks: " + ", ".join(receipt.failed_check_ids)
                )
            if receipt.reason_code:
                lines.append(f"Reason: {receipt.reason_code}")
        self.summary_label.setText("\n".join(lines))
        self.summary_label.setVisible(True)
        self.status_label.setVisible(False)
        self.accept()

    def submission_failed(self, message: str) -> None:
        """A landed typed refusal: surfaced verbatim, the dialog stays open."""

        self._finish_run()
        self.outcome = "failed"
        super().submission_failed(message)

    def _finish_run(self) -> None:
        self._running = False
        self.verify_button.setEnabled(True)
        self.browse_button.setEnabled(True)
        self.expected_edit.setEnabled(True)


# ---------------------------------------------------------------------------
# Workflow 8 — whole-installation restore handoff (Tools → "Restore From
# Backup…").  The dialog is selection + consequence presentation only: the
# coordinator owns the sealed chain, and the restore itself runs in the
# pre-store bootstrap child after the application has fully closed.  No
# destructive override is offered, mentioned as available, or inferred
# (invariant I3), and there is no retained-installation cleanup surface.
# ---------------------------------------------------------------------------


#: The S3 modal consequence warning, with the verbatim substance of the five
#: sealed bullets (RESTORE_UI_HANDOFF.md §2 step S3).  It offers no override
#: and no cleanup action of any kind.
_RESTORE_CONSEQUENCE_TEXT = (
    "Whole-installation restore replaces the entire database, settings and "
    "chat history with the package contents.\n\n"
    "• The current installation is preserved as a pre-restore installation "
    "and is not deleted.\n"
    "• The application will close cleanly and release all locks.\n"
    "• If an import is past its journal cutoff, closing may wait for it to "
    "settle.\n"
    "• Restore runs before the desktop can be used again: the operator must "
    "relaunch B.O.T.S. afterwards."
)

_PROCEED_BUTTON_TEXT = "Proceed with Restore and Restart"
_CANCEL_BUTTON_TEXT = "Cancel"


def _restore_result_presentation(
    status: int, receipt_text: str, refusal_text: str
) -> tuple[str, str]:
    """Truthful S8 headline + guidance for the exact 0/1/2/3 child status.

    Slice D semantics are preserved verbatim and never collapsed
    (RESTORE_UI_HANDOFF.md §2 S8): commit finalisation happens on a later
    normal startup, a rollback requires a restart, and a refusal/fail-closed
    leaves the data root preserved.
    """

    if status == 0:
        return (
            "Restore committed (exit status 0).",
            "Restore committed; relaunch B.O.T.S. The receipt is finalised by "
            "the later normal startup.  The raw canonical receipt is shown "
            "below, unaltered.",
        )
    if status == 2:
        if "restore rolled back" in refusal_text:
            return (
                "Restore not committed — rolled back (exit status 2).",
                "The installation was converged back to the preserved "
                "pre-restore state: restart required before startup can "
                "continue.  The typed outcome is shown below, unaltered.",
            )
        return (
            "Restore not committed (exit status 2).",
            "The restore was refused or rolled back; the data root is "
            "preserved and nothing was adopted.  The typed refusal is shown "
            "below, unaltered.",
        )
    if status == 3:
        return (
            "Restore failed closed (exit status 3).",
            "Unattributable evidence: the data root is preserved and human "
            "inspection is required.  The typed failure is shown below, "
            "unaltered.",
        )
    if status == 1:
        return (
            "Restore exited with status 1.",
            "The restore may have committed but an error escaped (fail "
            "loud).  The raw output is shown below, unaltered.",
        )
    return (
        f"Restore exited with status {status}.",
        "The raw child output is shown below, unaltered.",
    )


class RestoreHandoffDialog(_Phase9Dialog):
    """Workflow 8 selection surface (S1) + explicit consequence modal (S3).

    Selection: a ``*.botsbackup`` package and an optional expected backup id.
    Confirming hands the package to the coordinator for the live independent
    verification (S2); a refusal is shown verbatim and changes nothing.

    On a verified package the dialog presents the sealed S3 modal consequence
    warning (five bullets, above) with exactly [Cancel] /
    [Proceed with Restore and Restart].  Proceeding closes this dialog BEFORE
    the one-shot request is registered; the dialog is never a member of
    ``DesktopRuntime.windows``, so the subsequent orderly close neither
    closes nor needs it (oracle R-3).  Restore never runs from this dialog
    or from this live session.
    """

    confirm_requested = Signal(str, object)  # (package_path, expected_backup_id | None)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Restore From Backup")
        self.setObjectName("restoreHandoffDialog")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Restore this whole installation from one Backup v1 package "
            "(*.botsbackup).  The package is independently verified first; "
            "a refused verification changes nothing.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        package_row = QHBoxLayout()
        self.package_edit = QLineEdit(self)
        self.package_edit.setObjectName("restoreHandoffPackage")
        self.package_edit.setPlaceholderText("Choose a .botsbackup package")
        package_row.addWidget(self.package_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setObjectName("restoreHandoffBrowse")
        self.browse_button.clicked.connect(self.pick_package)
        package_row.addWidget(self.browse_button)
        layout.addLayout(package_row)

        self.expected_edit = QLineEdit(self)
        self.expected_edit.setObjectName("restoreHandoffExpectedId")
        self.expected_edit.setPlaceholderText(
            "Optional expected backup id (leave blank to accept the "
            "package's own verified identity)"
        )
        layout.addWidget(self.expected_edit)
        self._add_error_label(layout)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.continue_button = QPushButton("Continue", self)
        self.continue_button.setObjectName("restoreHandoffContinueButton")
        self.continue_button.clicked.connect(self.confirm)
        actions.addWidget(self.continue_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("restoreHandoffCancelButton")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def package_path(self) -> str:
        return self.package_edit.text().strip()

    def expected_backup_id(self) -> str | None:
        expected = self.expected_edit.text().strip()
        return expected or None

    def pick_package(self) -> None:
        """Native picker seam; tests patch QFileDialog.getOpenFileName."""

        path, _filter = QFileDialog.getOpenFileName(
            self, "Restore from backup", self.package_path(), _BACKUP_FILTER
        )
        if path:
            self.package_edit.setText(path)
            self.clear_error()

    def confirm(self) -> None:
        package = self.package_path()
        if not package:
            self.show_error("Choose a .botsbackup package to restore from.")
            return
        self.clear_error()
        self.confirm_requested.emit(package, self.expected_backup_id())

    # ------------------------------------------------------------------
    # S3 — explicit modal consequence (verbatim sealed substance)
    # ------------------------------------------------------------------

    def confirm_consequence(self) -> bool:
        """Show the S3 modal; True only when the operator proceeds.

        The modal is presented with the exact sealed buttons [Cancel] /
        [Proceed with Restore and Restart]; no destructive override is
        offered, mentioned as available, or inferred.
        """

        box = self._build_consequence_message_box()
        box.exec()
        return box.result() == QDialog.DialogCode.Accepted

    def _build_consequence_message_box(self) -> QMessageBox:
        box = QMessageBox(self)
        box.setWindowTitle("Restore From Backup")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(_RESTORE_CONSEQUENCE_TEXT)
        proceed = box.addButton(
            _PROCEED_BUTTON_TEXT, QMessageBox.ButtonRole.AcceptRole
        )
        cancel = box.addButton(
            _CANCEL_BUTTON_TEXT, QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(cancel)
        return box


class RestoreHandoffResultDialog(_Phase9Dialog):
    """S8: the GUI-visible post-close restore result (F-06).

    Created by the runtime's post-close step after every window is closed,
    while the qasync loop is still alive.  It shows the raw canonical
    receipt (stdout) or the typed refusal (stderr) together with the EXACT
    0/1/2/3 exit status — the outcome is never collapsed into a generic
    success/failure and is never inferred from an inherited terminal — plus
    the truthful relaunch instruction (commit finalises on a later normal
    startup; a rollback requires a restart; a refusal preserves the data
    root).
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        data_root,
        argv,
        status: int,
        receipt_text: str = "",
        refusal_text: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Restore Result")
        self.setObjectName("restoreHandoffResultDialog")
        self.setMinimumWidth(560)
        self.status_code = int(status)
        self.raw_receipt_text = receipt_text
        self.raw_refusal_text = refusal_text
        self.argv = tuple(argv)

        headline, guidance = _restore_result_presentation(
            self.status_code, receipt_text, refusal_text
        )

        layout = QVBoxLayout(self)
        self.headline_label = QLabel(headline, self)
        self.headline_label.setObjectName("restoreHandoffResultHeadline")
        self.headline_label.setWordWrap(True)
        layout.addWidget(self.headline_label)
        self.guidance_label = QLabel(guidance, self)
        self.guidance_label.setObjectName("restoreHandoffResultGuidance")
        self.guidance_label.setWordWrap(True)
        layout.addWidget(self.guidance_label)
        self.status_label = QLabel(f"Exit status: {self.status_code}", self)
        self.status_label.setObjectName("restoreHandoffResultStatus")
        layout.addWidget(self.status_label)
        self.data_root_label = QLabel(f"Data root: {data_root}", self)
        self.data_root_label.setObjectName("restoreHandoffResultDataRoot")
        self.data_root_label.setWordWrap(True)
        layout.addWidget(self.data_root_label)

        self.detail_view = QPlainTextEdit(self)
        self.detail_view.setObjectName("restoreHandoffResultDetail")
        self.detail_view.setReadOnly(True)
        raw_sections = []
        if receipt_text:
            raw_sections.append("— child stdout (raw) —\n" + receipt_text.rstrip("\n"))
        if refusal_text:
            raw_sections.append("— child stderr (raw) —\n" + refusal_text.rstrip("\n"))
        self.detail_view.setPlainText(
            "\n\n".join(raw_sections)
            or "(the child produced no output on either stream)"
        )
        layout.addWidget(self.detail_view, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = QPushButton("Close", self)
        self.close_button.setObjectName("restoreHandoffResultClose")
        self.close_button.clicked.connect(self.accept)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
