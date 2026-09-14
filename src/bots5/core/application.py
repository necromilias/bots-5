from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import Context
from dataclasses import dataclass, replace
from enum import Enum
from functools import wraps

from bots5.domain.clock import Clock, SystemClock
from bots5.domain.ids import IdFactory, Uuid7Factory
from bots5.domain.models import (
    AttemptState,
    ChatActivity,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
    WorkspaceWindowState,
)
from bots5.domain.provider import (
    BackendType,
    CapabilityFact,
    CapabilityKey,
    CapabilitySource,
    CapabilityState,
    CatalogueRefreshFailureClass,
)
from bots5.domain.search import (
    SearchFilters,
    SearchNavigation,
    SearchPage,
    SearchResult,
    SearchStatus,
)
from bots5.errors import ProviderError

from .errors import AuthorityError, RevisionConflict, StateError
from .events import EventBus, EventSubscription
from .execution import ExecutionManager
from .generation import (
    GenerationBackend,
    GenerationCompleted,
    GenerationDelta,
    GenerationDispatched,
    GenerationFailed,
    GenerationMetadata,
    GenerationRequest,
)
from .context import ContextBuilder, ContextPlan, ContextSource
from .inspection import InspectionProjection, build_inspection_projection
from .ports import AppStateStore
from .provider_configuration import ProviderConfiguration
from .secrets import SecretStoreError, reject_secret_material, sanitize_secret_error
from bots5.providers.discovery import ModelDiscoveryError


class GenerationTimeout(Exception):
    """A B.O.T.S.-owned generation deadline expired after request preparation."""


