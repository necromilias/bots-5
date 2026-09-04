from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import wraps

from bots5.domain.clock import Clock, SystemClock
from bots5.domain.ids import IdFactory, Uuid7Factory
from bots5.domain.models import (
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)

from .errors import StateError
from .events import EventBus, EventSubscription
from .execution import ExecutionManager
from .generation import (
    GenerationBackend,
    GenerationCompleted,
    GenerationDelta,
    GenerationFailed,
    GenerationRequest,
)
from .ports import AppStateStore


def _tracked_command(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self._command_scope():
            return await method(self, *args, **kwargs)

    return wrapper


class BotsApplication:
    def __init__(
        self,
        store: AppStateStore,
        events: EventBus,
        backend: GenerationBackend,
        *,
        ids: IdFactory | None = None,
        clock: Clock | None = None,
        execution: ExecutionManager | None = None,
        backend_id: str = "fake",
        model: str = "fake-v0.1",
    ) -> None:
        self._store = store
        self._events = events
        self._backend = backend
        self._ids = ids or Uuid7Factory()
        self._clock = clock or SystemClock()
        self._execution = execution or ExecutionManager()
        self._backend_id = backend_id
        self._model = model
        self._closed = False
        self._pending_generations: dict[str, tuple[Message, GenerationAttempt]] = {}
        self._active_commands = 0
        self._commands_idle = asyncio.Event()
        self._commands_idle.set()
        self._store.reconcile_interrupted_generations(self._clock.now())

    @asynccontextmanager
    async def _command_scope(self):
        self._ensure_open()
        self._active_commands += 1
        self._commands_idle.clear()
        try:
            yield
        finally:
            self._active_commands -= 1
            if self._active_commands == 0:
                self._commands_idle.set()

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateError("application is closed")

    def _track_generation(
        self,
        message: Message,
        attempt: GenerationAttempt,
    ) -> None:
        self._pending_generations[attempt.id] = (message, attempt)

    def _update_tracked_generation(
        self,
        message: Message,
        attempt: GenerationAttempt,
    ) -> None:
        if attempt.id in self._pending_generations:
            self._pending_generations[attempt.id] = (message, attempt)

    async def _publish_after_persistence(self, kind: str, **payload) -> None:
        try:
            await self._events.publish(kind, **payload)
        except StateError:
            if not self._closed:
                raise

    def subscribe(self) -> EventSubscription:
        self._ensure_open()
        return self._events.subscribe()

    @_tracked_command
    async def create_chat(self, title: str = "New chat") -> Chat:
        self._ensure_open()
        now = self._clock.now()
        chat = Chat(id=self._ids.new(), title=title, created_at=now, updated_at=now)
        self._store.create_chat(chat)
        await self._events.publish("chat_created", chat_id=chat.id, title=chat.title)
        self._ensure_open()
        return chat

    @_tracked_command
    async def list_chats(self) -> tuple[Chat, ...]:
        self._ensure_open()
        return self._store.list_chats()

    @_tracked_command
    async def open_chat(
        self,
        chat_id: str,
        *,
        head_message_id: str | None = None,
    ) -> tuple[Chat, tuple[Message, ...]]:
        self._ensure_open()
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        return chat, self._store.list_branch_messages(chat_id, head_message_id)

    @_tracked_command
    async def list_message_history(self, chat_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        if self._store.get_chat(chat_id) is None:
            raise StateError(f"chat not found: {chat_id}")
        return self._store.list_messages(chat_id)

    @_tracked_command
    async def list_revisions(self, chat_id: str, lineage_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        if self._store.get_chat(chat_id) is None:
            raise StateError(f"chat not found: {chat_id}")
        return self._store.list_revisions(chat_id, lineage_id)

    @_tracked_command
    async def list_generation_attempts(self, chat_id: str) -> tuple[GenerationAttempt, ...]:
        self._ensure_open()
        if self._store.get_chat(chat_id) is None:
            raise StateError(f"chat not found: {chat_id}")
        return self._store.list_generation_attempts(chat_id)

    def _active_branch_contains(self, chat_id: str, message_id: str) -> Message:
        branch = self._store.list_branch_messages(chat_id)
        for message in branch:
            if message.id == message_id:
                return message
        raise StateError(f"message is not on the active branch: {message_id}")

    def _request_and_attempt(
        self,
        *,
        chat_id: str,
        user_message: Message,
        assistant_message: Message,
        attempt_id: str,
        now,
    ) -> tuple[GenerationRequest, GenerationAttempt]:
        request = GenerationRequest(
            attempt_id=attempt_id,
            chat_id=chat_id,
            user_message_id=user_message.id,
            backend_id=self._backend_id,
            model=self._model,
            prompt=user_message.content,
        )
        attempt = GenerationAttempt(
            id=attempt_id,
            chat_id=chat_id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            backend_id=self._backend_id,
            model=self._model,
            state=AttemptState.RUNNING,
            request_snapshot=json.dumps(request.model_dump(mode="json"), sort_keys=True),
            started_at=now,
        )
        return request, attempt

    def _new_assistant(
        self,
        *,
        chat_id: str,
        user_message: Message,
        sequence: int,
        now,
        lineage_id: str | None = None,
        revision: int = 1,
        supersedes_id: str | None = None,
    ) -> Message:
        return Message(
            id=self._ids.new(),
            chat_id=chat_id,
            role=MessageRole.ASSISTANT,
            state=MessageState.STREAMING,
            content="",
            sequence=sequence,
            created_at=now,
            parent_id=user_message.id,
            lineage_id=lineage_id or self._ids.new(),
            revision=revision,
            supersedes_id=supersedes_id,
        )

    async def _start_generation(
        self,
        request: GenerationRequest,
        assistant_message: Message,
        attempt: GenerationAttempt,
    ) -> None:
        ready = asyncio.Event()
        task = self._execution.start(
            self._run_generation(request, assistant_message, attempt, ready),
            name=f"bots5-generation-{attempt.id}",
        )
        ready_wait = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_wait, task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done and not ready.is_set():
                if task.cancelled():
                    return
                task.result()
        finally:
            if not ready_wait.done():
                ready_wait.cancel()
            await asyncio.gather(ready_wait, return_exceptions=True)

    @_tracked_command
    async def send_message(self, chat_id: str, text: str) -> GenerationAttempt:
        self._ensure_open()
        if not text.strip():
            raise StateError("message text must not be empty")
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")

        now = self._clock.now()
        user_message = Message(
            id=self._ids.new(),
            chat_id=chat_id,
            role=MessageRole.USER,
            state=MessageState.SENT,
            content=text,
            sequence=self._store.next_message_sequence(chat_id),
            created_at=now,
            parent_id=chat.head_message_id,
            lineage_id=self._ids.new(),
        )
        assistant_message = self._new_assistant(
            chat_id=chat_id,
            user_message=user_message,
            sequence=user_message.sequence + 1,
            now=now,
        )
        request, attempt = self._request_and_attempt(
            chat_id=chat_id,
            user_message=user_message,
            assistant_message=assistant_message,
            attempt_id=self._ids.new(),
            now=now,
        )
        self._store.persist_generation_start(
            replace(
                chat,
                updated_at=now,
                head_message_id=assistant_message.id,
                revision=chat.revision + 1,
            ),
            user_message,
            assistant_message,
            attempt,
            expected_chat_revision=chat.revision,
        )
        self._track_generation(assistant_message, attempt)
        await self._events.publish(
            "message_sent",
            chat_id=chat_id,
            message_id=user_message.id,
            attempt_id=attempt.id,
        )
        await self._events.publish(
            "generation_started",
            chat_id=chat_id,
            message_id=assistant_message.id,
            attempt_id=attempt.id,
            lineage_id=assistant_message.lineage_id,
            revision=assistant_message.revision,
        )
        self._ensure_open()
        await self._start_generation(request, assistant_message, attempt)
        self._ensure_open()
        return attempt

    @_tracked_command
    async def edit_message(self, chat_id: str, message_id: str, text: str) -> GenerationAttempt:
        self._ensure_open()
        if not text.strip():
            raise StateError("message text must not be empty")
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        target = self._active_branch_contains(chat_id, message_id)
        if target.role != MessageRole.USER or target.state != MessageState.SENT:
            raise StateError("only sent user messages can be edited")
        lineage_id = target.lineage_id or target.id
        revision = len(self._store.list_revisions(chat_id, lineage_id)) + 1
        now = self._clock.now()
        user_message = Message(
            id=self._ids.new(),
            chat_id=chat_id,
            role=MessageRole.USER,
            state=MessageState.SENT,
            content=text,
            sequence=self._store.next_message_sequence(chat_id),
            created_at=now,
            parent_id=target.parent_id,
            lineage_id=lineage_id,
            revision=revision,
            supersedes_id=target.id,
        )
        assistant_message = self._new_assistant(
            chat_id=chat_id,
            user_message=user_message,
            sequence=user_message.sequence + 1,
            now=now,
        )
        request, attempt = self._request_and_attempt(
            chat_id=chat_id,
            user_message=user_message,
            assistant_message=assistant_message,
            attempt_id=self._ids.new(),
            now=now,
        )
        updated_chat = replace(
            chat,
            updated_at=now,
            head_message_id=assistant_message.id,
            revision=chat.revision + 1,
        )
        self._store.persist_generation_start(
            updated_chat,
            user_message,
            assistant_message,
            attempt,
            expected_chat_revision=chat.revision,
        )
        self._track_generation(assistant_message, attempt)
        await self._events.publish(
            "message_revision_created",
            chat_id=chat_id,
            message_id=user_message.id,
            lineage_id=user_message.lineage_id,
            revision=user_message.revision,
            supersedes_id=user_message.supersedes_id,
            reason="edit",
        )
        await self._events.publish(
            "branch_head_changed",
            chat_id=chat_id,
            previous_head_message_id=chat.head_message_id,
            head_message_id=assistant_message.id,
            chat_revision=updated_chat.revision,
        )
        await self._events.publish(
            "generation_started",
            chat_id=chat_id,
            message_id=assistant_message.id,
            attempt_id=attempt.id,
            lineage_id=assistant_message.lineage_id,
            revision=assistant_message.revision,
        )
        self._ensure_open()
        await self._start_generation(request, assistant_message, attempt)
        self._ensure_open()
        return attempt

    @_tracked_command
    async def regenerate_message(self, chat_id: str, message_id: str) -> GenerationAttempt:
        self._ensure_open()
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        target = self._active_branch_contains(chat_id, message_id)
        if target.role != MessageRole.ASSISTANT or target.state == MessageState.STREAMING:
            raise StateError("only terminal assistant messages can be regenerated")
        if target.parent_id is None:
            raise StateError("assistant message has no user parent")
        user_message = self._store.get_message(target.parent_id)
        if user_message is None or user_message.role != MessageRole.USER:
            raise StateError("assistant message has an invalid user parent")
        lineage_id = target.lineage_id or target.id
        revision = len(self._store.list_revisions(chat_id, lineage_id)) + 1
        now = self._clock.now()
        assistant_message = self._new_assistant(
            chat_id=chat_id,
            user_message=user_message,
            sequence=self._store.next_message_sequence(chat_id),
            now=now,
            lineage_id=lineage_id,
            revision=revision,
            supersedes_id=target.id,
        )
        request, attempt = self._request_and_attempt(
            chat_id=chat_id,
            user_message=user_message,
            assistant_message=assistant_message,
            attempt_id=self._ids.new(),
            now=now,
        )
        updated_chat = replace(
            chat,
            updated_at=now,
            head_message_id=assistant_message.id,
            revision=chat.revision + 1,
        )
        self._store.persist_regeneration_start(
            updated_chat,
            assistant_message,
            attempt,
            expected_chat_revision=chat.revision,
        )
        self._track_generation(assistant_message, attempt)
        await self._events.publish(
            "message_revision_created",
            chat_id=chat_id,
            message_id=assistant_message.id,
            lineage_id=assistant_message.lineage_id,
            revision=assistant_message.revision,
            supersedes_id=assistant_message.supersedes_id,
            reason="regenerate",
        )
        await self._events.publish(
            "branch_head_changed",
            chat_id=chat_id,
            previous_head_message_id=chat.head_message_id,
            head_message_id=assistant_message.id,
            chat_revision=updated_chat.revision,
        )
        await self._events.publish(
            "generation_started",
            chat_id=chat_id,
            message_id=assistant_message.id,
            attempt_id=attempt.id,
            lineage_id=assistant_message.lineage_id,
            revision=assistant_message.revision,
        )
        self._ensure_open()
        await self._start_generation(request, assistant_message, attempt)
        self._ensure_open()
        return attempt

    async def _run_generation(
        self,
        request: GenerationRequest,
        assistant_message: Message,
        attempt: GenerationAttempt,
        ready: asyncio.Event,
    ) -> None:
        message = assistant_message
        current_attempt = attempt
        terminal_persisted = False
        try:
            ready.set()
            await asyncio.sleep(0)
            terminal = False
            async for event in self._backend.stream(request):
                if event.attempt_id != attempt.id:
                    raise StateError("generation backend returned an event for another attempt")
                if isinstance(event, GenerationDelta):
                    message = replace(
                        message,
                        state=MessageState.STREAMING,
                        content=message.content + event.text,
                    )
                    self._store.update_streaming_message(message)
                    self._update_tracked_generation(message, current_attempt)
                    await self._publish_after_persistence(
                        "message_delta",
                        chat_id=message.chat_id,
                        message_id=message.id,
                        attempt_id=attempt.id,
                        text=event.text,
                    )
                elif isinstance(event, GenerationCompleted):
                    terminal = True
                    now = self._clock.now()
                    if event.finish_reason == "stop":
                        message = replace(message, state=MessageState.COMPLETE)
                        current_attempt = replace(
                            current_attempt,
                            state=AttemptState.COMPLETE,
                            ended_at=now,
                        )
                        event_kind = "generation_completed"
                    else:
                        message = replace(message, state=MessageState.TRUNCATED)
                        current_attempt = replace(
                            current_attempt,
                            state=AttemptState.INCOMPLETE,
                            ended_at=now,
                            error_type="non_stop_finish",
                            error_message=f"generation ended with finish_reason={event.finish_reason}",
                        )
                        event_kind = "generation_incomplete"
                    self._store.finalize_generation(message, current_attempt)
                    terminal_persisted = True
                    await self._publish_after_persistence(
                        event_kind,
                        chat_id=message.chat_id,
                        message_id=message.id,
                        attempt_id=attempt.id,
                        finish_reason=event.finish_reason,
                    )
                    break
                elif isinstance(event, GenerationFailed):
                    terminal = True
                    now = self._clock.now()
                    message = replace(message, state=MessageState.FAILED)
                    current_attempt = replace(
                        current_attempt,
                        state=AttemptState.FAILED,
                        ended_at=now,
                        error_type=event.error_type,
                        error_message=event.error_message,
                    )
                    self._store.finalize_generation(message, current_attempt)
                    terminal_persisted = True
                    await self._publish_after_persistence(
                        "generation_failed",
                        chat_id=message.chat_id,
                        message_id=message.id,
                        attempt_id=attempt.id,
                        error_type=event.error_type,
                        error_message=event.error_message,
                    )
                    break

            if not terminal:
                now = self._clock.now()
                message = replace(message, state=MessageState.INCOMPLETE)
                current_attempt = replace(
                    current_attempt,
                    state=AttemptState.INCOMPLETE,
                    ended_at=now,
                    error_type="missing_terminal_event",
                    error_message="generation stream ended without a terminal event",
                )
                self._store.finalize_generation(message, current_attempt)
                terminal_persisted = True
                await self._publish_after_persistence(
                    "generation_incomplete",
                    chat_id=message.chat_id,
                    message_id=message.id,
                    attempt_id=attempt.id,
                )
        except asyncio.CancelledError:
            if terminal_persisted:
                raise
            now = self._clock.now()
            message = replace(message, state=MessageState.ABORTED)
            current_attempt = replace(
                current_attempt,
                state=AttemptState.ABORTED,
                ended_at=now,
                error_type="aborted",
                error_message="generation was cancelled",
            )
            self._store.finalize_generation(message, current_attempt)
            terminal_persisted = True
            await self._publish_after_persistence(
                "generation_aborted",
                chat_id=message.chat_id,
                message_id=message.id,
                attempt_id=attempt.id,
            )
            raise
        except Exception as exc:
            if terminal_persisted:
                raise
            now = self._clock.now()
            message = replace(message, state=MessageState.FAILED)
            current_attempt = replace(
                current_attempt,
                state=AttemptState.FAILED,
                ended_at=now,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            self._store.finalize_generation(message, current_attempt)
            terminal_persisted = True
            await self._publish_after_persistence(
                "generation_failed",
                chat_id=message.chat_id,
                message_id=message.id,
                attempt_id=attempt.id,
                error_type=current_attempt.error_type,
                error_message=current_attempt.error_message,
            )
        finally:
            self._pending_generations.pop(attempt.id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.close()
        shutdown_failure: BaseException | None = None
        try:
            await self._execution.shutdown()
        except BaseException as exc:
            shutdown_failure = exc

        await self._commands_idle.wait()

        reconciliation_failure: BaseException | None = None
        for attempt_id, (message, attempt) in tuple(self._pending_generations.items()):
            try:
                stored_message = self._store.get_message(message.id)
                stored_attempt = next(
                    (
                        item
                        for item in self._store.list_generation_attempts(message.chat_id)
                        if item.id == attempt_id
                    ),
                    None,
                )
                if (
                    stored_message is not None
                    and stored_attempt is not None
                    and stored_message.state == MessageState.STREAMING
                    and stored_attempt.state == AttemptState.RUNNING
                ):
                    now = self._clock.now()
                    self._store.finalize_generation(
                        replace(stored_message, state=MessageState.ABORTED),
                        replace(
                            stored_attempt,
                            state=AttemptState.ABORTED,
                            ended_at=now,
                            error_type="aborted",
                            error_message="generation was cancelled during shutdown",
                        ),
                    )
            except BaseException as exc:
                if reconciliation_failure is None:
                    reconciliation_failure = exc
            finally:
                self._pending_generations.pop(attempt_id, None)

        try:
            self._store.close()
        finally:
            if shutdown_failure is not None:
                raise shutdown_failure
            if reconciliation_failure is not None:
                raise reconciliation_failure
