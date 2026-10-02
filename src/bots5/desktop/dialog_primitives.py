"""Phase 11 scope amendment: shared industrial dialog/panel primitives.

Presentation-only building blocks used by Settings, Tune and the shared
generation-settings editor so the dialog surfaces speak the same B.O.T.S.
visual language instead of being bespoke dead ends:

- :class:`ChamferedPanel` — hairline-bordered panel/card with chamfered
  (machine-built) corners painted with QPainter; no gradients, no shadows,
  no rounded gloss.
- :class:`SectionHeader` — dense section identity band (title + subtitle).
- :class:`Hairline` — 1px separator rule.
- :class:`StateBadge` — subordinate capability/state marker with a gate
  property driving its tone.
- :class:`DialogInstrumentStrip` — real B.O.T.S. instrumentation only: the
  caller supplies live label/value pairs (connection counts, catalogue state,
  capability sources); the primitive never fabricates telemetry.
- :class:`GenerationSettingsEditor` — the registry-driven editor for the
  normalized generation-settings plane, shared by Tune (chat scope) and
  Settings (model and application scopes).

The primitives own presentation only: every value, capability fact and label
is supplied by the caller, and all functional behaviour stays in the dialogs
and the core.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from bots5.domain.generation_settings_registry import (
    EXTENDED_SETTING_KEYS,
    SETTING_DEFINITIONS_BY_KEY,
    SettingDefinition,
    SettingValueType,
    UI_KIND_CHECK,
    UI_KIND_CHOICES,
    UI_KIND_TEXT,
    decode_text_setting,
    encode_text_setting,
)
from bots5.desktop.theme import (
    ACCENT_STRUCTURE,
    BORDER_DEFAULT,
    CHAMFER_SIZE,
    SURFACE_PANEL,
    SURFACE_RAISED,
    scale_value,
)


class DialogScrollArea(QScrollArea):
    """Shared scroll surface for Settings/Tune/pop-out detail panes.

    Expansive content scrolls instead of growing the top-level dialog beyond
    the available work area.  Vertical scrolling appears as needed; horizontal
    scrolling is suppressed so content reflows rather than demanding a wider
    window.  Because this is a real :class:`QScrollArea`, mouse wheel, touchpad
    and scrollbar dragging work, Page Up/Down and focus traversal scroll the
    focused control into view, and the navigation/footer outside the area stay
    reachable while the detail pane scrolls.
    """

    def __init__(self, content: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("botsDialogScrollArea")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Horizontal scrolling appears only if content genuinely cannot reflow
        # narrower than the viewport; it must never be a silent clip.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        if content is not None:
            self.set_content(content)

    def set_content(self, content: QWidget) -> None:
        self.setWidget(content)


def scrollable(content: QWidget, parent: QWidget | None = None) -> DialogScrollArea:
    """Wrap ``content`` in a shared :class:`DialogScrollArea`."""
    return DialogScrollArea(content, parent)


def fit_dialog_to_screen(dialog: QDialog, *, margin: int = 48) -> None:
    """Clamp a top-level dialog to the available screen/work-area geometry.

    Keeps the dialog on-screen on small displays; combined with the shared
    scroll primitive this degrades oversized content into scrolling instead of
    clipping it.
    """
    screen = dialog.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    max_width = max(320, available.width() - margin)
    max_height = max(240, available.height() - margin)
    dialog.setMaximumSize(max_width, max_height)
    minimum = dialog.minimumSize()
    dialog.setMinimumSize(min(minimum.width(), max_width), min(minimum.height(), max_height))
    dialog.resize(min(dialog.width(), max_width), min(dialog.height(), max_height))


class ChamferedPanel(QFrame):
    """A panel or card framed by a hairline border with chamfered corners."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        card: bool = False,
        chamfer: int = CHAMFER_SIZE,
        accent: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("botsChamferedCard" if card else "botsChamferedPanel")
        self._chamfer = chamfer
        self._accent = accent
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_accent(self, accent: bool) -> None:
        if accent != self._accent:
            self._accent = accent
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = float(self.width())
        height = float(self.height())
        cut = float(scale_value(self._chamfer, 1.0))
        cut = min(cut, width / 2.0, height / 2.0)
        path = QPainterPath()
        if cut <= 0.0:
            path.addRect(QRectF(0.5, 0.5, max(0.0, width - 1.0), max(0.0, height - 1.0)))
        else:
            path.moveTo(cut, 0.5)
            path.lineTo(width - cut, 0.5)
            path.lineTo(width - 0.5, cut)
            path.lineTo(width - 0.5, height - cut)
            path.lineTo(width - cut, height - 0.5)
            path.lineTo(cut, height - 0.5)
            path.lineTo(0.5, height - cut)
            path.lineTo(0.5, cut)
            path.closeSubpath()
        fill = SURFACE_RAISED if self.objectName() == "botsChamferedCard" else SURFACE_PANEL
        painter.fillPath(path, QColor(fill))
        pen = QPen(QColor(ACCENT_STRUCTURE if self._accent else BORDER_DEFAULT))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()


