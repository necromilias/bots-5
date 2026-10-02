"""Phase 11 F8 — model-selector polish.

The landed desktop selector was a flat ``QComboBox`` whose item text was the
connection / provider-model-id / availability string.  This module adds the
information hierarchy required by the sealed Phase 11 design (§4.4) without
touching the landed contracts around it:

- Primary line: the catalogue entry's ``display_name`` (previously unused in
  the selector — adopting it here is the change).
- Secondary line: connection name + context window, when the catalogue
  metadata carries one.
- A connection-health pill per connection group (derived, presentation-only).
- Entries grouped by connection.
- Search/filter that narrows the visible entries.
- A detail card showing context length, streaming support and pricing drawn
  from the catalogue metadata when it is populated; pricing is omitted
  cleanly when the metadata does not carry it.

Recents/favourites are SESSION-ONLY (fork R-6 settled for Phase 11): this
module adds no persistence, no settings row and no migration.

Landed contracts preserved (unchanged elsewhere):
- exactly one ``QLabel`` with objectName ``modelPill`` lives in ``TopBar``;
- in legacy / no-Phase-5 mode the selector is hidden and Tune/Settings/Tool
  stay disabled.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import (
    ACCENT_BLUE,
    BORDER_SUBTLE,
    STATUS_ERROR,
    STATUS_SUCCESS,
    STATUS_WARNING,
    SURFACE_BUBBLE,
    TEXT_INACTIVE,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


# Health states for the connection-health pill.  The derivation is
# presentation-only: it never mutates store state and it never invents
# substrate that does not exist.
HEALTH_OK = "ok"
HEALTH_UNKNOWN = "unknown"
HEALTH_DOWN = "down"

_HEALTH_LABELS = {
    HEALTH_OK: "Ready",
    HEALTH_UNKNOWN: "Unknown",
    HEALTH_DOWN: "Offline",
}

_PILL_STYLES = {
    HEALTH_OK: (
        "QLabel#connectionHealthPill {"
        f" color: {STATUS_SUCCESS};"
        " border: 1px solid #2f5c38;"
        " border-radius: 8px;"
        " padding: 1px 7px;"
        " font-size: 10px;"
        " font-weight: 700;"
        "}"
    ),
    HEALTH_UNKNOWN: (
        "QLabel#connectionHealthPill {"
        f" color: {STATUS_WARNING};"
        " border: 1px solid #6b571f;"
        " border-radius: 8px;"
        " padding: 1px 7px;"
        " font-size: 10px;"
        " font-weight: 700;"
        "}"
    ),
    HEALTH_DOWN: (
        "QLabel#connectionHealthPill {"
        f" color: {STATUS_ERROR};"
        " border: 1px solid #6b2b26;"
        " border-radius: 8px;"
        " padding: 1px 7px;"
        " font-size: 10px;"
        " font-weight: 700;"
        "}"
    ),
}

_ROW_STYLESHEET = (
    """
QFrame#modelSelectorRow, QFrame#modelSelectorGroupHeader {
    background: transparent;
    border: none;
}
QLabel#modelSelectorPrimary {
    color: %TEXT_PRIMARY%;
    font-size: 13px;
    font-weight: 600;
}
QLabel#modelSelectorSecondary {
    color: %TEXT_SECONDARY%;
    font-size: 11px;
}
QLabel#modelSelectorGroupLabel {
    color: %TEXT_SECONDARY%;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
}
QLabel#modelSelectorDetailTitle {
    color: %TEXT_PRIMARY%;
    font-size: 13px;
    font-weight: 700;
}
QLabel#modelSelectorDetailSubtitle {
    color: %TEXT_SECONDARY%;
    font-size: 11px;
}
QLabel#modelSelectorDetailKey {
    color: %TEXT_SECONDARY%;
    font-size: 11px;
    font-weight: 700;
}
QLabel#modelSelectorDetailValue {
    color: %TEXT_PRIMARY%;
    font-size: 11px;
}
QLabel#modelSelectorDetailEmpty {
    color: %TEXT_INACTIVE%;
    font-size: 11px;
}
QFrame#modelSelectorDetailCard {
    background: %SURFACE_BUBBLE%;
    border: 1px solid %BORDER_SUBTLE%;
    border-radius: 6px;
}
QPushButton#modelSelectorChoose {
    background: %ACCENT_BLUE%;
    color: #0d1117;
    border: 1px solid %ACCENT_BLUE%;
    border-radius: 4px;
    padding: 5px 14px;
    font-weight: 700;
}
""".replace("%TEXT_PRIMARY%", TEXT_PRIMARY)
    .replace("%TEXT_SECONDARY%", TEXT_SECONDARY)
    .replace("%TEXT_INACTIVE%", TEXT_INACTIVE)
    .replace("%SURFACE_BUBBLE%", SURFACE_BUBBLE)
    .replace("%BORDER_SUBTLE%", BORDER_SUBTLE)
    .replace("%ACCENT_BLUE%", ACCENT_BLUE)
)


def _format_context_tokens(value: object) -> str | None:
    """Render a positive token count as ``128,000 tokens``; else ``None``."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return f"{value:,} tokens"


