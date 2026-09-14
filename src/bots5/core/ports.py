from __future__ import annotations

from datetime import datetime
from typing import ContextManager, Protocol

from bots5.domain.models import Attachment, Chat, GenerationAttempt, Message, WorkspaceWindowState
from bots5.domain.search import (
    SearchFilters,
    SearchNavigation,
    SearchPage,
    SearchResult,
    SearchStatus,
)


class AppStateStore(Protocol):
    def command_admission(self, *, independent: bool = False) -> ContextManager[None]:
        """Acquire or join one complete forward-effect grant."""
        ...

    def event_admission(self, *, independent: bool = False) -> ContextManager[None]:
        """Acquire or join the grant used by the bound event producer."""
        ...

    def issued_event_effect(self) -> ContextManager[None]:
        """Retain already-issued producer delivery until it settles."""
        ...

    def assert_admitting(self) -> None:
        """Raise when the durable store has been poisoned or closed."""
        ...

    def ingest_attachment(self, source, *, filename: str | None = None) -> Attachment:
        ...

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        ...

    def list_attachments(self) -> tuple[Attachment, ...]:
        ...

    def delete_attachment(self, attachment_id: str) -> None:
        ...

    def create_chat(self, chat: Chat) -> None:
        ...

    def archive_chat(
        self,
        chat_id: str,
        archived_at: datetime | None,
        *,
        expected_revision: int | None = None,
    ) -> Chat:
        ...

    def list_chats(self) -> tuple[Chat, ...]:
        ...

    def get_chat(self, chat_id: str) -> Chat | None:
        ...

    def list_messages(self, chat_id: str) -> tuple[Message, ...]:
        ...

    def list_branch_messages(
        self,
        chat_id: str,
        leaf_message_id: str | None = None,
    ) -> tuple[Message, ...]:
        ...

    def list_revisions(self, chat_id: str, lineage_id: str) -> tuple[Message, ...]:
        ...

    def list_generation_attempts(self, chat_id: str) -> tuple[GenerationAttempt, ...]:
        ...

    def list_active_generation_attempts(
        self,
        chat_id: str | None = None,
    ) -> tuple[GenerationAttempt, ...]:
        ...

    def get_generation_attempt(self, attempt_id: str) -> GenerationAttempt | None:
        ...

    def get_message(self, message_id: str) -> Message | None:
        ...

    def list_message_attachments(self, message_id: str) -> tuple[Attachment, ...]:
        ...

    def list_attempt_attachments(self, attempt_id: str) -> tuple[Attachment, ...]:
        ...

    def list_message_attachment_metadata(self, message_id: str) -> tuple[Attachment, ...]:
        """Return durable attachment metadata without opening payload bytes."""
        ...

    def list_attempt_attachment_metadata(self, attempt_id: str) -> tuple[Attachment, ...]:
        """Return durable attempt-attachment metadata without opening payload bytes."""
        ...

    def next_message_sequence(self, chat_id: str) -> int:
        ...

    def persist_generation_start(
        self,
        chat: Chat,
        user_message: Message,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
    ) -> None:
        ...

    def persist_regeneration_start(
        self,
        chat: Chat,
        assistant_message: Message,
        attempt: GenerationAttempt,
        *,
        expected_chat_revision: int | None = None,
    ) -> None:
        ...

    def update_streaming_message(self, message: Message) -> None:
        ...

    def finalize_generation(
        self,
        message: Message,
        attempt: GenerationAttempt,
    ) -> None:
        ...

    def update_attempt(self, attempt: GenerationAttempt) -> None:
        ...

    def reconcile_interrupted_generations(self, now) -> None:
        ...

    def search_status(self) -> SearchStatus:
        ...

    def search(
        self,
        query: str,
        *,
        filters: SearchFilters = SearchFilters(),
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        ...

    def rebuild_search_index(self) -> SearchStatus:
        ...

    def diagnose_search_index(self) -> SearchStatus:
        ...

    def resolve_search_result(
        self,
        result: SearchResult,
        *,
        location_index: int = 0,
    ) -> SearchNavigation:
        ...

    def list_workspace_windows(self) -> tuple[WorkspaceWindowState, ...]:
        ...

    def save_workspace_window(self, state: WorkspaceWindowState) -> None:
        ...

    def delete_workspace_window(self, window_id: str) -> None:
        ...

    def close(self) -> None:
        ...
