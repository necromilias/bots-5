"""
Action registry for Phase 11 command palette and keybindings.

This module defines a single declarative source of truth for all user-invokable
actions in the desktop application. Each action declares its ID, label, category,
default keyboard shortcut, handler callable, and an enabled-predicate.

The registry is used by:
- CommandPaletteDialog to populate searchable entries
- Keyboard shortcut dispatch to map chords to actions
- Menu construction (derived from registered actions)

Conflict detection ensures no two actions claim the same shortcut chord.
Persistence is deferred to a future Phase 11 milestone (R-10 fork).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


class ActionHandler(Protocol):
    """Callable signature for an action handler."""

    def __call__(self, *args, **kwargs) -> None: ...


class ActionEnabledPredicate(Protocol):
    """Callable signature for an action enabled predicate."""

    def __call__(self, *args, **kwargs) -> bool: ...


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """
    Declarative definition of a user-invokable action.

    Fields:
        action_id: Unique identifier (dotted path style, e.g. "chat.create").
        title: Human-readable label shown in palette and menus.
        category: Logical grouping for palette filtering (e.g. "chat", "view").
        default_shortcut: Default keyboard chord (e.g. "Ctrl+Shift+N").
        handler: Callable invoked when the action is dispatched.
        is_enabled: Optional predicate controlling action availability.
    """

    action_id: str
    title: str
    category: str
    default_shortcut: str
    handler: ActionHandler
    is_enabled: ActionEnabledPredicate | None = None


class ActionRegistry:
    """
    Single source of truth for all desktop actions.

    The registry holds ActionDefinitions and provides conflict detection
    for shortcut chords. Actions are registered once at application startup;
    the registry is effectively immutable after registration.

    Conflict detection:
        - Multiple actions may register the same chord; conflicts are reported
          but not silently resolved (operator must choose).
        - The registry exposes conflict sets for the palette to surface to the user.

    Persistence (M4b/M6): overrides are read from and written to the R-10
    SQLite settings plane by the MainWindow wiring; this registry only holds
    the in-memory effective shortcut state.
    """

    def __init__(self) -> None:
        self._actions_by_id: dict[str, ActionDefinition] = {}
        self._actions_by_shortcut: dict[str, list[ActionDefinition]] = {}
        self._conflicts: dict[str, tuple[ActionDefinition, ...]] = {}
        # Phase 11 M4b/M6: persisted keybinding overrides (R-10 SQLite plane).
        # An override maps an action id to an effective shortcut chord that
        # replaces the action's default for conflict detection and dispatch;
        # an empty/absent entry means "use the default shortcut".
        self._overrides: dict[str, str] = {}

    def register(self, action: ActionDefinition) -> None:
        """
        Register an action definition.

        Args:
            action: ActionDefinition to register.

        Raises:
            ValueError: If an action with the same ID is already registered.
        """
        if action.action_id in self._actions_by_id:
            raise ValueError(f"action already registered: {action.action_id}")
        self._actions_by_id[action.action_id] = action

        # Track shortcut usage for conflict detection
        shortcut = action.default_shortcut
        if shortcut:
            existing = self._actions_by_shortcut.get(shortcut, [])
            existing.append(action)
            self._actions_by_shortcut[shortcut] = existing

    def get_action(self, action_id: str) -> ActionDefinition | None:
        """Return the ActionDefinition for the given ID, or None."""
        return self._actions_by_id.get(action_id)

    def get_all_actions(self) -> tuple[ActionDefinition, ...]:
        """Return all registered actions."""
        return tuple(self._actions_by_id.values())

    def get_actions_by_category(self) -> dict[str, tuple[ActionDefinition, ...]]:
        """
        Return actions grouped by category.

        Categories are sorted alphabetically; actions within each category
        are sorted by title.
        """
        groups: dict[str, list[ActionDefinition]] = {}
        for action in self._actions_by_id.values():
            group = groups.setdefault(action.category, [])
            group.append(action)
        # Sort actions within each category by title
        for group in groups.values():
            group.sort(key=lambda a: a.title)
        # Sort categories alphabetically
        return {k: tuple(groups[k]) for k in sorted(groups.keys())}

    def detect_conflicts(self) -> dict[str, tuple[ActionDefinition, ...]]:
        """
        Detect and report shortcut conflicts.

        Conflict detection runs over the EFFECTIVE shortcut of every action:
        the persisted override when one is applied, otherwise the action's
        default shortcut. With no overrides applied the result is identical
        to the default-only detection.

        Returns:
            Dict mapping conflicting shortcut strings to the tuple of actions
            claiming that shortcut. Only shortcuts with 2+ actions are included.
        """
        self._conflicts = {}
        claims: dict[str, list[ActionDefinition]] = {}
        for action in self._actions_by_id.values():
            shortcut = self._overrides.get(action.action_id) or action.default_shortcut
            if shortcut:
                claims.setdefault(shortcut, []).append(action)
        for shortcut, actions in claims.items():
            if len(actions) > 1:
                self._conflicts[shortcut] = tuple(actions)
        return self._conflicts

    def get_conflicts(self) -> dict[str, tuple[ActionDefinition, ...]]:
        """Return the last computed conflict map (after detect_conflicts)."""
        return self._conflicts

    def has_conflicts(self) -> bool:
        """Return True if any conflicts were detected."""
        return len(self._conflicts) > 0

    def get_action_for_shortcut(self, shortcut: str) -> list[ActionDefinition]:
        """Return actions registered for a given shortcut."""
        return list(self._actions_by_shortcut.get(shortcut, []))

    # ------------------------------------------------------------------
    # Phase 11 M4b/M6: keybinding overrides (persisted via R-10 plane)
    # ------------------------------------------------------------------

    def effective_shortcut(self, action_id: str) -> str:
        """Return the action's effective shortcut: override, else default."""
        action = self._actions_by_id.get(action_id)
        if action is None:
            raise KeyError(f"unknown action: {action_id}")
        return self._overrides.get(action_id) or action.default_shortcut

    def apply_override(self, action_id: str, shortcut: str) -> bool:
        """Apply one keybinding override and report whether it conflicts.

        An empty ``shortcut`` clears the override, restoring the action's
        default shortcut. Conflict detection follows the sealed design
        semantics: the new override conflicts when any OTHER action already
        claims the same effective chord; conflicts are reported, never
        silently resolved.

        Args:
            action_id: The registered action to override.
            shortcut: The new chord (e.g. "Ctrl+Shift+G"), or "" to reset.

        Returns:
            True when the applied override conflicts with another action.

        Raises:
            KeyError: If the action id is not registered.
        """
        action = self._actions_by_id.get(action_id)
        if action is None:
            raise KeyError(f"unknown action: {action_id}")
        normalized = (shortcut or "").strip()
        if normalized:
            self._overrides[action_id] = normalized
        else:
            self._overrides.pop(action_id, None)
        conflicts = self.detect_conflicts()
        if not normalized:
            return False
        return any(item.action_id == action_id for item in conflicts.get(normalized, ()))

    def override_for(self, action_id: str) -> str:
        """Return the stored override chord for an action, or "" if none."""
        return self._overrides.get(action_id, "")

    def reset_overrides(self) -> None:
        """Clear every override, restoring every action's default shortcut."""
        self._overrides.clear()
        self.detect_conflicts()
