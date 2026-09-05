from __future__ import annotations

import asyncio
from collections import defaultdict

from PySide6.QtCore import QObject, Signal

from bots5.core.application import BotsApplication
from bots5.core.events import CoreEvent
from bots5.domain.ids import IdFactory, Uuid7Factory
from bots5.domain.models import ChatActivity, WorkspaceWindowState

from .bridge import CoreEventBridge
from .profile import DesktopSessionInfo


_TERMINAL_EVENT_KINDS = frozenset(
    {
        "generation_completed",
        "generation_incomplete",
        "generation_failed",
        "generation_aborted",
    }
)


class DesktopSessionController(QObject):
    """One UI event source and presentation projection over one application core."""

    event_received = Signal(object)
    activity_changed = Signal(str, object)

    def __init__(
        self,
        application: BotsApplication,
        session: DesktopSessionInfo,
        ids: IdFactory | None = None,
    ) -> None:
        super().__init__()
        self.application = application
        self.session = session
        if ids is None:
            ids = getattr(application, "_ids", None) or Uuid7Factory()
        self._ids = ids
        self.bridge = CoreEventBridge(application)
        self.bridge.event_received.connect(self._on_event)
        self._started = False
        self._windows: dict[str, object] = {}
        self._selected_chat_by_window: dict[str, str | None] = {}
        self._window_ordinals: dict[str, int] = {}
        self._active_by_chat: dict[str, set[str]] = defaultdict(set)
        self._background_completion: set[str] = set()
        self._needs_attention: set[str] = set()
        self._closed_event: asyncio.Event | None = None

    @property
    def window_count(self) -> int:
        return len(self._windows)

    def start(self) -> None:
        if not self._started:
            self.bridge.start()
            self._started = True
        if self._closed_event is None:
            self._closed_event = asyncio.Event()
            if not self._windows:
                self._closed_event.set()

    async def load_workspace(self) -> tuple[WorkspaceWindowState, ...]:
        return await self.application.list_workspace_windows()

    def register_window(
        self,
        window: object,
        *,
        window_id: str | None = None,
        ordinal: int | None = None,
    ) -> tuple[str, int]:
        self.start()
        resolved_id = window_id or f"window-{self._ids.new()}"
        resolved_ordinal = (
            max(self._window_ordinals.values(), default=-1) + 1
            if ordinal is None
            else ordinal
        )
        self._windows[resolved_id] = window
        self._selected_chat_by_window.setdefault(resolved_id, None)
        self._window_ordinals[resolved_id] = resolved_ordinal
        if self._closed_event is not None:
            self._closed_event.clear()
        return resolved_id, resolved_ordinal

    def is_last_window(self, window_id: str) -> bool:
        return window_id in self._windows and len(self._windows) == 1

    def set_selected_chat(self, window_id: str, chat_id: str | None) -> None:
        if window_id not in self._windows:
            return
        self._selected_chat_by_window[window_id] = chat_id
        if chat_id is not None:
            self._background_completion.discard(chat_id)
            self._needs_attention.discard(chat_id)
            self.activity_changed.emit(chat_id, self.activity_for(chat_id))

    def activity_for(self, chat_id: str) -> ChatActivity:
        return ChatActivity(
            active_attempt_ids=tuple(sorted(self._active_by_chat.get(chat_id, set()))),
            background_completion=chat_id in self._background_completion,
            needs_attention=chat_id in self._needs_attention,
        )

    def _chat_is_viewed(self, chat_id: str) -> bool:
        return chat_id in self._selected_chat_by_window.values()

    def _on_event(self, event: CoreEvent) -> None:
        if not isinstance(event, CoreEvent):
            return
        chat_id = event.payload.get("chat_id")
        attempt_id = event.payload.get("attempt_id")
        activity_changed = False
        if isinstance(chat_id, str) and isinstance(attempt_id, str):
            if event.kind == "generation_started":
                self._active_by_chat[chat_id].add(attempt_id)
                activity_changed = True
            elif event.kind in _TERMINAL_EVENT_KINDS:
                self._active_by_chat[chat_id].discard(attempt_id)
                if not self._chat_is_viewed(chat_id):
                    if event.kind == "generation_completed":
                        self._background_completion.add(chat_id)
                    else:
                        self._needs_attention.add(chat_id)
                activity_changed = True
        self.event_received.emit(event)
        if isinstance(chat_id, str) and activity_changed:
            self.activity_changed.emit(chat_id, self.activity_for(chat_id))

    async def save_window(
        self,
        *,
        window_id: str,
        geometry: tuple[int, int, int, int] | None,
        selected_chat_id: str | None,
        rail_collapsed: bool,
        restore_open: bool = True,
    ) -> WorkspaceWindowState:
        return await self.application.save_workspace_window(
            window_id=window_id,
            ordinal=self._window_ordinals.get(window_id, 0),
            geometry=geometry,
            selected_chat_id=selected_chat_id,
            rail_collapsed=rail_collapsed,
            restore_open=restore_open,
        )

    async def unregister_window(
        self,
        window_id: str,
        *,
        geometry: tuple[int, int, int, int] | None = None,
        selected_chat_id: str | None = None,
        rail_collapsed: bool = False,
        restore_open: bool = False,
    ) -> None:
        if window_id not in self._windows:
            return
        if restore_open:
            await self.save_window(
                window_id=window_id,
                geometry=geometry,
                selected_chat_id=selected_chat_id,
                rail_collapsed=rail_collapsed,
                restore_open=True,
            )
        else:
            await self.application.delete_workspace_window(window_id)
        self._windows.pop(window_id, None)
        self._selected_chat_by_window.pop(window_id, None)
        self._window_ordinals.pop(window_id, None)
        if not self._windows and self._closed_event is not None:
            self._closed_event.set()

    async def wait_closed(self) -> None:
        self.start()
        assert self._closed_event is not None
        await self._closed_event.wait()

    async def close(self) -> None:
        await self.bridge.stop_async()
        self._started = False