def connection_health_state(entry: "ModelSelectorEntry") -> str:
    """Derive the presentation-only health state for an entry's connection."""

    if not entry.connection_available or entry.connection_refresh_status == "failed":
        return HEALTH_DOWN
    if entry.connection_refresh_status == "succeeded":
        return HEALTH_OK
    if entry.connection_backend_type == "fake":
        # The deterministic built-in backend ships a trusted, always-available
        # seeded catalogue; it never has a discovery refresh to succeed.
        return HEALTH_OK
    return HEALTH_UNKNOWN


def entry_secondary_line(entry: "ModelSelectorEntry") -> str:
    """Connection name + context window; omit the context part when unknown."""

    parts: list[str] = []
    if entry.connection_name:
        parts.append(entry.connection_name)
    context = _format_context_tokens(
        entry.metadata.get("context_length")
        if entry.metadata is not None
        else None
    ) or _format_context_tokens(entry.context_tokens)
    if context:
        parts.append(context)
    return " · ".join(parts)


def entry_detail_rows(entry: "ModelSelectorEntry") -> tuple[tuple[str, str], ...]:
    """Detail-card rows (context length, streaming, pricing) from metadata.

    Rows exist only when the catalogue metadata actually carries them; absent
    pricing (and any other absent field) is omitted cleanly.
    """

    metadata = entry.metadata or {}
    rows: list[tuple[str, str]] = []
    context = _format_context_tokens(metadata.get("context_length")) or _format_context_tokens(
        entry.context_tokens
    )
    if context:
        rows.append(("Context", context))
    streaming = metadata.get("streaming")
    if isinstance(streaming, bool):
        rows.append(("Streaming", "supported" if streaming else "not supported"))
    pricing = metadata.get("pricing")
    if isinstance(pricing, (str, int, float)) and not isinstance(pricing, bool):
        text = str(pricing).strip()
        if text:
            rows.append(("Pricing", text))
    return tuple(rows)


