from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta, timezone
import math
import threading

import pytest

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.domain.models import Chat, Message, MessageRole, MessageState
from bots5.domain.search import (
    SearchBranchState,
    SearchDocumentKind,
    SearchFilters,
    SearchIndexCondition,
    SearchLocation,
    SearchNavigation,
    SearchPage,
    SearchResult,
    SearchStatus,
)


NOW = datetime(2026, 9, 11, 1, 2, 3, tzinfo=timezone.utc)


def _status(
    condition: SearchIndexCondition = SearchIndexCondition.VALID,
    *,
    source_revision: int = 3,
    checkpoint_revision: int = 3,
    generation: int = 7,
) -> SearchStatus:
    return SearchStatus(
        condition,
        source_revision,
        checkpoint_revision,
        generation,
        1,
        "unicode61-v1",
    )


def _message_result(*, generation: int = 7) -> SearchResult:
    return SearchResult(
        document_key="message:message-1",
        document_kind=SearchDocumentKind.MESSAGE,
        document_id="message-1",
        title="A chat",
        snippet="literal result",
        rank=-0.75,
        authoritative_at=NOW,
        chat_id="chat-1",
        message_id="message-1",
        lineage_id="lineage-1",
        revision=2,
        supersedes_message_id="message-0",
        role=MessageRole.USER,
        state=MessageState.SENT,
        locations=(
            SearchLocation(
                "chat-1",
                "message-1",
                SearchBranchState.ACTIVE,
                None,
            ),
        ),
        checkpoint_revision=3,
        generation=generation,
    )


def _chat() -> Chat:
    return Chat(
        id="chat-1",
        title="A chat",
        created_at=NOW,
        updated_at=NOW,
        head_message_id="message-1",
        revision=4,
    )


def _message() -> Message:
    return Message(
        id="message-1",
        chat_id="chat-1",
        role=MessageRole.USER,
        state=MessageState.SENT,
        content="literal result",
        sequence=1,
        created_at=NOW,
        lineage_id="lineage-1",
        revision=2,
        supersedes_id="message-0",
    )


def _navigation() -> SearchNavigation:
    return SearchNavigation(
        chat=_chat(),
        messages=(_message(),),
        focus_message_id="message-1",
        historical_leaf_message_id=None,
        branch_state=SearchBranchState.ACTIVE,
        archived_at=None,
    )


def test_search_domain_contracts_are_immutable_and_validate_identity_and_bounds():
    filters = SearchFilters(
        document_kinds=(SearchDocumentKind.MESSAGE,),
        roles=(MessageRole.USER,),
        message_states=(MessageState.SENT,),
        active_branch_only=True,
    )
    with pytest.raises(FrozenInstanceError):
        filters.include_archived = True  # type: ignore[misc]

    with pytest.raises(ValueError, match="chat id"):
        SearchFilters(chat_id="")
    with pytest.raises(ValueError, match="inverted"):
        SearchFilters(after=NOW, before=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="chat id"):
        SearchLocation("", None, SearchBranchState.ACTIVE)

    result = _message_result()
    assert result.document_key == "message:message-1"
    with pytest.raises(ValueError, match="identity"):
        replace(result, document_key="message:not-message-1")
    with pytest.raises(ValueError, match="finite"):
        replace(result, rank=math.inf)
    with pytest.raises(ValueError, match="revision and generation"):
        replace(result, checkpoint_revision=-1)
    with pytest.raises(ValueError, match="require a location"):
        SearchResult(
            document_key="attachment:attachment-1",
            document_kind=SearchDocumentKind.ATTACHMENT,
            document_id="attachment-1",
            title="notes.txt",
            snippet="notes",
            rank=-1.0,
            authoritative_at=NOW,
        )