class Hairline(QFrame):
    """A 1px separator rule on the default hairline border token."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("botsHairline")
        self.setFixedHeight(1)


class SectionHeader(QWidget):
    """Dense section identity: title, optional subtitle, hairline rule."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("botsSectionTitle")
        layout.addWidget(self.title_label)
        self.subtitle_label: QLabel | None = None
        if subtitle:
            self.subtitle_label = QLabel(subtitle, self)
            self.subtitle_label.setObjectName("botsSectionSubtitle")
            self.subtitle_label.setWordWrap(True)
            layout.addWidget(self.subtitle_label)
        layout.addWidget(Hairline(self))


class StateBadge(QLabel):
    """Subordinate state marker; the ``gate`` property selects the tone."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("botsStateBadge")

    def set_gate(self, gate: str) -> None:
        self.setProperty("gate", gate)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)


class DialogInstrumentStrip(QFrame):
    """Compact strip of REAL caller-supplied instrumentation.

    The strip renders exactly the label/value pairs given to
    :meth:`set_instruments` — session activity, import-queue depth, campaign
    status, catalogue state and similar live B.O.T.S. state.  It contains no
    fabricated telemetry and no decorative greeble.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("botsInstrumentStrip")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 2, 8, 2)
        self._layout.setSpacing(14)
        self._pairs: tuple[tuple[str, str], ...] = ()
        self.set_instruments(())

    def set_instruments(self, pairs) -> None:
        pairs = tuple(pairs)
        if pairs == self._pairs:
            return
        self._pairs = pairs
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for label, value in pairs:
            cell = QWidget(self)
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(4)
            label_widget = QLabel(label, cell)
            label_widget.setObjectName("botsInstrumentLabel")
            value_widget = QLabel(value, cell)
            value_widget.setObjectName("botsInstrumentValue")
            cell_layout.addWidget(label_widget)
            cell_layout.addWidget(value_widget)
            self._layout.addWidget(cell)
        self._layout.addStretch(1)


class _SettingRow:
    """Internal per-setting row state for :class:`GenerationSettingsEditor`."""

    __slots__ = (
        "definition",
        "container",
        "control",
        "inherit",
        "badge",
        "note",
        "get_value",
        "set_value",
        "set_placeholder",
        "unset_sentinel",
    )

    def __init__(self) -> None:
        self.definition: SettingDefinition | None = None
        self.container: QWidget | None = None
        self.control: QWidget | None = None
        self.inherit: QCheckBox | None = None
        self.badge: StateBadge | None = None
        self.note: QLabel | None = None
        self.get_value: Any = None
        self.set_value: Any = None
        self.set_placeholder: Any = None
        self.unset_sentinel: Any = None


_INVALID_MARKER = "__bots_invalid__"