@dataclass(frozen=True)
class ModelSelectorEntry:
    """Presentation record for one catalogue entry in the selector."""

    model_entry_id: str
    display_name: str
    provider_model_id: str = ""
    connection_id: str = ""
    connection_name: str = ""
    availability: str = "available"
    connection_available: bool = True
    connection_refresh_status: str = "never"
    connection_backend_type: str = ""
    context_tokens: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class _SearchEdit(QLineEdit):
    """Search box that hands navigation keys to the popup list."""

    navigate = Signal(int)  # +1 down, -1 up
    commit = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt naming)
        if event.key() == Qt.Key.Key_Down:
            self.navigate.emit(1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up:
            self.navigate.emit(-1)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def _build_row_widget(entry: ModelSelectorEntry, parent: QWidget) -> QFrame:
    """One selectable entry row: primary display_name + secondary line."""

    row = QFrame(parent)
    row.setObjectName("modelSelectorRow")
    layout = QVBoxLayout(row)
    layout.setContentsMargins(8, 4, 8, 4)
    layout.setSpacing(1)
    primary = QLabel(entry.display_name or entry.provider_model_id, row)
    primary.setObjectName("modelSelectorPrimary")
    layout.addWidget(primary)
    secondary_text = entry_secondary_line(entry)
    if secondary_text:
        secondary = QLabel(secondary_text, row)
        secondary.setObjectName("modelSelectorSecondary")
        layout.addWidget(secondary)
    return row


def _build_header_widget(connection_name: str, health: str, parent: QWidget) -> QFrame:
    """Connection group header: name label + connection-health pill."""

    header = QFrame(parent)
    header.setObjectName("modelSelectorGroupHeader")
    layout = QHBoxLayout(header)
    layout.setContentsMargins(8, 6, 8, 3)
    layout.setSpacing(6)
    label = QLabel(connection_name.upper(), header)
    label.setObjectName("modelSelectorGroupLabel")
    layout.addWidget(label)
    layout.addStretch(1)
    pill = QLabel(_HEALTH_LABELS.get(health, health), header)
    pill.setObjectName("connectionHealthPill")
    pill.setProperty("health", health)
    pill.setStyleSheet(_PILL_STYLES.get(health, ""))
    layout.addWidget(pill)
    return header


class ModelSelectorDetailCard(QFrame):
    """Detail card: context length, streaming support, pricing (when populated)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modelSelectorDetailCard")
        self.setStyleSheet(_ROW_STYLESHEET)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 8, 10, 8)
        self._layout.setSpacing(3)
        self._title = QLabel("", self)
        self._title.setObjectName("modelSelectorDetailTitle")
        self._title.setWordWrap(True)
        self._layout.addWidget(self._title)
        self._subtitle = QLabel("", self)
        self._subtitle.setObjectName("modelSelectorDetailSubtitle")
        self._subtitle.setWordWrap(True)
        self._layout.addWidget(self._subtitle)
        self._empty = QLabel("No catalogue metadata for this entry", self)
        self._empty.setObjectName("modelSelectorDetailEmpty")
        self._layout.addWidget(self._empty)
        self._row_holders: list[QWidget] = []
        self.set_entry(None)

    def set_entry(self, entry: ModelSelectorEntry | None) -> None:
        for holder in self._row_holders:
            self._layout.removeWidget(holder)
            # Detach immediately so the widget tree never reports stale rows.
            holder.setParent(None)
            holder.deleteLater()
        self._row_holders.clear()
        if entry is None:
            self._title.setText("")
            self._subtitle.setText("")
            self._empty.setVisible(False)
            return
        self._title.setText(entry.display_name or entry.provider_model_id)
        identity_bits = [bit for bit in (entry.provider_model_id, entry.availability) if bit]
        self._subtitle.setText(" · ".join(identity_bits))
        rows = entry_detail_rows(entry)
        self._empty.setVisible(not rows)
        for key, value in rows:
            key_label = QLabel(key)
            key_label.setObjectName("modelSelectorDetailKey")
            value_label = QLabel(value)
            value_label.setObjectName("modelSelectorDetailValue")
            value_label.setWordWrap(True)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(key_label)
            row.addStretch(1)
            row.addWidget(value_label, 1)
            holder = QWidget(self)
            holder.setLayout(row)
            self._layout.addWidget(holder)
            self._row_holders.append(holder)


class ModelSelectorPopup(QDialog):
    """Searchable, connection-grouped model chooser with a detail card.

    The popup is presentation + selection intent only: it emits
    ``model_entry_chosen`` and the landed window command path performs the
    durable selection.  It persists nothing.
    """

    model_entry_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select model")
        self.setObjectName("modelSelectorPopup")
        self.setModal(False)
        self.setMinimumWidth(430)
        self.setMinimumHeight(420)
        self.setStyleSheet(_ROW_STYLESHEET)

        self._entries: tuple[ModelSelectorEntry, ...] = ()
        self._selected_model_entry_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.search_edit = _SearchEdit(self)
        self.search_edit.setObjectName("modelSelectorSearch")
        self.search_edit.setPlaceholderText("Filter models…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        self.search_edit.navigate.connect(self._move_current)
        self.search_edit.commit.connect(self._commit_current)
        layout.addWidget(self.search_edit)

        self.list = QListWidget(self)
        self.list.setObjectName("modelSelectorList")
        self.list.currentItemChanged.connect(self._on_current_item_changed)
        self.list.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self.list, 1)

        self.detail_card = ModelSelectorDetailCard(self)
        layout.addWidget(self.detail_card)

        footer = QHBoxLayout()
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("modelSelectorStatus")
        footer.addWidget(self.status_label, 1)
        self.choose_button = QPushButton("Use model", self)
        self.choose_button.setObjectName("modelSelectorChoose")
        self.choose_button.clicked.connect(self._commit_current)
        footer.addWidget(self.choose_button)
        layout.addLayout(footer)

    # -- population ---------------------------------------------------------

    def set_entries(
        self,
        entries: Sequence[ModelSelectorEntry],
        selected_model_entry_id: str | None,
    ) -> None:
        self._entries = tuple(entries)
        self._selected_model_entry_id = selected_model_entry_id
        self._apply_filter(self.search_edit.text())

    def _apply_filter(self, pattern: str) -> None:
        needle = pattern.strip().casefold()
        self.list.clear()
        visible = [
            entry
            for entry in self._entries
            if not needle
            or needle in entry.display_name.casefold()
            or needle in entry.provider_model_id.casefold()
            or needle in entry.connection_name.casefold()
        ]
        current_connection: str | None = None
        for entry in visible:
            if entry.connection_id != current_connection:
                current_connection = entry.connection_id
                header = _build_header_widget(
                    entry.connection_name, connection_health_state(entry), self
                )
                header_item = QListWidgetItem()
                header_item.setFlags(Qt.ItemFlag.NoItemFlags)
                header_item.setSizeHint(header.sizeHint())
                self.list.addItem(header_item)
                self.list.setItemWidget(header_item, header)
            row = _build_row_widget(entry, self)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, entry.model_entry_id)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            if entry.model_entry_id == self._selected_model_entry_id:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            for index in range(self.list.count()):
                candidate = self.list.item(index)
                if candidate.flags() & Qt.ItemFlag.ItemIsEnabled:
                    self.list.setCurrentRow(index)
                    break
        shown_entries = sum(
            1
            for index in range(self.list.count())
            if self.list.item(index).data(Qt.ItemDataRole.UserRole)
        )
        total = len(self._entries)
        self.status_label.setText(
            f"{shown_entries} of {total} models" if needle else f"{total} models"
        )
        self._sync_detail_card()

    # -- selection ----------------------------------------------------------

    def _current_entry(self) -> ModelSelectorEntry | None:
        item = self.list.currentItem()
        if item is None:
            return None
        model_entry_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(model_entry_id, str):
            return None
        return next((e for e in self._entries if e.model_entry_id == model_entry_id), None)

    def _on_current_item_changed(self, current: QListWidgetItem | None, _previous) -> None:
        self._sync_detail_card()

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        self._commit_current()

    def _move_current(self, direction: int) -> None:
        row = self.list.currentRow()
        count = self.list.count()
        if count == 0:
            return
        for _ in range(count):
            row += direction
            if row < 0 or row >= count:
                return
            item = self.list.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(row)
                return

    def _commit_current(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        self._selected_model_entry_id = entry.model_entry_id
        self.model_entry_chosen.emit(entry.model_entry_id)
        self.accept()

    def _sync_detail_card(self) -> None:
        self.detail_card.set_entry(self._current_entry())
        entry = self._current_entry()
        self.choose_button.setEnabled(entry is not None)

    # -- API ----------------------------------------------------------------

    def focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()


class ModelSelectorButton(QFrame):
    """Top-bar host for the rich selector (replaces the flat combo box).

    Keeps the landed objectName ``modelSelector`` and a QComboBox-shaped
    selection API (``currentIndex``/``setCurrentIndex``/
    ``currentIndexChanged``) so the pinned Phase 5 selector behaviour is
    preserved.  Clicking it only emits ``open_requested``; hosting the popup
    is the window's job.
    """

    currentIndexChanged = Signal(int)
    open_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modelSelector")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Select the durable chat model")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 8, 4)
        layout.setSpacing(6)
        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(0)
        self.primary_label = QLabel("Selection required", self)
        self.primary_label.setObjectName("modelSelectorButtonPrimary")
        text_column.addWidget(self.primary_label)
        self.secondary_label = QLabel("", self)
        self.secondary_label.setObjectName("modelSelectorButtonSecondary")
        text_column.addWidget(self.secondary_label)
        layout.addLayout(text_column, 1)
        self.chevron = QLabel("▾", self)
        self.chevron.setObjectName("modelSelectorChevron")
        layout.addWidget(self.chevron)

        self._entries: tuple[ModelSelectorEntry, ...] = ()
        self._current_index = -1

    # -- population ---------------------------------------------------------

    def set_entries(
        self,
        entries: Sequence[ModelSelectorEntry],
        selected_model_entry_id: str | None,
    ) -> None:
        self._entries = tuple(entries)
        selected_index = -1
        for index, entry in enumerate(self._entries):
            if entry.model_entry_id == selected_model_entry_id:
                selected_index = index
                break
        self._set_current_index(selected_index, emit=False)

    def has_entries(self) -> bool:
        return bool(self._entries)

    def entries(self) -> tuple[ModelSelectorEntry, ...]:
        return self._entries

    def model_entry_id_at(self, index: int) -> str | None:
        if 0 <= index < len(self._entries):
            return self._entries[index].model_entry_id
        return None

    def current_model_entry_id(self) -> str | None:
        return self.model_entry_id_at(self._current_index)

    # -- QComboBox-shaped API -----------------------------------------------

    def currentIndex(self) -> int:  # noqa: N802 (QComboBox API shape)
        return self._current_index

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 (QComboBox API shape)
        self._set_current_index(int(index), emit=True)

    def _set_current_index(self, index: int, *, emit: bool) -> None:
        changed = index != self._current_index
        self._current_index = index
        entry = self._entries[index] if 0 <= index < len(self._entries) else None
        if entry is None:
            self.primary_label.setText("Selection required")
            self.secondary_label.setText("")
            self.setToolTip("This chat has no durable current model selection.")
        else:
            self.primary_label.setText(entry.display_name or entry.provider_model_id)
            secondary = entry_secondary_line(entry)
            self.secondary_label.setText(secondary)
            tooltip_bits = [bit for bit in (secondary, entry.availability) if bit]
            self.setToolTip(" — ".join(tooltip_bits) or entry.display_name)
        if emit and changed:
            self.currentIndexChanged.emit(self._current_index)

    # -- interaction --------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt naming)
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt naming)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.open_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
