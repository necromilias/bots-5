from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .models import Chat, Message, MessageRole, MessageState


class SearchDocumentKind(StrEnum):
    CHAT = "chat"
    MESSAGE = "message"
    ATTACHMENT = "attachment"


class SearchBranchState(StrEnum):
    ACTIVE = "active"
    HISTORICAL = "historical"


class SearchIndexCondition(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    REBUILDING = "REBUILDING"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class SearchFilters:
    chat_id: str | None = None
    document_kinds: tuple[SearchDocumentKind, ...] = ()
    roles: tuple[MessageRole, ...] = ()
    message_states: tuple[MessageState, ...] = ()
    backend_ids: tuple[str, ...] = ()
    provider_ids: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    connection_ids: tuple[str, ...] = ()
    model_entry_ids: tuple[str, ...] = ()
    active_branch_only: bool = False
    include_archived: bool = False
    after: datetime | None = None
    before: datetime | None = None

    def __post_init__(self) -> None:
        if self.chat_id == "":
            raise ValueError("search chat id must not be empty")
        if self.after is not None and self.before is not None and self.after > self.before:
            raise ValueError("search time range is inverted")


@dataclass(frozen=True, slots=True)
class SearchLocation:
    chat_id: str
    message_id: str | None
    branch_state: SearchBranchState
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.chat_id:
            raise ValueError("search location chat id must not be empty")
        if self.message_id == "":
            raise ValueError("search location message id must not be empty")


@dataclass(frozen=True, slots=True)
class SearchResult:
    document_key: str
    document_kind: SearchDocumentKind
    document_id: str
    title: str
    snippet: str
    rank: float
    authoritative_at: datetime
    chat_id: str | None = None
    message_id: str | None = None
    lineage_id: str | None = None
    revision: int | None = None
    supersedes_message_id: str | None = None
    role: MessageRole | None = None
    state: MessageState | None = None
    locations: tuple[SearchLocation, ...] = ()
    checkpoint_revision: int = 0
    generation: int = 0
    include_archived: bool = False
    active_branch_only: bool = False

    def __post_init__(self) -> None:
        if not self.document_id or self.document_key != (
            f"{self.document_kind.value}:{self.document_id}"
        ):
            raise ValueError("search document identity is inconsistent")
        if not math.isfinite(self.rank):
            raise ValueError("search rank must be finite")
        if self.checkpoint_revision < 0 or self.generation < 0:
            raise ValueError("search result revision and generation must be nonnegative")
        if self.document_kind is SearchDocumentKind.ATTACHMENT and not self.locations:
            raise ValueError("visible attachment search results require a location")


@dataclass(frozen=True, slots=True)
class SearchStatus:
    condition: SearchIndexCondition
    source_revision: int | None
    checkpoint_revision: int | None
    generation: int | None
    schema_version: int | None
    tokenizer_version: str | None
    detail: str | None = None

    def __post_init__(self) -> None:
        values = (self.source_revision, self.checkpoint_revision, self.generation)
        if any(value is not None and value < 0 for value in values):
            raise ValueError("search status revisions and generation must be nonnegative")

    @property
    def is_searchable(self) -> bool:
        return self.condition is SearchIndexCondition.VALID


@dataclass(frozen=True, slots=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    next_cursor: str | None
    status: SearchStatus

    def __post_init__(self) -> None:
        if self.status.condition is not SearchIndexCondition.VALID:
            raise ValueError("a search page requires a valid index status")
        for result in self.results:
            if (
                result.checkpoint_revision != self.status.checkpoint_revision
                or result.generation != self.status.generation
            ):
                raise ValueError("search result does not match page status")


@dataclass(frozen=True, slots=True)
class SearchNavigation:
    chat: Chat
    messages: tuple[Message, ...]
    focus_message_id: str | None
    historical_leaf_message_id: str | None
    branch_state: SearchBranchState
    archived_at: datetime | None

    @property
    def is_historical(self) -> bool:
        return self.branch_state is SearchBranchState.HISTORICAL