def test_search_status_page_and_navigation_invariants_are_explicit():
    status = _status()
    page = SearchPage((_message_result(),), None, status)

    assert status.is_searchable
    assert page.status is status
    assert not _navigation().is_historical
    assert SearchNavigation(
        chat=_chat(),
        messages=(_message(),),
        focus_message_id="message-1",
        historical_leaf_message_id="message-1",
        branch_state=SearchBranchState.HISTORICAL,
        archived_at=None,
    ).is_historical

    with pytest.raises(ValueError, match="nonnegative"):
        _status(source_revision=-1)
    with pytest.raises(ValueError, match="valid index"):
        SearchPage(
            (),
            None,
            _status(
                SearchIndexCondition.STALE,
                source_revision=4,
                checkpoint_revision=3,
            ),
        )
    with pytest.raises(ValueError, match="does not match"):
        SearchPage((_message_result(generation=8),), None, status)


@dataclass(slots=True)
class _FixedClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0


class _Ids:
    def __init__(self) -> None:
        self._next = 0

    def new(self) -> str:
        self._next += 1
        return f"event-{self._next}"


class _NeverProviderBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request):
        del request
        self.calls += 1
        raise AssertionError("Phase 7 core search must not contact a provider")
        yield  # pragma: no cover - makes this an async iterator


