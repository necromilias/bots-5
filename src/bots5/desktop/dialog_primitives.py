"""Phase 11 scope amendment: shared industrial dialog/panel primitives.

Presentation-only building blocks used by Settings, Tune and the shared
generation-settings editor so the dialog surfaces speak the same B.O.T.S.
visual language instead of being bespoke dead ends:

- :class:`ChamferedPanel` — hairline-bordered panel/card with chamfered
  (machine-built) corners and restrained graphite inset layers painted with
  QPainter; no rounded gloss or ornamental shadows.
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
from math import ceil
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen, QTextOption
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QApplication,
    QMessageBox,
    QDoubleSpinBox,
    QFrame,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QPlainTextEdit,
    QTextEdit,
    QPushButton,
    QDialogButtonBox,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from bots5.domain.generation_settings_registry import (
    EXTENDED_SETTING_KEYS,
    SETTING_DEFINITIONS_BY_KEY,
    SettingDefinition,
    SettingGroup,
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
    BORDER_SUBTLE,
    BORDER_STRONG,
    BASE_CANVAS,
    HEADER_HIGH,
    HEADER_SURFACE,
    CHAMFER_SIZE,
    SURFACE_PANEL,
    SURFACE_RAISED,
    scale_value,
    DIALOG_INSET,
    ROW_GAP,
)
from .icons import icon_action
from .window_chrome import NativeWindowEdges


class ElidingLabel(QLabel):
    """One-line identity with full text retained for accessibility/tooltips."""
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setToolTip(text)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def sizeHint(self) -> QSize:
        return QSize(240, self.fontMetrics().height() + 2)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height() + 2)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)


class ContentFitLabel(QLabel):
    """Wrapped notice sized to its actual width, including stylesheet padding.

    A toolbar otherwise keeps the QLabel's narrow size-hint height even after
    assigning it the full window width. No timer or retained callback is used.
    """

    def _fit_height(self) -> None:
        height = self.heightForWidth(max(1, self.width()))
        if height > 0 and (self.minimumHeight() != height or self.maximumHeight() != height):
            self.setFixedHeight(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if self.isVisible() and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit_height()


class _FactTextView(QTextEdit):
    """Notify its owning label after the actual rendered font changes."""

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            owner = self.parentWidget()
            if owner is not None and getattr(owner, "_view", None) is self:
                owner.updateGeometry()
                if isinstance(owner, FittedWrappedLabel):
                    owner._fit_height()


class SelectableWrappedLabel(QLabel):
    """Exact label value with native selection and wrapping even within IDs.

    QLabel's selectable plain-text layout can treat some hexadecimal IDs as
    indivisible words. A read-only text document provides wrap-anywhere while
    QLabel retains the original text/accessibility contract for inspectors.
    """
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._view = _FactTextView(self)
        self._view.setReadOnly(True)
        self._view.setPlainText(text)
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setStyleSheet("QTextEdit { background: transparent; border: none; padding: 0; }")
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.document().setDocumentMargin(0)
        option = self._view.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._view.document().setDefaultTextOption(option)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setFocusProxy(self._view)
        self.setAccessibleName(text)
        self._view.setAccessibleName(text)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def setText(self, text: str) -> None:
        super().setText(text)
        if hasattr(self, "_view"):
            self._view.setPlainText(text)
            self._view.setAccessibleName(text)
            self.setAccessibleName(text)
            self.updateGeometry()

    def heightForWidth(self, width: int) -> int:
        # Qt stylesheet font resolution can give QTextEdit a different font
        # from its containing QLabel. Clone the rendered document unchanged;
        # replacing its font with the label's font undercounts wrapped lines.
        document = self._view.document().clone()
        document.setTextWidth(max(1, width))
        return ceil(document.size().height()) + 4

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().lineSpacing() + 4)

    def sizeHint(self) -> QSize:
        return QSize(240, self.heightForWidth(240))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._view.setFont(self.font())
        self._view.setPalette(self.palette())
        self._view.setGeometry(self.contentsRect())

    def paintEvent(self, event) -> None:
        # The child owns text rendering/hit-testing; drawing the QLabel's
        # original unwrapped text underneath would duplicate its glyphs.
        pass


class FittedWrappedLabel(SelectableWrappedLabel):
    """Full fact text whose height follows its actual width and current value."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._fit_height()

    def setWordWrap(self, enabled: bool) -> None:
        super().setWordWrap(enabled)
        if hasattr(self, "_view"):
            self._fit_height()

    def _fit_height(self) -> None:
        # This label owns its height from the actual text document. QLabel's
        # native height-for-width path can retain a different text-layout
        # height in QBoxLayout/QFormLayout; advertising both owners lets that
        # cached height place siblings inside the explicitly fitted widget.
        policy = self.sizePolicy()
        if policy.hasHeightForWidth():
            policy.setHeightForWidth(False)
            self.setSizePolicy(policy)
        height = self.heightForWidth(max(1, self.width()))
        if height != self.minimumHeight() or height != self.maximumHeight():
            self.setFixedHeight(height)
        # A height change inside resizeEvent need not deliver another resize
        # event before painting; keep the rendered viewport in the final rect.
        self._view.setGeometry(self.contentsRect())

    def setText(self, text: str) -> None:
        super().setText(text)
        self._fit_height()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if hasattr(self, "_view") and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._view.setFont(self.font())
            self._fit_height()


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

    def wheelEvent(self, event) -> None:
        # Some Qt/platform combinations ignore pixel-only touchpad updates.
        # Consume their actual pixel delta; retain Qt's angle-wheel handling
        # and allow the event to propagate when this surface reaches its edge.
        pixels = event.pixelDelta()
        if not pixels.isNull():
            horizontal = abs(pixels.x()) > abs(pixels.y())
            bar = self.horizontalScrollBar() if horizontal else self.verticalScrollBar()
            delta = pixels.x() if horizontal else pixels.y()
            prior = bar.value()
            bar.setValue(prior - delta)
            if bar.value() != prior:
                event.accept()
            else:
                event.ignore()
            return
        super().wheelEvent(event)

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


