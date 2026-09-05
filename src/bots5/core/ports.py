from __future__ import annotations

from typing import Protocol

from bots5.domain.models import Chat, GenerationAttempt, Message, WorkspaceWindowState


class AppStateStore(Protocol):
    def create_chat(self, chat: Chat) -> None:
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

    def list_workspace_windows(self) -> tuple[WorkspaceWindowState, ...]:
        ...

    def save_workspace_window(self, state: WorkspaceWindowState) -> None:
        ...

    def delete_workspace_window(self, window_id: str) -> None:
        ...

    def close(self) -> None:
        ...
