from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # R-15: SearchFilters imports .models, so the domain window-state model
    # references it only under typing to keep the import graph acyclic.
    from .search import SearchFilters


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageState(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    STREAMING = "streaming"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    TRUNCATED = "truncated"
    ABORTED = "aborted"
    # Phase 11 fork R-11 (lossless message deletion): a deleted message becomes a
    # tombstone that keeps its lineage and sequence, so a transcript can still
    # show that something was removed without losing structural continuity.
    # The Phase 9 `messages_validate_insert` trigger enumerates the allowed
    # states, so this value is owned by the 0014 tombstone revision and by the
    # phase9_schema exact-DDL checker -- it is not a model-only change.
    DELETED = "deleted"


class ChatSort(StrEnum):
    """Alternate sort orders for the chat list.

    RECENT (default): Pins float to top, then by last activity (updated_at DESC, id DESC).
    CREATION: Newest first by creation time (created_at DESC, id DESC).
    TITLE: Case-insensitive ascending title, id DESC tiebreak, locale-free.
    """
    RECENT = "recent"
    CREATION = "creation"
    TITLE = "title"


class AttemptState(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class Chat:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    head_message_id: str | None = None
    revision: int = 0
    archived_at: datetime | None = None
    # Phase 11 M3 (F4/F5): organisation metadata.  A chat is in AT MOST ONE
    # folder (flat, no nesting) and carries at most one floating pin.  Both
    # fields default to "unorganised" so every pre-M3 construction site keeps
    # its exact meaning.
    folder_id: str | None = None
    is_pinned: bool = False

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("chat revision must be nonnegative")


@dataclass(frozen=True, slots=True)
class Folder:
    """Phase 11 M3 (F4): one flat organisation folder."""

    id: str
    name: str
    created_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("folder sequence must be positive")


@dataclass(frozen=True, slots=True)
class ChatDeletionInventory:
    """Phase 11 M3 (F7): the loss inventory for one whole-chat deletion.

    Read-only projection computed BEFORE a deletion is admitted, so the
    deliberate confirmation dialog can state exactly what is lost.
    """

    chat_id: str
    title: str
    message_count: int
    attachment_count: int
    generation_attempt_count: int


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    chat_id: str
    role: MessageRole
    state: MessageState
    content: str
    sequence: int
    created_at: datetime
    parent_id: str | None = None
    lineage_id: str | None = None
    revision: int = 1
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("message revision must be positive")
        if self.lineage_id is None:
            object.__setattr__(self, "lineage_id", self.id)
        if not self.lineage_id:
            raise ValueError("message lineage id must not be empty")
        if self.parent_id == self.id:
            raise ValueError("message cannot parent itself")
        if self.supersedes_id == self.id:
            raise ValueError("message cannot supersede itself")


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    id: str
    chat_id: str
    user_message_id: str
    assistant_message_id: str
    backend_id: str
    model: str
    state: AttemptState
    request_snapshot: str
    started_at: datetime
    ended_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None
    provider_id: str | None = None
    returned_model: str | None = None
    request_id: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None
    remote_outcome_unknown: bool | None = None
    connection_id: str | None = None
    model_entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentBlob:
    digest: str
    size: int
    state: str
    gc_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Attachment:
    id: str
    blob_digest: str
    filename: str
    source_kind: str
    source_name: str
    text_representation_id: str | None
    text_digest: str | None
    ineligibility_reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatActivity:
    """Session-derived activity for one chat; never persisted as domain truth."""

    active_attempt_ids: tuple[str, ...] = ()
    background_completion: bool = False
    needs_attention: bool = False

    @property
    def has_running(self) -> bool:
        return bool(self.active_attempt_ids)


@dataclass(frozen=True, slots=True)
class WorkspaceWindowState:
    """Minimal restorable presentation state for one native window."""

    window_id: str
    ordinal: int
    geometry: tuple[int, int, int, int] | None
    selected_chat_id: str | None
    rail_collapsed: bool
    restore_open: bool
    updated_at: datetime
    inspector_open: bool = False
    inspector_message_id: str | None = None
    inspector_leaf_message_id: str | None = None
    # Phase 11 M4b: restored presentation plane (0016_phase11_workspace_state).
    maximized: bool = False
    transcript_scroll_position: int | None = None
    # Phase 11 fork R-15: faithful search-state restore plane
    # (0018_phase11_search_state).  The full SearchFilters value object is
    # carried so every one of its thirteen fields survives a restart; the
    # open/closed panel state and the result pagination cursor round-trip
    # alongside it.  All four are advisory presentation state and default to
    # "nothing restored".
    search_open: bool = False
    search_query: str | None = None
    search_filters: SearchFilters | None = None
    search_cursor: str | None = None

    def __post_init__(self) -> None:
        if not self.window_id:
            raise ValueError("workspace window id must not be empty")
        if self.ordinal < 0:
            raise ValueError("workspace window ordinal must be nonnegative")
        if self.transcript_scroll_position is not None and self.transcript_scroll_position < 0:
            raise ValueError("workspace transcript scroll position must be nonnegative")
        if self.geometry is not None:
            if len(self.geometry) != 4 or not all(isinstance(value, int) for value in self.geometry):
                raise ValueError("workspace geometry must contain four integers")