def position_dialog_at_anchor(dialog: QDialog, point) -> None:
    """Keep an anchored popout fully inside its current screen work area."""
    screen = QGuiApplication.screenAt(point) or dialog.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    frame = dialog.frameGeometry()
    x = max(area.left(), min(point.x(), area.right() - frame.width() + 1))
    y = max(area.top(), min(point.y(), area.bottom() - frame.height() + 1))
    dialog.move(x, y)


def normalize_message_box(box: QMessageBox, *, destructive: bool = False) -> None:
    """Normalize a confirmation without replacing its button/result contract."""
    if getattr(box, "_bots_message_normalized", False):
        return
    box._bots_message_normalized = True
    # QMessageBox can rebuild its private grid when window flags change.
    # Establish owned chrome before retaining any Qt layout items.
    _prepare_dialog_chrome(box, resizable=True)
    geometry_filter = _ConfirmationGeometryFilter(box)
    box._bots_geometry_filter = geometry_filter
    box.installEventFilter(geometry_filter)
    box.setObjectName("botsConfirmation")
    grid = box.layout()
    grid.setContentsMargins(DIALOG_INSET, DIALOG_INSET, DIALOG_INSET, DIALOG_INSET)
    grid.setSpacing(12)
    # Keep Qt's button instances and result/default machinery, but place its
    # expansive warning inside a real scrolling body with fixed actions.
    items = []
    while grid.count():
        items.append(grid.takeAt(0))
    masthead = DialogHeader(box)
    grid.addWidget(masthead, 0, 0)
    body = QWidget(box)
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(0, 0, 8, 0)
    body_layout.setSpacing(ROW_GAP)
    body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    footer = QFrame(box)
    footer.setObjectName("botsDialogFooter")
    footer_layout = QVBoxLayout(footer)
    footer_layout.setContentsMargins(0, 10, 0, 0)
    for item in items:
        widget = item.widget()
        if widget is not None:
            if isinstance(widget, QDialogButtonBox):
                footer_layout.addWidget(widget)
            else:
                if isinstance(widget, QLabel) and widget.text():
                    text = QPlainTextEdit(body)
                    text.setObjectName("botsConfirmationText")
                    text.setReadOnly(True)
                    text.setPlainText(widget.text())
                    text.setFrameShape(QFrame.Shape.NoFrame)
                    text.setMinimumWidth(0)
                    text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
                    option = text.document().defaultTextOption()
                    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                    text.document().setDefaultTextOption(option)
                    text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    body_layout.addWidget(text, 1)
                    widget.hide()
                    box._bots_confirmation_text = text
                else:
                    body_layout.addWidget(widget)
        elif item.layout() is not None:
            body_layout.addLayout(item.layout())
    scroll = scrollable(body, box)
    scroll.setMinimumSize(0, 0)
    scroll.setMaximumHeight(420)
    grid.addWidget(scroll, 1, 0)
    grid.setRowStretch(1, 1)
    grid.addWidget(footer, 2, 0)
    grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
    box._bots_confirmation_scroll = scroll
    box._bots_confirmation_footer = footer
    for button in box.buttons():
        role = box.buttonRole(button)
        if role in (QMessageBox.ButtonRole.YesRole, QMessageBox.ButtonRole.AcceptRole):
            button.setProperty("role", "destructive" if destructive else "primary")
            button.style().unpolish(button)
            button.style().polish(button)
    def settle_geometry():
        # QMessageBox recalculates a fixed geometry during Show, after our
        # application event filter. Restore the bounded scrolling shell once
        # that native calculation has completed.
        screen = box.screen() or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width = min(680, max(320, area.width() - 48))
        height = min(560, max(240, area.height() - 48))
        box.setMinimumSize(min(480, width), 240)
        box.setMaximumSize(width, height)
        line_count = sum(max(1, (len(line) + 64) // 65) for line in box.text().splitlines())
        preferred_height = min(480, max(240, 176 + 18 * line_count))
        box.resize(min(560, width), min(preferred_height, height))
        box.move(area.center() - box.rect().center())
    settle_geometry()
    QTimer.singleShot(0, box, settle_geometry)


class _ConfirmationGeometryFilter(QObject):
    """Use the recomposed layout rather than QMessageBox's label sizing."""
    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            # QMessageBox::resizeEvent/updateSize recomputes a fixed size from
            # its original text labels, defeating the scrolling shell. Qt's
            # installed layout still handles resize; skip that private sizing.
            if watched.layout() is not None:
                watched.layout().activate()
            return True
        return False


class _ConfirmationNormalizer(QObject):
    def __init__(self, destructive: bool, owner, title: str) -> None:
        super().__init__()
        self.destructive = destructive
        self.owner = owner
        self.title = title
        self._boxes = []

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.Polish and isinstance(watched, QMessageBox)
                and watched.parentWidget() is self.owner and watched.windowTitle() == self.title):
            self._boxes.append(watched)
            normalize_message_box(watched, destructive=self.destructive)
        return False


def normalized_question(parent, title, text, buttons, default, *, destructive: bool = False):
    """Retain the landed Qt static seam and its exact consent/default values."""
    application = QApplication.instance()
    observer = _ConfirmationNormalizer(destructive, parent, title)
    if application is not None:
        application.installEventFilter(observer)
    try:
        return QMessageBox.question(parent, title, text, buttons, default)
    finally:
        if application is not None:
            application.removeEventFilter(observer)


class _DialogActionOrder(QObject):
    def __init__(self, buttons):
        super().__init__(buttons)
        self.buttons = buttons

    def apply(self):
        buttons = self.buttons
        layout = buttons.layout()
        if layout is None:
            return
        while layout.count():
            layout.takeAt(0)
        reset = []
        cancel = []
        primary = []
        for button in buttons.buttons():
            role = buttons.buttonRole(button)
            if role in (QDialogButtonBox.ButtonRole.ResetRole, QDialogButtonBox.ButtonRole.HelpRole):
                reset.append(button)
            elif role in (QDialogButtonBox.ButtonRole.RejectRole, QDialogButtonBox.ButtonRole.NoRole):
                cancel.append(button)
            else:
                primary.append(button)
        for button in reset:
            layout.addWidget(button)
        layout.addStretch(1)
        for button in cancel + primary:
            layout.addWidget(button)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show:
            self.apply()
        elif event.type() == QEvent.Type.StyleChange:
            QTimer.singleShot(0, self, self.apply)
        return False


def order_dialog_actions(buttons: QDialogButtonBox) -> None:
    """Reorder existing Qt buttons without changing roles, ownership or style."""
    observer = _DialogActionOrder(buttons)
    buttons._bots_action_order = observer
    buttons.installEventFilter(observer)
    observer.apply()


def normalize_dialog(dialog: QDialog, *, size: tuple[int, int] = (640, 480), description: str = "") -> None:
    """Recompose an existing form with a fixed identity/action shell.

    The original controls, signals and result contract are retained. Its last
    action row stays outside the scroll region. This is deliberately a small
    adapter for the existing desktop forms, not a new workflow framework.
    """
    if getattr(dialog, "_bots_normalized", False):
        return
    dialog._bots_normalized = True
    old = dialog.layout()
    if old is None:
        return
    footer_item = old.takeAt(old.count() - 1)
    body = QWidget(dialog)
    body.setObjectName("botsDialogBody")
    body.setLayout(old)
    old.setContentsMargins(DIALOG_INSET, 8, DIALOG_INSET, DIALOG_INSET)
    old.setSpacing(ROW_GAP)
    old.setAlignment(Qt.AlignmentFlag.AlignTop)
    root = QVBoxLayout(dialog)
    root.setContentsMargins(DIALOG_INSET, DIALOG_INSET, DIALOG_INSET, 0)
    root.setSpacing(ROW_GAP)
    masthead = DialogHeader(dialog, description=description)
    root.addWidget(masthead)
    root.addWidget(scrollable(body, dialog), 1)
    footer = QFrame(dialog)
    footer.setObjectName("botsDialogFooter")
    footer_layout = QVBoxLayout(footer)
    footer_layout.setContentsMargins(0, 10, 0, 10)
    if footer_item is not None:
        if footer_item.widget() is not None:
            footer_layout.addWidget(footer_item.widget())
        elif footer_item.layout() is not None:
            footer_layout.addLayout(footer_item.layout())
    root.addWidget(footer)
    for buttons in dialog.findChildren(QDialogButtonBox):
        order_dialog_actions(buttons)
    for form in body.findChildren(QFormLayout):
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(ROW_GAP)
    for label in body.findChildren(QLabel):
        label.setWordWrap(True)
        label.setMinimumWidth(0)
    dialog.setMinimumSize(320, 240)
    dialog.resize(*size)
    fit_dialog_to_screen(dialog)


class ChamferedPanel(QFrame):
    """A panel or card framed by a hairline border with chamfered corners."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        card: bool = False,
        chamfer: int = CHAMFER_SIZE,
        accent: bool = False,
        header: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("botsChamferedCard" if card else "botsChamferedPanel")
        self._chamfer = chamfer
        self._accent = accent
        self._header = header
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
        major = self._chamfer >= 7 and width > 12 and height > 12
        painter.fillPath(path, QColor(BORDER_SUBTLE if major else fill))
        pen = QPen(QColor(ACCENT_STRUCTURE if self._accent else BORDER_DEFAULT))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawPath(path)
        if major:
            # A four-pixel steel-blue shoulder surrounds only major containers.
            # Its upper/left ledge catches light; the inset work face has a
            # dark edge. Small cards and transcript rows stay unembellished.
            inset = 4.0
            inner = QPainterPath()
            inner.moveTo(cut + 1, inset)
            inner.lineTo(width - cut - 1, inset)
            inner.lineTo(width - inset, cut + 1)
            inner.lineTo(width - inset, height - cut - 1)
            inner.lineTo(width - cut - 1, height - inset)
            inner.lineTo(cut + 1, height - inset)
            inner.lineTo(inset, height - cut - 1)
            inner.lineTo(inset, cut + 1)
            inner.closeSubpath()
            face = QColor(fill)
            if self._header:
                face = QLinearGradient(0, 0, 0, height)
                face.setColorAt(0, QColor(HEADER_HIGH))
                face.setColorAt(1, QColor(HEADER_SURFACE))
            painter.fillPath(inner, QBrush(face))
            painter.setPen(QPen(QColor(BASE_CANVAS), 1.0))
            painter.drawPath(inner)
            painter.setPen(QPen(QColor(BORDER_STRONG), 1.0))
            painter.drawLine(QPointF(cut, 1.5), QPointF(width - cut, 1.5))
            painter.drawLine(QPointF(1.5, cut), QPointF(1.5, height - cut))
            painter.setPen(QPen(QColor(BORDER_SUBTLE), 1.0))
            painter.drawLine(QPointF(cut + 1, 3.0), QPointF(width - cut - 1, 3.0))
            painter.drawLine(QPointF(3.0, cut + 1), QPointF(3.0, height - cut - 1))
        painter.end()


def _prepare_dialog_chrome(dialog: QDialog, *, resizable: bool) -> None:
    if dialog.property("botsOwnedDialog"):
        return
    dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    dialog.setProperty("botsOwnedDialog", True)
    dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if resizable and not hasattr(dialog, "_native_edges"):
        NativeWindowEdges(dialog)


class DialogHeader(ChamferedPanel):
    """Owned dialog chrome; Qt retains modal, focus and result handling.

    Install only in B.O.T.S. forms, never in a platform file chooser. The
    existing content/footer layout remains responsible for scrolling and
    minimum sizes. A rejected compositor operation has no synthetic fallback.
    """

    def __init__(self, dialog: QDialog, *, description: str = "", resizable: bool = True) -> None:
        _prepare_dialog_chrome(dialog, resizable=resizable)
        super().__init__(dialog, chamfer=8, header=True)
        dialog._bots_dialog_header = self
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        row.setSpacing(8)
        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(3)
        self.title_label = ElidingLabel(dialog.windowTitle(), self)
        self.title_label.setObjectName("botsDialogTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        dialog.windowTitleChanged.connect(self.title_label.setText)
        self.content_layout.addWidget(self.title_label)
        self.subtitle_label = None
        if description:
            self.subtitle_label = QLabel(description, self)
            self.subtitle_label.setObjectName("botsDialogDescription")
            self.subtitle_label.setWordWrap(True)
            self.content_layout.addWidget(self.subtitle_label)
        row.addLayout(self.content_layout, 1)
        self.close_button = QToolButton(self)
        self.close_button.setObjectName("dialogClose")
        icon_action(self.close_button, "close", "Close dialog")
        self.close_button.setToolTip("Close (Esc)")
        # Like native decoration, the chrome must not become the initial
        # form focus or an auto/default action. Escape remains QDialog's.
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(dialog.close)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            dialog = self.window()
            handle = dialog.windowHandle()
            accepted = bool(handle is not None and handle.startSystemMove())
            dialog._last_system_chrome_operation = ("move", None, accepted)
            if accepted:
                event.accept()
                return
        super().mousePressEvent(event)


class Hairline(QFrame):
    """A 1px separator rule on the default hairline border token."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("botsHairline")
        self.setFixedHeight(1)


class WorkPanel(ChamferedPanel):
    """Major operational panel with a fixed identity ledge and work body."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, chamfer=8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        self.header = QFrame(self)
        self.header.setObjectName("workPanelHeading")
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(10, 7, 10, 7)
        self.title = QLabel(title, self.header)
        self.title.setObjectName("workPanelTitle")
        self.header_layout.addWidget(self.title, 1)
        layout.addWidget(self.header)
        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 8, 10, 10)
        self.body_layout.setSpacing(8)
        layout.addWidget(self.body, 1)


class PanelPair(QWidget):
    """Bounded master/detail work panels which stack within a narrow page."""

    stacked_changed = Signal(bool)

    def __init__(
        self, master: QWidget, detail: QWidget, parent: QWidget | None = None,
        *, master_max_width: int = 280, master_weight: int = 1,
        detail_weight: int = 2, narrow_master_height: int = 150,
        breakpoint: int = 700,
    ) -> None:
        super().__init__(parent)
        self.master = master
        self.detail = detail
        self._master_max_width = master_max_width
        self._narrow_master_height = narrow_master_height
        self._breakpoint = breakpoint
        self._narrow: bool | None = None
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addWidget(master, master_weight)
        self._layout.addWidget(detail, detail_weight)
        detail.setMinimumWidth(0)
        self._fit_panels()

    def _fit_panels(self) -> None:
        narrow = self.width() < self._breakpoint
        if narrow == self._narrow:
            if narrow:
                self.master.setMaximumHeight(max(self._narrow_master_height, self.master.minimumSizeHint().height()))
            return
        self._narrow = narrow
        self._layout.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        self.master.setMinimumWidth(0 if narrow else 220)
        self.master.setMaximumWidth(16_777_215 if narrow else self._master_max_width)
        self.master.setMaximumHeight(max(self._narrow_master_height, self.master.minimumSizeHint().height()) if narrow else 16_777_215)
        self.stacked_changed.emit(narrow)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_panels()


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
        "label",
        "line_layout",
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
        self.label = None
        self.line_layout = None
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
        self._row_layout_mode = None
        self._rows: dict[str, _SettingRow] = {}
        self._capabilities: Mapping[str, Any] = {}
        layout = QVBoxLayout(self)
        # Enforce the live registry-row minimum after font/width changes;
        # a cached enclosing height-for-width hint must not compress rows.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(2)
        layout.addLayout(self._grid)
        self.group_headers: dict[str, QLabel] = {}
        for group in (SettingGroup.SAMPLING, SettingGroup.OUTPUT, SettingGroup.REASONING,
                      SettingGroup.REPETITION, SettingGroup.DETERMINISM, SettingGroup.LOGPROBS,
                      SettingGroup.ADVANCED_SAMPLING, SettingGroup.RUNTIME):
            keys = [key for key in sorted(EXTENDED_SETTING_KEYS) if SETTING_DEFINITIONS_BY_KEY[key].group is group]
            if not keys:
                continue
            header = QLabel(group.value.replace("_", " ").title(), self)
            header.setObjectName("generationGroupHeading")
            self.group_headers[group.value] = header
            self._grid.addWidget(header, self._grid.rowCount(), 0)
            for key in keys:
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
        inner = QGridLayout(line)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)
        label = QLabel(definition.label, line)
        label.setObjectName("botsFieldLabel")
        label.setToolTip(definition.description)
        # Labels reflow instead of forcing a wide minimum content width, so the
        # detail pane can shrink with the dialog rather than clip horizontally.
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setMaximumWidth(145)
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
        control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        if not isinstance(control, QPlainTextEdit):
            control.setMaximumWidth(280)
        row.label = label
        row.line_layout = inner
        inner.addWidget(label, 0, 0, 1, 4)
        inner.addWidget(control, 1, 0, 1, 2)
        inner.addWidget(row.inherit, 1, 2)
        inner.addWidget(row.badge, 1, 3)
        inner.setColumnStretch(1, 1)
        container_layout.addWidget(line)
        container_layout.addWidget(row.note)
        row.inherit.toggled.connect(lambda checked, r=row: self._refresh_row(r))
        self._grid.addWidget(container, self._grid.rowCount(), 0, 1, 1)
        row.container = container
        return row

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        narrow = self.width() < 580
        compact = self.width() < 340
        mode = "compact" if compact else "narrow" if narrow else "wide"
        if mode == self._row_layout_mode:
            return
        self._row_layout_mode = mode
        for row in self._rows.values():
            row.label.setMaximumWidth(16_777_215 if narrow else 145)
            grid = row.line_layout
            for widget in (row.label, row.control, row.inherit, row.badge):
                grid.removeWidget(widget)
            grid.setColumnStretch(0, 0)
            grid.setColumnStretch(1, 1)
            if compact:
                grid.addWidget(row.label, 0, 0, 1, 4)
                grid.addWidget(row.control, 1, 0, 1, 4)
                grid.addWidget(row.inherit, 2, 0, 1, 2)
                grid.addWidget(row.badge, 2, 2, 1, 2)
            elif narrow:
                grid.addWidget(row.label, 0, 0, 1, 4)
                grid.addWidget(row.control, 1, 0, 1, 2)
                grid.addWidget(row.inherit, 1, 2)
                grid.addWidget(row.badge, 1, 3)
            else:
                grid.addWidget(row.label, 0, 0)
                grid.addWidget(row.control, 0, 1)
                grid.addWidget(row.inherit, 0, 2)
                grid.addWidget(row.badge, 0, 3)

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
