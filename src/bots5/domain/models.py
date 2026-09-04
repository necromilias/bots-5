from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("chat revision must be nonnegative")


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
