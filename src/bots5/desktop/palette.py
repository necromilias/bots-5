"""
Command palette dialog for Phase 11.

This module implements a searchable, keyboard-navigable palette that:
- Displays all registered actions grouped by category
- Filters results as the operator types
- Shows the current keyboard shortcut for each action
- Dispatches selected actions to their handlers

The palette is triggered by Ctrl+Shift+P by default.
"""

from __future__ import annotations

import re
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from .actions import ActionDefinition, ActionRegistry
from .dialog_primitives import DialogHeader, WorkPanel, fit_dialog_to_screen
from .theme import DIALOG_INSET, ROW_GAP


class PaletteFilter(QObject):
    """
    Simple fuzzy matcher for palette entries.

    Matching is performed against both title and category.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pattern: str = ""

    def set_pattern(self, pattern: str) -> None:
        """Set the search pattern (empty string shows all)."""
        self._pattern = pattern.lower()

    def matches(self, action: ActionDefinition) -> bool:
        """Return True if the action matches the current pattern."""
        if not self._pattern:
            return True
        pattern = self._pattern
        # Match against title
        if pattern in action.title.lower():
            return True
        # Match against category
        if pattern in action.category.lower():
            return True
        # Fuzzy character matching
        return self._fuzzy_match(pattern, action.title.lower()) or self._fuzzy_match(
            pattern, action.category.lower()
        )

    def _fuzzy_match(self, pattern: str, text: str) -> bool:
        """Check if pattern characters appear in order in text."""
        if not pattern:
            return True
        pos = 0
        for ch in pattern:
            pos = text.find(ch, pos)
            if pos == -1:
                return False
            pos += 1
        return True


class CommandPaletteDialog(QDialog):
    """
    Searchable command palette dialog.

    The palette displays all registered actions grouped by category.
    Users can filter by typing, navigate with keyboard (Up/Down/Enter),
    and invoke actions by pressing Enter or clicking.

    Keyboard navigation:
        - Type to filter actions
        - Up/Down to navigate
        - Enter/Return to invoke selected action
        - Escape to cancel

    Signal:
        action_selected: Emitted when an action is invoked with the action_id.
    """

    action_selected = Signal(str)  # action_id

    def __init__(
        self,
        registry: ActionRegistry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._filter = PaletteFilter(self)
        self._selected_action_id: str | None = None

        self.setWindowTitle("Command Palette")
        self.setObjectName("commandPaletteDialog")
        self.setModal(True)
        self.setMinimumSize(320, 240)
        self.resize(720, 540)
        fit_dialog_to_screen(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(DIALOG_INSET, DIALOG_INSET, DIALOG_INSET, 0)
        layout.setSpacing(ROW_GAP)
        masthead = DialogHeader(self)
        layout.addWidget(masthead)

        # Search input
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(ROW_GAP)

        search_label = QLabel("Search", self)
        search_label.setObjectName("paletteSearchIcon")
        search_layout.addWidget(search_label)

        self._search_edit = QLineEdit(self)
        self._search_edit.setObjectName("paletteSearchEdit")
        self._search_edit.setPlaceholderText("Type to filter actions...")
        self._search_edit.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self._search_edit, 1)

        # Shortcuts hint
        shortcuts_hint = QLabel("Enter: select  Esc: cancel", self)
        shortcuts_hint.setObjectName("paletteShortcutsHint")
        shortcuts_hint.setStyleSheet(
            "QLabel#paletteShortcutsHint { color: #888; font-size: 11px; }"
        )
        shortcuts_hint.setWordWrap(True)
        masthead.content_layout.addWidget(shortcuts_hint)
        masthead.content_layout.addLayout(search_layout)
        results = WorkPanel("Registered actions", self)
        layout.addWidget(results, 1)

        # Phase 11 M1: shortcut conflicts must be visible to the operator and must
        # never be silently accepted.  The palette is where shortcut ownership is
        # displayed, so the conflict report belongs here rather than only on stdout.
        conflicts = registry.detect_conflicts()
        if conflicts:
            conflict_lines = "; ".join(
                f"{shortcut} -> {', '.join(action.title for action in actions)}"
                for shortcut, actions in sorted(conflicts.items())
            )
            self._conflict_banner = QLabel(
                f"Shortcut conflicts detected: {conflict_lines}", self
            )
            self._conflict_banner.setObjectName("paletteConflictBanner")
            self._conflict_banner.setWordWrap(True)
            self._conflict_banner.setStyleSheet(
                "QLabel#paletteConflictBanner { color: #ffd79a; font-size: 11px; }"
            )
            results.body_layout.addWidget(self._conflict_banner)

        # Results list (grouped by category)
        self._list_widget = QListWidget(self)
        self._list_widget.setObjectName("paletteResultList")
        self._list_widget.itemActivated.connect(self._on_item_activated)
        self._list_widget.currentItemChanged.connect(self._on_current_item_changed)
        self._list_widget.setMinimumWidth(0)
        self._list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        results.body_layout.addWidget(self._list_widget, 1)

        # Footer
        footer = QWidget(self)
        footer.setObjectName("botsDialogFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 8, 0, 8)
        footer_layout.setSpacing(12)

        self._status_label = QLabel("", footer)
        self._status_label.setObjectName("paletteStatusLabel")
        self._status_label.setStyleSheet(
            "QLabel#paletteStatusLabel { color: #888; font-size: 11px; }"
        )
        footer_layout.addWidget(self._status_label)

        footer_layout.addStretch(1)

        # Cancel button (always present for explicit dismissal)
        cancel_button = QDialogButtonBox.StandardButton.Close
        self._button_box = QDialogButtonBox(cancel_button, footer)
        self._button_box.rejected.connect(self.reject)
        footer_layout.addWidget(self._button_box.button(QDialogButtonBox.StandardButton.Close))

        layout.addWidget(footer)

        # Register keyboard shortcuts
        self._escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._escape_shortcut.activated.connect(self.reject)

        # Initial population
        self._populate_list()

    def _on_search_changed(self, text: str) -> None:
        """Handle search text changes."""
        self._filter.set_pattern(text)
        self._populate_list()

    def _populate_list(self) -> None:
        """Rebuild the result list based on current filter."""
        self._list_widget.clear()
        self._selected_action_id = None

        # Get all actions
        all_actions = self._registry.get_all_actions()
        # Filter and sort by category, then title
        filtered = [a for a in all_actions if self._filter.matches(a)]
        filtered.sort(key=lambda a: (a.category, a.title))

        if not filtered:
            # Show no results message
            item = QListWidgetItem("No actions match your search")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._list_widget.addItem(item)
            self._status_label.setText(f"0 of {len(all_actions)} actions")
            return

        # Group by category
        from collections import defaultdict

        by_category: dict[str, list[ActionDefinition]] = defaultdict(list)
        for action in filtered:
            by_category[action.category].append(action)

        # Add items grouped by category
        current_category: str | None = None
        for action in filtered:
            if action.category != current_category:
                # Category header
                header = QListWidgetItem(f"  {action.category.upper()}")
                header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self._list_widget.addItem(header)
                current_category = action.category

            # Action item
            chord = action.default_shortcut
            item = QListWidgetItem(f"  {action.title}" + (f"    {chord}" if chord else ""))
            item.setData(Qt.ItemDataRole.UserRole, action.action_id)
            shortcut_text = action.default_shortcut if action.default_shortcut else ""
            item.setToolTip(f"{action.title} ({shortcut_text})")
            self._list_widget.addItem(item)

        self._status_label.setText(f"{len(filtered)} of {len(all_actions)} actions")

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        """Handle double-click or Enter on an item."""
        action_id = item.data(Qt.ItemDataRole.UserRole)
        if action_id and isinstance(action_id, str):
            self._selected_action_id = action_id
            self.accept()

    def _on_current_item_changed(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        """Track the currently selected item for Enter key dispatch."""
        if current is None:
            return
        action_id = current.data(Qt.ItemDataRole.UserRole)
        if action_id and isinstance(action_id, str):
            self._selected_action_id = action_id

    def get_selected_action_id(self) -> str | None:
        """Return the selected action_id after accept(), or None on reject."""
        return self._selected_action_id

    def focus_search(self) -> None:
        """Focus the search input for keyboard-first operation."""
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    @staticmethod
    def show_palette(registry: ActionRegistry, parent: QWidget | None) -> str | None:
        """
        Convenience method to show the palette and return the selected action_id.

        Args:
            registry: The ActionRegistry containing available actions.
            parent: Parent widget for the dialog.

        Returns:
            The selected action_id, or None if cancelled.
        """
        dialog = CommandPaletteDialog(registry, parent)
        # Focus search immediately for keyboard-first usage
        dialog.focus_search()
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            return dialog.get_selected_action_id()
        return None