class _CoreContractStore:
    def __init__(self) -> None:
        self.chat = _chat()
        self.valid_status = _status()
        self.diagnostic_status = _status()
        self.page = SearchPage((_message_result(),), "next-token", self.valid_status)
        self.navigation = _navigation()
        self.calls: list[tuple[object, ...]] = []
        self.event_loop = asyncio.get_running_loop()
        self.rebuild_started = asyncio.Event()
        self.rebuild_completion_barrier = threading.Barrier(2)
        self.rebuild_completed = threading.Event()
        self.rebuild_thread_id: int | None = None

    def command_admission(self, *, independent: bool = False):
        del independent
        return nullcontext()

    def event_admission(self, *, independent: bool = False):
        del independent
        return nullcontext()

    def issued_event_effect(self):
        return nullcontext()

    def assert_admitting(self) -> None:
        return None

    def reconcile_interrupted_generations(self, now: datetime) -> None:
        del now

    def archive_chat(
        self,
        chat_id: str,
        archived_at: datetime | None,
        *,
        expected_revision: int | None = None,
    ) -> Chat:
        self.calls.append(("archive_chat", chat_id, archived_at, expected_revision))
        self.chat = replace(
            self.chat,
            archived_at=archived_at,
        )
        return self.chat

    def search_status(self) -> SearchStatus:
        self.calls.append(("search_status",))
        return self.valid_status

    def search(
        self,
        query: str,
        *,
        filters: SearchFilters = SearchFilters(),
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        self.calls.append(("search", query, filters, limit, cursor))
        return self.page

    def rebuild_search_index(self) -> SearchStatus:
        self.calls.append(("rebuild_search_index",))
        self.rebuild_thread_id = threading.get_ident()
        self.event_loop.call_soon_threadsafe(self.rebuild_started.set)
        self.rebuild_completion_barrier.wait(timeout=5)
        self.rebuild_completed.set()
        return self.valid_status

    def diagnose_search_index(self) -> SearchStatus:
        self.calls.append(("diagnose_search_index",))
        return self.diagnostic_status

    def resolve_search_result(
        self,
        result: SearchResult,
        *,
        location_index: int = 0,
    ) -> SearchNavigation:
        self.calls.append(("resolve_search_result", result, location_index))
        return self.navigation


def _application() -> tuple[BotsApplication, _CoreContractStore, _NeverProviderBackend]:
    store = _CoreContractStore()
    backend = _NeverProviderBackend()
    application = BotsApplication(
        store,  # type: ignore[arg-type]
        EventBus(_FixedClock(), _Ids()),  # type: ignore[arg-type]
        backend,
        ids=_Ids(),  # type: ignore[arg-type]
        clock=_FixedClock(),
    )
    return application, store, backend


def test_application_archive_unarchive_delegate_and_publish_corresponding_events():
    async def scenario() -> None:
        application, store, backend = _application()
        subscription = application.subscribe()

        archived = await application.archive_chat("chat-1")
        archived_event = await subscription.__anext__()
        assert archived.archived_at == NOW
        assert archived.revision == 4
        assert archived_event.kind == "chat_archived"
        assert archived_event.payload == {"chat_id": "chat-1", "archived_at": NOW}

        unarchived = await application.unarchive_chat("chat-1")
        unarchived_event = await subscription.__anext__()
        assert unarchived.archived_at is None
        assert unarchived.revision == 4
        assert unarchived_event.kind == "chat_unarchived"
        assert unarchived_event.payload == {"chat_id": "chat-1", "archived_at": None}
        assert store.calls == [
            ("archive_chat", "chat-1", NOW, None),
            ("archive_chat", "chat-1", None, None),
        ]
        assert backend.calls == 0
        subscription.close()

    asyncio.run(scenario())


def test_application_search_status_diagnose_and_resolve_are_exact_delegates():
    async def scenario() -> None:
        application, store, backend = _application()
        filters = SearchFilters(
            chat_id="chat-1",
            active_branch_only=True,
            include_archived=True,
        )
        result = _message_result()

        assert await application.search_status() is store.valid_status
        assert await application.search(
            '"operator" AND *',
            filters=filters,
            limit=13,
            cursor="cursor-1",
        ) is store.page
        assert await application.diagnose_search_index() is store.diagnostic_status
        assert await application.resolve_search_result(
            result,
            location_index=0,
        ) is store.navigation
        assert store.calls == [
            ("search_status",),
            ("search", '"operator" AND *', filters, 13, "cursor-1"),
            ("diagnose_search_index",),
            ("resolve_search_result", result, 0),
        ]
        assert backend.calls == 0

    asyncio.run(scenario())


def test_application_rebuild_is_awaited_and_event_follows_store_completion():
    async def scenario() -> None:
        application, store, backend = _application()
        subscription = application.subscribe()
        event_loop_thread_id = threading.get_ident()
        rebuild = asyncio.create_task(application.rebuild_search_index())

        await asyncio.wait_for(store.rebuild_started.wait(), timeout=1)
        try:
            assert not rebuild.done()
            assert subscription._queue.empty()
            assert not store.rebuild_completed.is_set()
            assert store.rebuild_thread_id != event_loop_thread_id
        finally:
            store.rebuild_completion_barrier.wait(timeout=1)

        assert store.rebuild_completed.wait(timeout=1)
        assert await asyncio.wait_for(rebuild, timeout=1) is store.valid_status
        event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        assert store.rebuild_completed.is_set()
        assert event.kind == "search_index_rebuilt"
        assert event.payload == {
            "condition": "VALID",
            "source_revision": 3,
            "checkpoint_revision": 3,
            "generation": 7,
        }
        assert store.calls == [("rebuild_search_index",)]
        assert backend.calls == 0
        subscription.close()

    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        loop.set_default_executor(executor)
        loop.run_until_complete(scenario())
    finally:
        executor.shutdown(wait=True)
        loop.close()


def test_application_rebuild_cancellation_waits_for_owned_effect_and_event():
    async def scenario() -> None:
        application, store, backend = _application()
        subscription = application.subscribe()
        rebuild = asyncio.create_task(application.rebuild_search_index())

        await asyncio.wait_for(store.rebuild_started.wait(), timeout=1)
        rebuild.cancel()
        cancellation_turn = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_turn.set)
        await cancellation_turn.wait()
        assert not rebuild.done()
        assert application._active_commands == 1
        assert not application._commands_idle.is_set()
        assert subscription._queue.empty()

        store.rebuild_completion_barrier.wait(timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await rebuild
        assert store.rebuild_completed.is_set()
        event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        assert event.kind == "search_index_rebuilt"
        assert application._active_commands == 0
        assert application._commands_idle.is_set()
        assert store.calls == [("rebuild_search_index",)]
        assert backend.calls == 0
        subscription.close()

    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        loop.set_default_executor(executor)
        loop.run_until_complete(scenario())
    finally:
        executor.shutdown(wait=True)
        loop.close()