class ApplicationCloseState(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class GenerationMode(str, Enum):
    CONFIGURED = "CONFIGURED"
    LEGACY_PHASE3_LOCAL_OPENAI = "LEGACY_PHASE3_LOCAL_OPENAI"
    LEGACY_CORE_COMPATIBILITY = "LEGACY_CORE_COMPATIBILITY"


@dataclass(frozen=True, slots=True)
class TerminalCloseError:
    stage: str
    code: str
    public_kind: str
    message: str


@dataclass(frozen=True, slots=True)
class TerminalCloseResult:
    errors: tuple[TerminalCloseError, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.errors


_CLOSE_PRECEDENCE = {
    "store": 0,
    "execution": 1,
    "reconciliation": 2,
    "events": 3,
}


def _close_error(stage: str, *, authority: bool = False) -> TerminalCloseError:
    # Fixed text deliberately excludes exception type, args, custom payload,
    # cause/context, traceback locals, and unknown secret-shaped material.
    return TerminalCloseError(
        stage=stage,
        code=f"close_{stage}_failed",
        public_kind="authority" if authority else "state",
        message=f"application close failed during {stage}",
    )


_FAKE_TRUSTED_CAPABILITIES = (
    (CapabilityKey.STREAMING.value, None),
    (CapabilityKey.TEMPERATURE.value, None),
    (CapabilityKey.MAX_OUTPUT_TOKENS.value, 16384),
    (CapabilityKey.CONTEXT_TOKENS.value, 32768),
    (CapabilityKey.REASONING_NONE.value, None),
    (CapabilityKey.REQUEST_ID.value, None),
    (CapabilityKey.RETURNED_MODEL.value, None),
)


def _fake_capability_facts(model_entry_id: str, observed_at):
    return tuple(
        CapabilityFact(
            model_entry_id,
            key,
            CapabilityState.SUPPORTED,
            CapabilitySource.TRUSTED_REGISTRY,
            source_revision=1,
            value=value,
            provenance={"field": "built-in fake backend"},
            observed_at=observed_at,
        )
        for key, value in _FAKE_TRUSTED_CAPABILITIES
    )


def _abandon_task(task: asyncio.Task[object]) -> None:
    """Cancel a child without allowing a cancellation-resistant backend to hold us up."""
    if not task.done():
        task.cancel()

    def consume(completed: asyncio.Task[object]) -> None:
        try:
            completed.result()
        except BaseException:
            pass

    task.add_done_callback(consume)


def _tracked_command(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self._command_scope():
            return await method(self, *args, **kwargs)

    return wrapper


def _merge_attempt_metadata(attempt: GenerationAttempt, event) -> GenerationAttempt:
    return replace(
        attempt,
        returned_model=event.returned_model or attempt.returned_model,
        request_id=event.request_id or attempt.request_id,
        prompt_tokens=(
            event.prompt_tokens
            if event.prompt_tokens is not None
            else attempt.prompt_tokens
        ),
        completion_tokens=(
            event.completion_tokens
            if event.completion_tokens is not None
            else attempt.completion_tokens
        ),
        reasoning_tokens=(
            event.reasoning_tokens
            if event.reasoning_tokens is not None
            else attempt.reasoning_tokens
        ),
        total_tokens=(
            event.total_tokens
            if event.total_tokens is not None
            else attempt.total_tokens
        ),
        known_cost_usd=(
            event.known_cost_usd
            if event.known_cost_usd is not None
            else attempt.known_cost_usd
        ),
    )


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
        provider_id: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        configuration: ProviderConfiguration | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._backend = backend
        self._ids = ids or Uuid7Factory()
        self._clock = clock or SystemClock()
        self._execution = execution or ExecutionManager()
        self._backend_id = backend_id
        self._model = model
        self._provider_id = provider_id
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._configuration = configuration
        if generation_mode is None:
            generation_mode = (
                GenerationMode.CONFIGURED
                if configuration is not None
                else GenerationMode.LEGACY_CORE_COMPATIBILITY
            )
        try:
            self._generation_mode = GenerationMode(generation_mode)
        except ValueError as exc:
            raise StateError("unknown generation mode") from exc
        if self._generation_mode is GenerationMode.CONFIGURED:
            if configuration is None:
                raise StateError("configured generation mode requires provider configuration")
        elif configuration is not None:
            raise StateError("legacy generation mode cannot carry provider configuration")
        elif self._generation_mode is GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI:
            if (
                provider_id != "local_openai"
                or backend_id != "openai_compatible_http"
                or not base_url
                or getattr(backend, "provider_id", None) != "local_openai"
                or getattr(backend, "base_url", None) != base_url
                or not hasattr(backend, "api_key_env")
                or getattr(backend, "api_key_env", None) != api_key_env
            ):
                raise StateError("legacy local generation mode binding is inconsistent")
        elif (
            backend_id == "openai_compatible_http"
            or backend.__class__.__module__
            == "bots5.infrastructure.generation.openai_compatible"
            or any(value is not None for value in (provider_id, base_url, api_key_env))
            or hasattr(backend, "provider_id")
            or hasattr(backend, "base_url")
            or hasattr(backend, "api_key_env")
        ):
            raise StateError(
                "core compatibility mode cannot carry a real provider or HTTP backend"
            )
        self._close_state = ApplicationCloseState.OPEN
        self._close_loop: asyncio.AbstractEventLoop | None = None
        self._close_task: asyncio.Task[TerminalCloseResult] | None = None
        self._close_result: TerminalCloseResult | None = None
        self._pending_generations: dict[str, tuple[Message, GenerationAttempt]] = {}
        self._generation_tasks: dict[str, asyncio.Task[None]] = {}
        self._generation_terminal_events: dict[str, asyncio.Event] = {}
        self._cancel_requested: set[str] = set()
        self._pending_attachment_ids: dict[str, tuple[str, ...]] = {}
        self._active_commands = 0
        self._commands_idle = asyncio.Event()
        self._commands_idle.set()
        self._events.bind_effect_authority(
            self._store.event_admission,
            self._store.issued_event_effect,
        )
        self._store.reconcile_interrupted_generations(self._clock.now())

    @property
    def generation_mode(self) -> GenerationMode:
        return self._generation_mode

    @property
    def phase6_enabled(self) -> bool:
        return self._generation_mode is GenerationMode.CONFIGURED and bool(
            self._configuration and self._configuration.phase6_enabled
        )

    @asynccontextmanager
    async def _command_scope(self):
        if self._close_state is not ApplicationCloseState.OPEN:
            raise StateError("application is closed")
        with self._application_effect_scope():
            self._ensure_open()
            self._active_commands += 1
            self._commands_idle.clear()
            try:
                yield
            finally:
                self._active_commands -= 1
                if self._active_commands == 0:
                    self._commands_idle.set()

    @contextmanager
    def _application_effect_scope(self, *, independent: bool = False):
        """Order one application mutation/publication effect before poison."""
        with self._store.command_admission(independent=independent):
            yield

    def _ensure_open(self) -> None:
        if self._close_state is not ApplicationCloseState.OPEN:
            raise StateError("application is closed")
        self._store.assert_admitting()

    @property
    def _closed(self) -> bool:
        """Compatibility predicate; close admission is owned by the state machine."""
        return self._close_state is not ApplicationCloseState.OPEN

    def _track_generation(
        self,
        message: Message,
        attempt: GenerationAttempt,
    ) -> None:
        self._pending_generations[attempt.id] = (message, attempt)
        self._generation_terminal_events[attempt.id] = asyncio.Event()

    def _mark_terminal_persisted(self, attempt_id: str) -> None:
        event = self._generation_terminal_events.get(attempt_id)
        if event is not None:
            event.set()

    def _finalize_generation(self, message: Message, attempt: GenerationAttempt) -> None:
        self._store.finalize_generation(message, attempt)
        self._mark_terminal_persisted(attempt.id)

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
            if self._close_state is ApplicationCloseState.OPEN:
                raise

    def subscribe(self) -> EventSubscription:
        with self._application_effect_scope():
            self._ensure_open()
            return self._events.subscribe()

    def has_active_generations(self) -> bool:
        return bool(self._pending_generations) or bool(
            self._store.list_active_generation_attempts()
        )

    def active_attempt_ids(self, chat_id: str | None = None) -> tuple[str, ...]:
        pending = tuple(
            attempt_id
            for attempt_id, (_, attempt) in self._pending_generations.items()
            if chat_id is None or attempt.chat_id == chat_id
        )
        persisted = tuple(
            attempt.id
            for attempt in self._store.list_active_generation_attempts(chat_id)
            if attempt.id not in pending
        )
        return pending + persisted

    def _ensure_chat_has_no_active_generation(self, chat_id: str) -> None:
        active = self._store.list_active_generation_attempts(chat_id)
        if active:
            raise StateError(
                "chat already has an active generation: "
                f"{chat_id} ({active[0].id})"
            )

    @_tracked_command
    async def chat_activity(self, chat_id: str) -> ChatActivity:
        self._ensure_open()
        if self._store.get_chat(chat_id) is None:
            raise StateError(f"chat not found: {chat_id}")
        return ChatActivity(
            active_attempt_ids=tuple(
                attempt.id for attempt in self._store.list_active_generation_attempts(chat_id)
            )
        )

    @_tracked_command
    async def list_workspace_windows(self) -> tuple[WorkspaceWindowState, ...]:
        self._ensure_open()
        return self._store.list_workspace_windows()

    @_tracked_command
    async def save_workspace_window(
        self,
        *,
        window_id: str,
        ordinal: int,
        geometry: tuple[int, int, int, int] | None,
        selected_chat_id: str | None,
        rail_collapsed: bool,
        restore_open: bool = True,
        inspector_open: bool = False,
        inspector_message_id: str | None = None,
        inspector_leaf_message_id: str | None = None,
    ) -> WorkspaceWindowState:
        self._ensure_open()
        state = WorkspaceWindowState(
            window_id=window_id,
            ordinal=ordinal,
            geometry=geometry,
            selected_chat_id=selected_chat_id,
            rail_collapsed=rail_collapsed,
            restore_open=restore_open,
            updated_at=self._clock.now(),
            inspector_open=inspector_open,
            inspector_message_id=inspector_message_id,
            inspector_leaf_message_id=inspector_leaf_message_id,
        )
        self._store.save_workspace_window(state)
        return state

    @_tracked_command
    async def delete_workspace_window(self, window_id: str) -> None:
        self._ensure_open()
        self._store.delete_workspace_window(window_id)

    @_tracked_command
    async def create_chat(self, title: str = "New chat") -> Chat:
        self._ensure_open()
        now = self._clock.now()
        chat = Chat(id=self._ids.new(), title=title, created_at=now, updated_at=now)
        self._store.create_chat(chat)
        if self._configuration is not None:
            _, default_model_entry_id, _ = self._store.get_application_generation_config()
            if default_model_entry_id is not None:
                self._store.set_chat_model_selection(chat.id, default_model_entry_id)
        await self._events.publish("chat_created", chat_id=chat.id, title=chat.title)
        self._ensure_open()
        return chat

    @_tracked_command
    async def archive_chat(self, chat_id: str) -> Chat:
        self._ensure_open()
        chat = self._store.archive_chat(chat_id, self._clock.now())
        await self._events.publish(
            "chat_archived",
            chat_id=chat.id,
            archived_at=chat.archived_at,
        )
        self._ensure_open()
        return chat

    @_tracked_command
    async def unarchive_chat(self, chat_id: str) -> Chat:
        self._ensure_open()
        chat = self._store.archive_chat(chat_id, None)
        await self._events.publish(
            "chat_unarchived",
            chat_id=chat.id,
            archived_at=None,
        )
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
    async def inspect_chat(
        self,
        chat_id: str,
        *,
        message_id: str | None = None,
        historical_leaf_message_id: str | None = None,
    ) -> InspectionProjection:
        """Return the core-owned, safe display projection for Details."""
        self._ensure_open()
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        message = None if message_id is None else self._store.get_message(message_id)
        if message is not None and message.chat_id != chat_id:
            raise StateError("inspection message is not in the selected chat")
        branch: tuple[Message, ...] = ()
        if historical_leaf_message_id is not None:
            leaf = self._store.get_message(historical_leaf_message_id)
            if leaf is None or leaf.chat_id != chat_id:
                historical_leaf_message_id = None
            else:
                branch = self._store.list_branch_messages(
                    chat_id, historical_leaf_message_id
                )
                if not branch or branch[-1].id != historical_leaf_message_id:
                    historical_leaf_message_id = None
        if historical_leaf_message_id is None:
            # A missing or stale historical leaf falls back to the
            # authoritative active path.  The selected identity must be
            # coherent with that path as well; it cannot retain a different
            # historical branch under an active-path label.
            branch = self._store.list_branch_messages(chat_id)
        branch_ids = frozenset(item.id for item in branch)
        if message is not None and message.id not in branch_ids:
            if historical_leaf_message_id is not None:
                # Saved UI identities are advisory.  An incoherent historical
                # leaf cannot label another branch's message or attempt.  Fall
                # back to the authoritative active path, then accept the
                # selected message only if it belongs to that path.
                historical_leaf_message_id = None
                branch = self._store.list_branch_messages(chat_id)
                branch_ids = frozenset(item.id for item in branch)
            if message.id not in branch_ids:
                message = None
        attempts = self._store.list_generation_attempts(chat_id)
        if message is not None:
            # A selected user can be shared by regenerated assistant siblings.
            # Its attempts are historical facts only when their resulting
            # assistant belongs to the branch that resolved this inspection.
            # Keep the unfiltered chat-level history when no message is
            # selected.
            attempts = tuple(
                attempt
                for attempt in attempts
                if (
                    attempt.assistant_message_id in branch_ids
                    and (
                        attempt.user_message_id == message.id
                        or attempt.assistant_message_id == message.id
                    )
                )
            )
        user_content = {
            attempt.id: (
                self._store.get_message(attempt.user_message_id).content
                if self._store.get_message(attempt.user_message_id) is not None
                else None
            )
            for attempt in attempts
        }
        revisions = () if message is None else self._store.list_revisions(
            chat_id, message.lineage_id or message.id
        )
        return build_inspection_projection(
            chat=chat, message=message,
            historical_leaf_message_id=historical_leaf_message_id,
            revision_count=len(revisions), attempts=attempts,
            user_content_by_attempt=user_content,
            message_attachments=()
            if message is None else self._store.list_message_attachment_metadata(message.id),
            attempt_attachments={
                attempt.id: self._store.list_attempt_attachment_metadata(attempt.id)
                for attempt in attempts
            },
        )

    @_tracked_command
    async def search_status(self) -> SearchStatus:
        self._ensure_open()
        return self._store.search_status()

    @_tracked_command
    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters = SearchFilters(),
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        self._ensure_open()
        return self._store.search(
            query,
            filters=filters,
            limit=limit,
            cursor=cursor,
        )

    @_tracked_command
    async def rebuild_search_index(self) -> SearchStatus:
        self._ensure_open()
        loop = asyncio.get_running_loop()
        # ``run_in_executor`` does not copy the caller's contextvars. The
        # synchronous store operation therefore acquires its own callee-owned
        # DataRootAuthority grant. Cancellation is deferred until both that
        # forward effect and its completion event have settled, so the worker
        # can never outlive its owning application command.
        worker = loop.run_in_executor(None, self._store.rebuild_search_index)
        cancellation: asyncio.CancelledError | None = None
        owner = asyncio.current_task()

        async def settle_without_detaching(future) -> None:
            nonlocal cancellation
            completed = asyncio.Event()
            future.add_done_callback(lambda _future: completed.set())
            while not future.done():
                try:
                    await completed.wait()
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    if owner is not None:
                        owner.uncancel()

        await settle_without_detaching(worker)
        status = worker.result()
        # A child task inherits context variables unless given an explicit
        # context.  Inheriting this command's executor-owned authority grant
        # would correctly be rejected when EventBus opens its child-task
        # admission scope.  Start publication with no inherited grants; the
        # bus acquires its own grant while this command continues to own and
        # await the forward effect.
        publication = asyncio.create_task(
            self._events.publish(
                "search_index_rebuilt",
                condition=status.condition.value,
                source_revision=status.source_revision,
                checkpoint_revision=status.checkpoint_revision,
                generation=status.generation,
            ),
            context=Context(),
        )
        await settle_without_detaching(publication)
        publication.result()
        self._ensure_open()
        if cancellation is not None:
            raise cancellation
        return status

    @_tracked_command
    async def diagnose_search_index(self) -> SearchStatus:
        self._ensure_open()
        return self._store.diagnose_search_index()

    @_tracked_command
    async def resolve_search_result(
        self,
        result: SearchResult,
        *,
        location_index: int = 0,
    ) -> SearchNavigation:
        self._ensure_open()
        return self._store.resolve_search_result(
            result,
            location_index=location_index,
        )

    @_tracked_command
    async def list_generation_attempts(self, chat_id: str) -> tuple[GenerationAttempt, ...]:
        self._ensure_open()
        if self._store.get_chat(chat_id) is None:
            raise StateError(f"chat not found: {chat_id}")
        return self._store.list_generation_attempts(chat_id)

    @_tracked_command
    async def attach_file(self, source, *, filename: str | None = None):
        """Capture a user file through the core-owned attachment boundary."""
        self._ensure_open()
        attachment = self._store.ingest_attachment(source, filename=filename)
        await self._events.publish(
            "attachment_created",
            attachment_id=attachment.id,
            blob_digest=attachment.blob_digest,
            text_eligible=attachment.text_representation_id is not None,
        )
        return attachment

    @_tracked_command
    async def stage_attachment(self, chat_id: str, attachment_id: str) -> tuple[str, ...]:
        self._ensure_open()
        if self._generation_mode is GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI:
            raise StateError(
                "attachments are unavailable in the Phase 3 local_openai compatibility mode"
            )
        if self._store.get_chat(chat_id) is None:
            raise StateError(f"chat not found: {chat_id}")
        if self._store.get_attachment(attachment_id) is None:
            raise StateError(f"attachment not found: {attachment_id}")
        current = list(self._pending_attachment_ids.get(chat_id, ()))
        if attachment_id not in current:
            current.append(attachment_id)
        self._pending_attachment_ids[chat_id] = tuple(current)
        await self._events.publish(
            "pending_attachments_changed",
            chat_id=chat_id,
            attachment_ids=tuple(current),
        )
        return tuple(current)

    @_tracked_command
    async def unstage_attachment(self, chat_id: str, attachment_id: str) -> tuple[str, ...]:
        self._ensure_open()
        current = tuple(item for item in self._pending_attachment_ids.get(chat_id, ()) if item != attachment_id)
        if current:
            self._pending_attachment_ids[chat_id] = current
        else:
            self._pending_attachment_ids.pop(chat_id, None)
        await self._events.publish(
            "pending_attachments_changed",
            chat_id=chat_id,
            attachment_ids=current,
        )
        return current

    @_tracked_command
    async def pending_attachments(self, chat_id: str):
        self._ensure_open()
        return tuple(
            attachment
            for attachment_id in self._pending_attachment_ids.get(chat_id, ())
            if (attachment := self._store.get_attachment(attachment_id)) is not None
        )

    @_tracked_command
    async def remove_attachment(self, attachment_id: str) -> None:
        self._ensure_open()
        self._store.delete_attachment(attachment_id)
        await self._events.publish("attachment_removed", attachment_id=attachment_id)

    @_tracked_command
    async def build_context_plan(
        self,
        chat_id: str,
        *,
        parent_message_id: str | None,
        current_user: ContextSource,
        builder: ContextBuilder,
        context_window: int,
        context_window_provenance: str,
        output_reserve: int,
        selected_attachments: tuple[ContextSource, ...] = (),
        bots_required_instructions: tuple[ContextSource, ...] = (),
        envelope: dict[str, object] | None = None,
    ) -> ContextPlan:
        """Build from an explicit parent rather than the mutable chat head."""
        self._ensure_open()
        if self._generation_mode is not GenerationMode.CONFIGURED:
            raise StateError("Phase 6 context planning is unavailable in legacy generation mode")
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        history: list[Message] = []
        cursor = parent_message_id
        visited: set[str] = set()
        while cursor is not None:
            if cursor in visited:
                raise StateError("message lineage contains a cycle")
            visited.add(cursor)
            message = self._store.get_message(cursor)
            if message is None or message.chat_id != chat_id:
                raise StateError("context parent is not in the requested chat")
            history.append(message)
            cursor = message.parent_id
        history.reverse()
        turns: list[tuple[ContextSource, ...]] = []
        pending: Message | None = None
        for message in history:
            if message.role is MessageRole.USER:
                pending = message
                continue
            if (
                pending is not None
                and message.role is MessageRole.ASSISTANT
                and message.parent_id == pending.id
            ):
                turns.append(
                    (
                        ContextSource(pending.id, "history", "user", pending.content, pending.state.value),
                        ContextSource(message.id, "history", "assistant", message.content, message.state.value),
                    )
                )
                pending = None
        return builder.build(
            current_user=current_user,
            historical_turns=tuple(turns),
            selected_attachments=selected_attachments,
            bots_required_instructions=bots_required_instructions,
            envelope=envelope,
            context_window=context_window,
            context_window_provenance=context_window_provenance,
            output_reserve=output_reserve,
            parent_id=parent_message_id,
        )

    def _active_branch_contains(self, chat_id: str, message_id: str) -> Message:
        branch = self._store.list_branch_messages(chat_id)
        for message in branch:
            if message.id == message_id:
                return message
        raise StateError(f"message is not on the active branch: {message_id}")

    def _context_history(self, chat_id: str, parent_id: str | None) -> tuple[tuple[ContextSource, ...], ...]:
        """Return explicit-parent active-lineage turns, never row-order history."""
        chain: list[Message] = []
        cursor = parent_id
        visited: set[str] = set()
        while cursor is not None:
            if cursor in visited:
                raise StateError("message lineage contains a cycle")
            visited.add(cursor)
            message = self._store.get_message(cursor)
            if message is None or message.chat_id != chat_id:
                raise StateError("context parent is not in the requested chat")
            chain.append(message)
            cursor = message.parent_id
        chain.reverse()
        turns: list[tuple[ContextSource, ...]] = []
        pending: Message | None = None
        for message in chain:
            if message.role is MessageRole.USER:
                pending = message
                continue
            if pending is None or message.role is not MessageRole.ASSISTANT or message.parent_id != pending.id:
                continue
            if message.state is MessageState.FAILED and not message.content:
                pending = None
                continue
            turns.append(
                (
                    ContextSource(
                        source_id=pending.id,
                        kind="history",
                        role="user",
                        content=pending.content,
                        state=pending.state.value,
                    ),
                    ContextSource(
                        source_id=message.id,
                        kind="history",
                        role="assistant",
                        content=message.content,
                        state=message.state.value,
                    ),
                )
            )
            pending = None
        return tuple(turns)

    def _selected_attachment_sources(self, chat_id: str) -> tuple[ContextSource, ...]:
        sources: list[ContextSource] = []
        for attachment_id in self._pending_attachment_ids.get(chat_id, ()):
            attachment = self._store.get_attachment(attachment_id)
            if attachment is None:
                raise StateError(f"selected attachment is missing: {attachment_id}")
            eligible = attachment.text_representation_id is not None
            if not eligible:
                raise StateError(
                    f"selected attachment is ineligible ({attachment.ineligibility_reason or 'not_text'}): {attachment_id}"
                )
            text = ""
            if eligible:
                try:
                    text = self._store.read_attachment_bytes(attachment.id).decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise StateError(f"selected attachment is not valid UTF-8: {attachment.id}") from exc
            sources.append(
                ContextSource(
                    source_id=attachment.id,
                    kind="attachment",
                    role="user",
                    content=text,
                    state="complete",
                    eligible=True,
                    selected=True,
                    reason=("selected" if eligible else (attachment.ineligibility_reason or "not_text")),
                    representation_id=attachment.text_representation_id,
                    representation_digest=attachment.text_digest,
                )
            )
        return tuple(sources)

    def _request_and_attempt(
        self,
        *,
        chat_id: str,
        user_message: Message,
        assistant_message: Message,
        attempt_id: str,
        now,
    ) -> tuple[GenerationRequest, GenerationAttempt, ContextPlan | None]:
        context_plan: ContextPlan | None = None
        if self._generation_mode is GenerationMode.CONFIGURED:
            context_history = self._context_history(chat_id, user_message.parent_id)
            selected_attachments = self._selected_attachment_sources(chat_id)
            # ProviderConfiguration owns the frozen model/capability lookup and
            # exact Phase 6 adapter; this call cannot be bypassed by UI state.
            # The current desktop schema has one normal send contract: v3
            # planning is attempted for every configured-model send.  The
            # explicit phase6_enabled=False test/legacy mode is the only
            # compatibility escape hatch for pre-Phase-6 callers.
            phase6 = self._configuration.phase6_enabled
        else:
            context_history = ()
            selected_attachments = ()
            phase6 = False
        if self._generation_mode is GenerationMode.CONFIGURED:
            prepared, snapshot = self._configuration.prepare_generation(
                chat_id=chat_id,
                user_message_id=user_message.id,
                prompt=user_message.content,
                attempt_id=attempt_id,
                context_history=context_history,
                selected_attachments=selected_attachments,
                parent_id=user_message.parent_id,
                phase6=phase6,
            )
            context_plan = prepared.context_plan
            request = prepared.request
            attempt = GenerationAttempt(
                id=attempt_id,
                chat_id=chat_id,
                user_message_id=user_message.id,
                assistant_message_id=assistant_message.id,
                backend_id=request.backend_id,
                model=request.model,
                state=AttemptState.RUNNING,
                request_snapshot=snapshot,
                started_at=now,
                provider_id=request.provider_id,
                remote_outcome_unknown=False,
                connection_id=prepared.attempt_connection_id,
                model_entry_id=prepared.model_entry_id,
            )
            return request, attempt, context_plan

        if self._pending_attachment_ids.get(chat_id):
            raise StateError(
                "attachments are unavailable in the Phase 3 local_openai compatibility mode"
            )
        request = GenerationRequest(
            attempt_id=attempt_id,
            chat_id=chat_id,
            user_message_id=user_message.id,
            backend_id=self._backend_id,
            model=self._model,
            prompt=user_message.content,
            provider_id=self._provider_id,
            base_url=self._base_url,
            api_key_env=self._api_key_env,
        )
        attempt = GenerationAttempt(
            id=attempt_id,
            chat_id=chat_id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            backend_id=self._backend_id,
            model=self._model,
            state=AttemptState.RUNNING,
            request_snapshot=json.dumps(
                request.model_dump(mode="json", exclude_none=True), sort_keys=True
            ),
            started_at=now,
            provider_id=self._provider_id,
            remote_outcome_unknown=False,
        )
        return request, attempt, None

    @_tracked_command
    async def list_provider_connections(self):
        self._ensure_open()
        if self._configuration is None:
            return ()
        return self._configuration.list_connections()

    @_tracked_command
    async def provider_credential_status(self, connection_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None
        connection = self._store.get_provider_connection(connection_id)
        if connection is None:
            raise StateError(f"provider connection not found: {connection_id}")
        return self._configuration.credential_status(connection)

    @_tracked_command
    async def list_model_catalogue(self, connection_id: str | None = None):
        self._ensure_open()
        if self._configuration is None:
            return ()
        return self._configuration.list_models(connection_id)

    @_tracked_command
    async def chat_model_selection(self, chat_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None
        return self._configuration.get_selection(chat_id)

    @_tracked_command
    async def select_model(self, chat_id: str, model_entry_id: str, *, expected_revision: int | None = None):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        selection = self._store.set_chat_model_selection(
            chat_id, model_entry_id, expected_revision=expected_revision
        )
        await self._events.publish(
            "chat_model_selection_changed",
            chat_id=chat_id,
            model_entry_id=model_entry_id,
            revision=selection.revision,
        )
        return selection

    @_tracked_command
    async def resolve_chat_generation_settings(self, chat_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None
        selection = self._configuration.get_selection(chat_id)
        if selection.model_entry_id is None:
            return None
        return self._configuration._resolve_settings(chat_id, selection.model_entry_id)

    @_tracked_command
    async def chat_generation_settings_override(self, chat_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None
        selection = self._configuration.get_selection(chat_id)
        if selection.model_entry_id is None:
            return None
        return self._store.get_chat_model_generation_settings(chat_id, selection.model_entry_id)

    @_tracked_command
    async def chat_generation_settings_override_with_revision(self, chat_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None, None
        selection = self._configuration.get_selection(chat_id)
        if selection.model_entry_id is None:
            return None, None
        return self._store.get_chat_model_generation_config(chat_id, selection.model_entry_id)

    @_tracked_command
    async def set_chat_generation_settings(
        self,
        chat_id: str,
        settings,
        *,
        expected_revision: int | None = None,
        expected_model_entry_id: str | None = None,
    ):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        selection = self._configuration.get_selection(chat_id)
        if selection.model_entry_id is None:
            raise StateError("chat requires an explicit model selection")
        if (
            expected_model_entry_id is not None
            and selection.model_entry_id != expected_model_entry_id
        ):
            raise RevisionConflict("chat model selection changed")
        from .provider_configuration import _validate_settings

        _validate_settings(settings)
        revision = self._store.set_chat_model_generation_settings(
            chat_id, selection.model_entry_id, settings, expected_revision=expected_revision
        )
        await self._events.publish(
            "chat_model_generation_settings_changed",
            chat_id=chat_id,
            model_entry_id=selection.model_entry_id,
            revision=revision,
        )
        return await self.resolve_chat_generation_settings(chat_id)

    @_tracked_command
    async def set_application_generation_settings(self, settings, *, expected_revision: int | None = None):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        from .provider_configuration import _validate_settings

        _validate_settings(settings)
        revision = self._store.set_application_generation_settings(
            settings, expected_revision=expected_revision
        )
        await self._events.publish("application_generation_settings_changed", revision=revision)
        return revision

    @_tracked_command
    async def application_generation_settings(self):
        self._ensure_open()
        if self._configuration is None:
            return None
        return self._store.get_application_generation_settings()

    @_tracked_command
    async def create_provider_connection(self, **kwargs):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        connection = self._configuration.create_connection(**kwargs)
        await self._events.publish("provider_connection_changed", connection_id=connection.id)
        return connection

    @_tracked_command
    async def edit_provider_connection(self, connection, *, expected_revision: int):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        updated = self._configuration.edit_connection(connection, expected_revision=expected_revision)
        await self._events.publish("provider_connection_changed", connection_id=updated.id)
        return updated

    @_tracked_command
    async def set_provider_connection_enabled(self, connection_id: str, enabled: bool, *, expected_revision: int):
        self._ensure_open()
        current = self._store.get_provider_connection(connection_id)
        if current is None:
            raise StateError(f"provider connection not found: {connection_id}")
        updated = self._store.set_provider_connection_enabled(
            connection_id, enabled, expected_revision=expected_revision
        )
        await self._events.publish("provider_connection_changed", connection_id=updated.id)
        return updated

    async def save_connection_credential(
        self,
        connection_id: str,
        value: str,
        *,
        expected_revision: int | None = None,
        expected_credential_reference: str | None = None,
    ):
        try:
            async with self._command_scope():
                self._ensure_open()
                if self._configuration is None:
                    raise StateError("provider/model configuration is unavailable")
                connection = self._store.get_provider_connection(connection_id)
                if connection is None:
                    raise StateError(f"provider connection not found: {connection_id}")
                if expected_revision is not None and connection.revision != expected_revision:
                    raise RevisionConflict("provider credential settings are stale")
                if (
                    expected_credential_reference is not None
                    and connection.credential_reference != expected_credential_reference
                ):
                    raise RevisionConflict("provider credential reference is stale")
                save_failure: SecretStoreError | None = None
                try:
                    status = self._configuration.save_credential(connection, value)
                except Exception as exc:
                    save_failure = SecretStoreError(sanitize_secret_error(exc, value))
                # The status event contains no credential material, but publication
                # may wait on a full subscriber queue. Clear the value first. This
                # also ensures the new exception is raised after the source error's
                # exception block has ended, without retaining its traceback/context.
                value = None
                if save_failure is not None:
                    raise save_failure
                await self._events.publish(
                    "provider_credential_status_changed",
                    connection_id=connection_id,
                    status=status.status,
                )
                return status
        finally:
            # Failed tasks retain traceback frames. Do not leave the submitted
            # credential in this frame for callers that inspect task.exception().
            value = None

    @_tracked_command
    async def delete_connection_credential(
        self,
        connection_id: str,
        *,
        expected_revision: int | None = None,
        expected_credential_reference: str | None = None,
    ):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        connection = self._store.get_provider_connection(connection_id)
        if connection is None:
            raise StateError(f"provider connection not found: {connection_id}")
        if expected_revision is not None and connection.revision != expected_revision:
            raise RevisionConflict("provider credential settings are stale")
        if (
            expected_credential_reference is not None
            and connection.credential_reference != expected_credential_reference
        ):
            raise RevisionConflict("provider credential reference is stale")
        status = self._configuration.delete_credential(connection)
        await self._events.publish("provider_credential_status_changed", connection_id=connection_id, status=status.status)
        return status

    @_tracked_command
    async def retire_provider_connection(self, connection_id: str, *, expected_revision: int, replacement_model_entry_id: str | None = None):
        self._ensure_open()
        updated = self._store.retire_provider_connection(
            connection_id,
            expected_revision=expected_revision,
            replacement_model_entry_id=replacement_model_entry_id,
        )
        await self._events.publish("provider_connection_changed", connection_id=updated.id)
        return updated

    @_tracked_command
    async def add_manual_model(self, *, connection_id: str, provider_model_id: str, display_name: str | None = None, metadata: dict[str, object] | None = None):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        model = self._configuration.add_manual_model(
            connection_id=connection_id,
            provider_model_id=provider_model_id,
            display_name=display_name,
            metadata=metadata,
        )
        await self._events.publish("model_catalogue_changed", connection_id=connection_id)
        return model

    @_tracked_command
    async def refresh_models(self, connection_id: str, discoverer):
        """Perform one operator-triggered refresh; no caller invokes this at startup."""
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        connection = self._store.get_provider_connection(connection_id)
        if connection is None:
            raise StateError(f"provider connection not found: {connection_id}")
        expected_catalogue_revision = connection.catalogue_revision
        expected_connection_revision = connection.revision
        failure_catalogue_changed = False
        failure_class = CatalogueRefreshFailureClass.UNKNOWN
        credential: str | None = None
        failure: BaseException | None = None
        try:
            credential = self._configuration.credential_value(connection)
            discovered = await discoverer.discover(connection, credential)
            records = tuple(model.as_record() for model in discovered)
            try:
                reject_secret_material(records, credential)
            except Exception as exc:
                raise ProviderError(sanitize_secret_error(exc, credential)) from None
            models = self._store.refresh_model_catalogue(
                connection_id,
                records,
                success=True,
                expected_catalogue_revision=expected_catalogue_revision,
                expected_connection_revision=expected_connection_revision,
            )
            refreshed_connection = self._store.get_provider_connection(connection_id)
            discovery_revision = None if refreshed_connection is None else refreshed_connection.catalogue_revision
            for model in models:
                if model.availability.value != "available":
                    continue
                metadata = model.metadata
                for metadata_key, capability_key in (
                    ("context_length", CapabilityKey.CONTEXT_TOKENS.value),
                    ("max_output_tokens", CapabilityKey.OUTPUT_TOKENS.value),
                ):
                    value = metadata.get(metadata_key)
                    if type(value) is int and value > 0:
                        self._store.set_capability_fact(
                            CapabilityFact(
                                model.id,
                                capability_key,
                                CapabilityState.SUPPORTED,
                                CapabilitySource.PROVIDER_METADATA,
                                discovery_revision,
                                value,
                                {"field": metadata_key, "catalogue_revision": discovery_revision},
                                self._clock.now(),
                            )
                        )
                if connection.backend_type is BackendType.FAKE:
                    for fact in _fake_capability_facts(model.id, self._clock.now()):
                        self._store.set_capability_fact(fact)
        except Exception as exc:
            if not isinstance(exc, RevisionConflict):
                if isinstance(exc, ModelDiscoveryError):
                    failure_class = exc.failure_class
                try:
                    self._store.refresh_model_catalogue(
                        connection_id,
                        (),
                        success=False,
                        failure_class=failure_class,
                        expected_catalogue_revision=expected_catalogue_revision,
                        expected_connection_revision=expected_connection_revision,
                    )
                    failure_catalogue_changed = True
                except RevisionConflict:
                    pass
            if credential is not None and not isinstance(exc, RevisionConflict):
                failure = ProviderError(
                    f"model discovery failed: {sanitize_secret_error(exc, credential)}"
                )
            else:
                failure = exc
            # Do not carry the resolved credential across any event-bus await.
            credential = None
            discovered = None
            records = None
            models = None
        if failure_catalogue_changed:
            await self._events.publish("model_catalogue_changed", connection_id=connection_id)
        if failure is not None:
            raise failure
        # The provider operation and all secret-dependent validation are complete.
        # Event publication may suspend under subscriber backpressure, so the
        # resolved credential must not remain reachable during that await.
        credential = None
        await self._events.publish("model_catalogue_changed", connection_id=connection_id)
        return models

    @_tracked_command
    async def set_application_default_model(self, model_entry_id: str, *, expected_revision: int | None = None):
        self._ensure_open()
        revision = self._store.set_application_default_model(
            model_entry_id, expected_revision=expected_revision
        )
        await self._events.publish("application_default_model_changed", model_entry_id=model_entry_id, revision=revision)
        return revision

    @_tracked_command
    async def set_model_defaults(self, model_entry_id: str, settings, *, expected_revision: int | None = None):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        revision = self._configuration.set_model_defaults(model_entry_id, settings, expected_revision=expected_revision)
        await self._events.publish("model_generation_settings_changed", model_entry_id=model_entry_id, revision=revision)
        return revision

    @_tracked_command
    async def model_defaults(self, model_entry_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None
        return self._store.get_model_generation_settings(model_entry_id)

    @_tracked_command
    async def model_defaults_with_revision(self, model_entry_id: str):
        self._ensure_open()
        if self._configuration is None:
            return None, None
        return self._store.get_model_generation_config(model_entry_id)

    @_tracked_command
    async def application_generation_config(self):
        self._ensure_open()
        if self._configuration is None:
            return None
        return self._store.get_application_generation_config()

    @_tracked_command
    async def set_capability_override(self, override, *, expected_revision: int | None = None):
        self._ensure_open()
        if self._configuration is None:
            raise StateError("provider/model configuration is unavailable")
        result = self._configuration.set_capability_override(override, expected_revision=expected_revision)
        await self._events.publish("capability_changed", model_entry_id=result.model_entry_id, capability_key=result.key)
        return result

    @_tracked_command
    async def capability_overrides(self, model_entry_id: str):
        self._ensure_open()
        if self._configuration is None:
            return ()
        return self._store.list_capability_overrides(model_entry_id)

    @_tracked_command
    async def model_capabilities(self, model_entry_id: str):
        self._ensure_open()
        if self._configuration is None:
            return ()
        return self._configuration.resolve_capabilities(model_entry_id)

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
        self._generation_tasks[attempt.id] = task
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
    async def cancel_generation(self, attempt_id: str) -> GenerationAttempt:
        """Cancel one local generation and wait until its aborted state is durable."""
        self._ensure_open()
        pending = self._pending_generations.get(attempt_id)
        task = self._generation_tasks.get(attempt_id)
        if pending is None or task is None:
            stored = self._store.get_generation_attempt(attempt_id)
            if stored is None:
                raise StateError(f"generation attempt is not running: {attempt_id}")
            if stored.state is not AttemptState.RUNNING:
                return stored
            raise StateError(f"generation task is unavailable: {attempt_id}")
        self._cancel_requested.add(attempt_id)
        terminal_event = self._generation_terminal_events.get(attempt_id)
        if not task.done() and (
            terminal_event is None or not terminal_event.is_set()
        ):
            task.cancel()
        if terminal_event is None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            wait_task = asyncio.create_task(terminal_event.wait())
            try:
                done, _ = await asyncio.wait(
                    (task, wait_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task not in done:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
                if not wait_task.done():
                    wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
        if task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        message, _ = pending
        stored = next(
            (
                attempt
                for attempt in self._store.list_generation_attempts(message.chat_id)
                if attempt.id == attempt_id
            ),
            None,
        )
        if stored is None:
            raise StateError(f"generation attempt disappeared: {attempt_id}")
        return stored

    @_tracked_command
    async def send_message(self, chat_id: str, text: str) -> GenerationAttempt:
        self._ensure_open()
        if not text.strip():
            raise StateError("message text must not be empty")
        chat = self._store.get_chat(chat_id)
        if chat is None:
            raise StateError(f"chat not found: {chat_id}")
        self._ensure_chat_has_no_active_generation(chat_id)

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
        request, attempt, context_plan = self._request_and_attempt(
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
            context_plan=context_plan,
            attachment_ids=self._pending_attachment_ids.get(chat_id, ()),
        )
        self._pending_attachment_ids.pop(chat_id, None)
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
        self._ensure_chat_has_no_active_generation(chat_id)
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
        request, attempt, context_plan = self._request_and_attempt(
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
            context_plan=context_plan,
            attachment_ids=self._pending_attachment_ids.get(chat_id, ()),
        )
        self._pending_attachment_ids.pop(chat_id, None)
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
        self._ensure_chat_has_no_active_generation(chat_id)
        target = self._active_branch_contains(chat_id, message_id)
        if target.role != MessageRole.ASSISTANT or target.state == MessageState.STREAMING:
            raise StateError("only terminal assistant messages can be regenerated")
        if target.parent_id is None:
            raise StateError("assistant message has no user parent")
        user_message = self._store.get_message(target.parent_id)
        if user_message is None or user_message.role != MessageRole.USER:
            raise StateError("assistant message has an invalid user parent")
        if chat_id not in self._pending_attachment_ids:
            self._pending_attachment_ids[chat_id] = tuple(
                attachment.id for attachment in self._store.list_message_attachments(user_message.id)
            )
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
        request, attempt, context_plan = self._request_and_attempt(
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
            context_plan=context_plan,
            attachment_ids=self._pending_attachment_ids.get(chat_id, ()),
        )
        self._pending_attachment_ids.pop(chat_id, None)
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
        dispatch_may_have_occurred = False
        try:
            ready.set()
            await asyncio.sleep(0)
            terminal = False
            stream = self._backend.stream(request)
            iterator = stream.__aiter__()
            deadline = (
                None
                if request.timeout_seconds is None
                else asyncio.get_running_loop().time() + request.timeout_seconds
            )
            while True:
                if deadline is None:
                    try:
                        event = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                else:
                    remaining = deadline - asyncio.get_running_loop().time()
                    next_event = asyncio.create_task(iterator.__anext__())
                    # Always give an already-available backend event one event
                    # loop turn.  Durable local persistence can consume the
                    # remaining wall-clock budget, but must not discard output
                    # the backend had already produced before the deadline.
                    deadline_wait = asyncio.create_task(
                        asyncio.sleep(max(0.0, remaining))
                    )
                    try:
                        done, _ = await asyncio.wait(
                            (next_event, deadline_wait),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if next_event in done:
                            deadline_wait.cancel()
                            await asyncio.gather(deadline_wait, return_exceptions=True)
                            try:
                                event = next_event.result()
                            except StopAsyncIteration:
                                break
                        else:
                            _abandon_task(next_event)
                            raise GenerationTimeout
                    finally:
                        if not next_event.done():
                            _abandon_task(next_event)
                        if not deadline_wait.done():
                            deadline_wait.cancel()
                        await asyncio.gather(deadline_wait, return_exceptions=True)
                        if next_event.done():
                            _abandon_task(next_event)
                if event.attempt_id != attempt.id:
                    raise StateError("generation backend returned an event for another attempt")
                if attempt.id in self._cancel_requested:
                    raise asyncio.CancelledError
                if isinstance(event, GenerationDispatched):
                    with self._application_effect_scope(independent=True):
                        dispatch_may_have_occurred = True
                        current_attempt = replace(current_attempt, remote_outcome_unknown=True)
                        self._store.update_attempt(current_attempt)
                        self._update_tracked_generation(message, current_attempt)
                        await self._publish_after_persistence(
                            "generation_dispatched",
                            chat_id=message.chat_id,
                            message_id=message.id,
                            attempt_id=attempt.id,
                        )
                elif isinstance(event, GenerationMetadata):
                    with self._application_effect_scope(independent=True):
                        current_attempt = _merge_attempt_metadata(current_attempt, event)
                        self._store.update_attempt(current_attempt)
                        self._update_tracked_generation(message, current_attempt)
                elif isinstance(event, GenerationDelta):
                    with self._application_effect_scope(independent=True):
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
                    with self._application_effect_scope(independent=True):
                        now = self._clock.now()
                        current_attempt = _merge_attempt_metadata(current_attempt, event)
                        if type(event.finish_reason) is not str or not event.finish_reason:
                            error_type = "malformed_finish_reason"
                            error_message = "generation completed with an invalid finish_reason"
                            message = replace(message, state=MessageState.FAILED)
                            current_attempt = replace(
                                current_attempt,
                                state=AttemptState.FAILED,
                                ended_at=now,
                                error_type=error_type,
                                error_message=error_message,
                                finish_reason=None,
                                remote_outcome_unknown=(
                                    event.remote_outcome_unknown
                                    if event.remote_outcome_unknown is not None
                                    else dispatch_may_have_occurred
                                ),
                            )
                            self._finalize_generation(message, current_attempt)
                            terminal_persisted = True
                            await self._publish_after_persistence(
                                "generation_failed",
                                chat_id=message.chat_id,
                                message_id=message.id,
                                attempt_id=attempt.id,
                                error_type=error_type,
                                error_message=error_message,
                            )
                        else:
                            current_attempt = replace(
                                current_attempt,
                                finish_reason=event.finish_reason,
                            )
                            if event.finish_reason == "stop":
                                message = replace(message, state=MessageState.COMPLETE)
                                current_attempt = replace(
                                    current_attempt,
                                    state=AttemptState.COMPLETE,
                                    ended_at=now,
                                    remote_outcome_unknown=event.remote_outcome_unknown,
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
                                    remote_outcome_unknown=event.remote_outcome_unknown,
                                )
                                event_kind = "generation_incomplete"
                            self._finalize_generation(message, current_attempt)
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
                    with self._application_effect_scope(independent=True):
                        now = self._clock.now()
                        message = replace(message, state=MessageState.FAILED)
                        current_attempt = replace(
                            current_attempt,
                            state=AttemptState.FAILED,
                            ended_at=now,
                            error_type=event.error_type,
                            error_message=event.error_message,
                            remote_outcome_unknown=(
                                event.remote_outcome_unknown
                                if event.remote_outcome_unknown is not None
                                else dispatch_may_have_occurred
                            ),
                        )
                        self._finalize_generation(message, current_attempt)
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

            if not terminal and attempt.id in self._cancel_requested:
                with self._application_effect_scope(independent=True):
                    now = self._clock.now()
                    message = replace(message, state=MessageState.ABORTED)
                    current_attempt = replace(
                        current_attempt,
                        state=AttemptState.ABORTED,
                        ended_at=now,
                        error_type="aborted",
                        error_message="generation was cancelled",
                        remote_outcome_unknown=dispatch_may_have_occurred,
                    )
                    self._finalize_generation(message, current_attempt)
                    terminal_persisted = True
                    await self._publish_after_persistence(
                        "generation_aborted",
                        chat_id=message.chat_id,
                        message_id=message.id,
                        attempt_id=attempt.id,
                    )
            elif not terminal:
                with self._application_effect_scope(independent=True):
                    now = self._clock.now()
                    message = replace(message, state=MessageState.INCOMPLETE)
                    current_attempt = replace(
                        current_attempt,
                        state=AttemptState.INCOMPLETE,
                        ended_at=now,
                        error_type="missing_terminal_event",
                        error_message="generation stream ended without a terminal event",
                        remote_outcome_unknown=dispatch_may_have_occurred,
                    )
                    self._finalize_generation(message, current_attempt)
                    terminal_persisted = True
                    await self._publish_after_persistence(
                        "generation_incomplete",
                        chat_id=message.chat_id,
                        message_id=message.id,
                        attempt_id=attempt.id,
                    )
        except GenerationTimeout:
            if terminal_persisted:
                raise
            with self._application_effect_scope(independent=True):
                now = self._clock.now()
                message = replace(message, state=MessageState.FAILED)
                current_attempt = replace(
                    current_attempt,
                    state=AttemptState.FAILED,
                    ended_at=now,
                    error_type="timeout",
                    error_message="B.O.T.S. generation deadline expired",
                    remote_outcome_unknown=(
                        True if dispatch_may_have_occurred else current_attempt.remote_outcome_unknown
                    ),
                )
                self._finalize_generation(message, current_attempt)
                terminal_persisted = True
                await self._publish_after_persistence(
                    "generation_failed",
                    chat_id=message.chat_id,
                    message_id=message.id,
                    attempt_id=attempt.id,
                    error_type="timeout",
                    error_message="B.O.T.S. generation deadline expired",
                )
        except asyncio.CancelledError:
            if terminal_persisted:
                raise
            close_stream = getattr(iterator, "aclose", None)
            if close_stream is not None:
                try:
                    await close_stream()
                except BaseException:
                    pass
            with self._application_effect_scope(independent=True):
                now = self._clock.now()
                message = replace(message, state=MessageState.ABORTED)
                current_attempt = replace(
                    current_attempt,
                    state=AttemptState.ABORTED,
                    ended_at=now,
                    error_type="aborted",
                    error_message="generation was cancelled",
                    remote_outcome_unknown=dispatch_may_have_occurred,
                )
                self._finalize_generation(message, current_attempt)
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
            with self._application_effect_scope(independent=True):
                now = self._clock.now()
                message = replace(message, state=MessageState.FAILED)
                current_attempt = replace(
                    current_attempt,
                    state=AttemptState.FAILED,
                    ended_at=now,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    returned_model=None,
                    request_id=None,
                    finish_reason=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    reasoning_tokens=None,
                    total_tokens=None,
                    known_cost_usd=None,
                    remote_outcome_unknown=(
                        True if dispatch_may_have_occurred else current_attempt.remote_outcome_unknown
                    ),
                )
                self._finalize_generation(message, current_attempt)
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
            self._generation_tasks.pop(attempt.id, None)
            self._generation_terminal_events.pop(attempt.id, None)
            self._cancel_requested.discard(attempt.id)

    async def _close_driver(
        self, initial_errors: tuple[TerminalCloseError, ...]
    ) -> TerminalCloseResult:
        """Run teardown exactly once and always complete with scalar-safe data."""
        errors = list(initial_errors)
        try:
            await self._execution.shutdown()
        except BaseException:
            errors.append(_close_error("execution"))

        try:
            await self._commands_idle.wait()
            # Selection and finalization are one fallback effect.  If this
            # independent close-driver grant loses authority, durable RUNNING
            # state is deliberately left for a fresh authority at restart.
            if self._pending_generations:
                effect_scope = self._application_effect_scope(independent=True)
            else:
                effect_scope = nullcontext()
            with effect_scope:
                for attempt_id, (message, attempt) in tuple(
                    self._pending_generations.items()
                ):
                    try:
                        stored_message = self._store.get_message(message.id)
                        stored_attempt = next(
                            (
                                item
                                for item in self._store.list_generation_attempts(
                                    message.chat_id
                                )
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
                                    error_message=(
                                        "generation was cancelled during shutdown"
                                    ),
                                    remote_outcome_unknown=(
                                        stored_attempt.remote_outcome_unknown
                                    ),
                                ),
                            )
                    except BaseException:
                        if not any(
                            error.stage == "reconciliation" for error in errors
                        ):
                            errors.append(_close_error("reconciliation"))
                    finally:
                        self._pending_generations.pop(attempt_id, None)
                        self._generation_tasks.pop(attempt_id, None)
                        self._generation_terminal_events.pop(attempt_id, None)
                        self._cancel_requested.discard(attempt_id)
        except BaseException:
            if not any(error.stage == "reconciliation" for error in errors):
                errors.append(_close_error("reconciliation"))
            # Release-only in-memory bookkeeping is still permitted after the
            # fallback grant is rejected or revoked; no persistence occurs.
            for attempt_id in tuple(self._pending_generations):
                self._pending_generations.pop(attempt_id, None)
                self._generation_tasks.pop(attempt_id, None)
                self._generation_terminal_events.pop(attempt_id, None)
                self._cancel_requested.discard(attempt_id)

        try:
            self._store.close()
        except BaseException:
            errors.append(_close_error("store", authority=True))

        errors.sort(key=lambda error: _CLOSE_PRECEDENCE[error.stage])
        result = TerminalCloseResult(tuple(errors))
        self._close_result = result
        self._close_state = (
            ApplicationCloseState.CLOSED
            if result.succeeded
            else ApplicationCloseState.FAILED
        )
        return result

    def _forget_close_task(self, task: asyncio.Task[TerminalCloseResult]) -> None:
        if self._close_task is task:
            # The task is guaranteed to have a normal, data-only result.
            task.result()
            self._close_task = None

    @staticmethod
    def _raise_terminal_close(result: TerminalCloseResult) -> None:
        if result.succeeded:
            return
        error = result.errors[0]
        if error.public_kind == "authority":
            raise AuthorityError(error.message) from None
        raise StateError(error.message) from None

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        if self._close_state is ApplicationCloseState.OPEN:
            self._close_state = ApplicationCloseState.CLOSING
            self._close_loop = loop
            event_errors: tuple[TerminalCloseError, ...] = ()
            try:
                self._events.close()
            except BaseException:
                event_errors = (_close_error("events"),)
            task = loop.create_task(self._close_driver(event_errors))
            self._close_task = task
            task.add_done_callback(self._forget_close_task)
        elif self._close_result is None and self._close_loop is not loop:
            raise StateError("application close belongs to another event loop")

        result = self._close_result
        if result is None:
            task = self._close_task
            if task is None:
                raise StateError("application close has no terminal operation")
            result = await asyncio.shield(task)
        self._raise_terminal_close(result)