class GenerationSettingsEditor(QWidget):
    """Registry-driven editor for the normalized generation-settings plane.

    One row per extended setting definition.  Each row carries an explicit
    inheritance checkbox: inherited rows show the effective value (control
    disabled); override rows enable the control.  Rows whose capability gate
    is not SUPPORTED stay visible but disabled with the reason shown, and a
    stored override value is preserved and marked inactive — never deleted,
    never transmitted until a supporting model is selected.

    The editor is presentation-only: callers supply the override values for
    the scope, the effective resolved values, their provenance, and the
    per-setting capability facts.
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("generationSettingsEditor")
        self._rows: dict[str, _SettingRow] = {}
        self._capabilities: Mapping[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(2)
        layout.addLayout(self._grid)
        for key in sorted(EXTENDED_SETTING_KEYS):
            self._rows[key] = self._build_row(SETTING_DEFINITIONS_BY_KEY[key])
        self._apply_advanced_visibility()

    # ------------------------------------------------------------------
    # Row construction
    # ------------------------------------------------------------------

    def _build_row(self, definition: SettingDefinition) -> _SettingRow:
        row = _SettingRow()
        row.definition = definition
        container = QWidget(self)
        container.setObjectName("generationSettingRow")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 1, 0, 1)
        container_layout.setSpacing(0)
        line = QWidget(container)
        inner = QHBoxLayout(line)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)
        label = QLabel(definition.label, line)
        label.setObjectName("botsFieldLabel")
        label.setToolTip(definition.description)
        # Labels reflow instead of forcing a wide minimum content width, so the
        # detail pane can shrink with the dialog rather than clip horizontally.
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        row.inherit = QCheckBox("inherit", line)
        row.inherit.setObjectName(f"inherit_{definition.key}")
        row.badge = StateBadge("", line)
        row.note = QLabel("", container)
        row.note.setObjectName("botsReason")
        row.note.setWordWrap(True)
        row.note.setIndent(2)
        control: QWidget
        if definition.ui_kind == UI_KIND_CHECK:
            check = QCheckBox(line)
            check.setObjectName(f"setting_{definition.key}")
            row.get_value = (lambda c=check: bool(c.isChecked()))
            row.set_value = (lambda v, c=check: c.setChecked(bool(v)))
            row.set_placeholder = (lambda text, c=check: None)
            check.toggled.connect(self._emit_changed)
            control = check
        elif definition.ui_kind == UI_KIND_CHOICES:
            combo = QComboBox(line)
            combo.setObjectName(f"setting_{definition.key}")
            assert definition.choices is not None
            combo.addItem("—", None)
            for choice in definition.choices:
                combo.addItem(choice, choice)
            row.get_value = (lambda c=combo: c.currentData())
            row.set_value = (lambda v, c=combo: c.setCurrentIndex(max(0, c.findData(v))))
            row.set_placeholder = (lambda text, c=combo: None)
            combo.currentIndexChanged.connect(self._emit_changed)
            control = combo
        elif definition.ui_kind == UI_KIND_TEXT:
            single_line = definition.value_type in {SettingValueType.STR, SettingValueType.INT}
            if single_line:
                edit = QLineEdit(line)
                edit.setObjectName(f"setting_{definition.key}")
                if definition.ui_placeholder:
                    edit.setPlaceholderText(definition.ui_placeholder)
                edit.setClearButtonEnabled(True)
                row.get_value = (lambda c=edit: self._parse(definition, c.text()))
                row.set_value = (lambda v, c=edit: c.setText(encode_text_setting(definition.key, v)))
                row.set_placeholder = (lambda text, c=edit: c.setPlaceholderText(text))
                edit.textChanged.connect(self._emit_changed)
                control = edit
            else:
                block = QPlainTextEdit(line)
                block.setObjectName(f"setting_{definition.key}")
                block.setFixedHeight(52)
                if definition.ui_placeholder:
                    block.setPlaceholderText(definition.ui_placeholder)
                row.get_value = (lambda c=block: self._parse(definition, c.toPlainText()))
                row.set_value = (lambda v, c=block: c.setPlainText(encode_text_setting(definition.key, v)))
                row.set_placeholder = (lambda text, c=block: c.setPlaceholderText(text))
                block.textChanged.connect(self._emit_changed)
                control = block
        elif definition.value_type is SettingValueType.INT:
            spin = QSpinBox(line)
            spin.setObjectName(f"setting_{definition.key}")
            minimum = int(definition.minimum) if definition.minimum is not None else 0
            maximum = int(definition.maximum) if definition.maximum is not None else 2_000_000_000
            sentinel = minimum - 1
            spin.setRange(sentinel, maximum)
            spin.setSingleStep(max(1, int(definition.ui_step)))
            spin.setSpecialValueText("—")
            row.unset_sentinel = sentinel
            row.get_value = (lambda c=spin, s=sentinel: (None if c.value() == s else int(c.value())))
            row.set_value = (lambda v, c=spin, s=sentinel: c.setValue(int(v) if v is not None else s))
            row.set_placeholder = (lambda text, c=spin: None)
            spin.valueChanged.connect(self._emit_changed)
            control = spin
        else:
            spin = QDoubleSpinBox(line)
            spin.setObjectName(f"setting_{definition.key}")
            minimum = float(definition.minimum) if definition.minimum is not None else 0.0
            maximum = float(definition.maximum) if definition.maximum is not None else 1_000_000.0
            step = float(definition.ui_step) or 0.01
            sentinel = minimum - step
            spin.setRange(sentinel, maximum)
            spin.setSingleStep(step)
            spin.setDecimals(definition.ui_decimals)
            spin.setSpecialValueText("—")
            row.unset_sentinel = sentinel
            row.get_value = (lambda c=spin, s=sentinel: (None if c.value() == s else float(c.value())))
            row.set_value = (lambda v, c=spin, s=sentinel: c.setValue(float(v) if v is not None else s))
            row.set_placeholder = (lambda text, c=spin: None)
            spin.valueChanged.connect(self._emit_changed)
            control = spin
        row.control = control
        # Controls may shrink below their size hint; the row reflows rather
        # than forcing the detail pane wider than the viewport.
        control.setMinimumWidth(0)
        inner.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        inner.addWidget(control, 1)
        inner.addWidget(row.inherit, 0, Qt.AlignmentFlag.AlignVCenter)
        inner.addWidget(row.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        container_layout.addWidget(line)
        container_layout.addWidget(row.note)
        row.inherit.toggled.connect(lambda checked, r=row: self._refresh_row(r))
        self._grid.addWidget(container, self._grid.rowCount(), 0, 1, 1)
        row.container = container
        return row

    def _parse(self, definition: SettingDefinition, text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return decode_text_setting(definition.key, text)
        except ValueError:
            return (_INVALID_MARKER, stripped)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def set_state(
        self,
        *,
        values: Mapping[str, object],
        effective: Mapping[str, object],
        provenance: Mapping[str, str],
        capabilities: Mapping[str, Any] | None,
    ) -> None:
        """Load one scope's state.

        ``values``  — this scope's stored override values (absent = inherit).
        ``effective`` — resolved effective values actually in force.
        ``provenance`` — where each effective value came from.
        ``capabilities`` — capability_key -> object with ``state.value`` for
        the selected model, or ``None`` when no model governs this scope.
        """
        self._capabilities = capabilities or {}
        for row in self._rows.values():
            assert row.definition is not None and row.inherit is not None
            key = row.definition.key
            override_value = values.get(key)
            effective_value = effective.get(key)
            row.inherit.blockSignals(True)
            row.inherit.setChecked(override_value is None)
            row.inherit.blockSignals(False)
            # Preserved-but-inactive overrides keep their stored value visible.
            display_value = override_value if override_value is not None else effective_value
            row.set_value(display_value)
            row.set_placeholder("—" if display_value is None else "")
            self._refresh_row(row, override_value is not None, effective_value, provenance.get(key))
        self._apply_advanced_visibility()

    def _capability_state(self, definition: SettingDefinition) -> str | None:
        if definition.capability_key is None:
            return None
        if not self._capabilities:
            # No model context (application scope): defaults apply wherever a
            # supporting model is selected; no gate is asserted here.
            return None
        capability = self._capabilities.get(definition.capability_key)
        if capability is None:
            return "unknown"
        state = getattr(capability, "state", None)
        value = getattr(state, "value", state)
        return str(value) if value is not None else "unknown"

    def _refresh_row(
        self,
        row: _SettingRow,
        has_override: bool | None = None,
        effective_value: object = None,
        provenance: str | None = None,
        *_ignored: object,
    ) -> None:
        assert row.definition is not None and row.inherit is not None
        if has_override is None:
            has_override = not row.inherit.isChecked()
        state = self._capability_state(row.definition)
        inherit = row.inherit.isChecked()
        gated = state in {"unsupported", "unknown"}
        row.control.setEnabled(not gated and not inherit)
        row.inherit.setEnabled(not gated)
        if gated:
            gate = f"inactive-{state}" if has_override else state
        elif state is None:
            gate = "default" if not has_override else "active"
        else:
            gate = "active" if has_override else "supported"
        assert row.badge is not None and row.note is not None
        row.badge.setText(self._badge_text(gate))
        row.badge.set_gate(gate)
        note_parts: list[str] = []
        if gate == "inactive-unsupported":
            note_parts.append("stored value preserved — unsupported by endpoint")
        elif gate == "inactive-unknown":
            note_parts.append("stored value preserved — capability unknown")
        elif gate == "unsupported":
            note_parts.append("unsupported by endpoint")
        elif gate == "unknown":
            note_parts.append("capability unknown")
        elif gate == "active":
            note_parts.append("emitted with this request")
        if effective_value is not None and provenance:
            if inherit:
                note_parts.append(f"effective from {provenance}")
            elif gate == "active":
                note_parts.append(f"override (resolved from {provenance})")
        row.note.setText(" · ".join(note_parts))

    def _badge_text(self, gate: str) -> str:
        return {
            "active": "ACTIVE",
            "supported": "READY",
            "default": "DEFAULT",
            "unsupported": "UNSUPPORTED",
            "unknown": "UNKNOWN",
            "inactive-unsupported": "INACTIVE",
            "inactive-unknown": "INACTIVE",
            "inactive-unserializable": "INACTIVE",
            "unserializable": "MAPPED-ELSEWHERE",
        }.get(gate, "")

    def _apply_advanced_visibility(self) -> None:
        # Mick's required Tune behaviour: EVERY normalized setting stays
        # visible.  Capability gating greys out and locks unsupported/unknown
        # controls; the UI never hides an advanced control merely to simplify
        # the surface.  Oversized content is handled by the shared dialog
        # scroll primitive, not by collapsing rows.
        for row in self._rows.values():
            assert row.container is not None
            row.container.setVisible(True)

    def _emit_changed(self, *_args: object) -> None:
        self.changed.emit()

    # ------------------------------------------------------------------
    # Payload
    # ------------------------------------------------------------------

    def override_payload(self) -> dict[str, object]:
        """Return this scope's complete override mapping (replace semantics).

        Inherited rows are absent; override rows carry their current value or
        ``None`` when the control is empty.  Rows with a parse error carry a
        ``(__bots_invalid__, text)`` marker tuple that the caller must refuse
        (fail closed) instead of persisting unvalidated input.
        """
        payload: dict[str, object] = {}
        for key, row in self._rows.items():
            assert row.inherit is not None and row.get_value is not None
            if row.inherit.isChecked():
                continue
            payload[key] = row.get_value()
        return payload

    def has_invalid_values(self) -> bool:
        for row in self._rows.values():
            assert row.inherit is not None and row.get_value is not None
            if row.inherit.isChecked():
                continue
            value = row.get_value()
            if isinstance(value, tuple) and value and value[0] == _INVALID_MARKER:
                return True
        return False
