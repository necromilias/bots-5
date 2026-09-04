from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus, EventSubscription
from bots5.core.errors import StateError
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import AttemptState, Chat, GenerationAttempt, Message, MessageRole, MessageState
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database


def _application(tmp_path: Path) -> BotsApplication:
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=32),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )


async def _wait_for_terminal(subscription: EventSubscription, attempt_id: str) -> list[str]:
    kinds: list[str] = []
    while True:
        event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        if event.payload.get("attempt_id") == attempt_id:
            kinds.append(event.kind)
            if event.kind in {
                "generation_completed",
                "generation_incomplete",
                "generation_failed",
                "generation_aborted",
            }:
                return kinds


async def _wait_for_generation(subscription: EventSubscription, attempt_id: str) -> list[str]:
    kinds: list[str] = []
    while True:
        event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        kinds.append(event.kind)
        if (
            event.payload.get("attempt_id") == attempt_id
            and event.kind
            in {
                "generation_completed",
                "generation_incomplete",
                "generation_failed",
                "generation_aborted",
            }
        ):
            return kinds


def test_edit_appends_user_revision_and_moves_active_branch(tmp_path):
    async def scenario():
        application = _application(tmp_path)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            first = await application.send_message(chat.id, "first")
            await _wait_for_terminal(subscription, first.id)
            second = await application.send_message(chat.id, "second")
            await _wait_for_terminal(subscription, second.id)
            _, before = await application.open_chat(chat.id)
            target = before[2]

            edited = await application.edit_message(chat.id, target.id, "edited second")
            event_kinds = await _wait_for_generation(subscription, edited.id)
            _, active = await application.open_chat(chat.id)
            history = await application.list_message_history(chat.id)
            _, original_branch = await application.open_chat(
                chat.id,
                head_message_id=before[-1].id,
            )

            assert [message.content for message in active] == [
                "first",
                "fake response to: first",
                "edited second",
                "fake response to: edited second",
            ]
            assert len(history) == 6
            replacement = active[2]
            assert replacement.lineage_id == target.lineage_id
            assert replacement.revision == 2
            assert replacement.supersedes_id == target.id
            assert target.content == "second"
            assert history[3].content == "fake response to: second"
            assert [message.content for message in original_branch] == [
                "first",
                "fake response to: first",
                "second",
                "fake response to: second",
            ]
            assert event_kinds[:3] == [
                "message_revision_created",
                "branch_head_changed",
                "generation_started",
            ]
            assert event_kinds[-1] == "generation_completed"
            with pytest.raises(StateError, match="active branch"):
                await application.edit_message(chat.id, target.id, "edited again")
        finally:
            await application.close()

    asyncio.run(scenario())


def test_application_close_reconciles_a_send_blocked_on_a_full_queue(tmp_path):
    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids, queue_size=1),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        subscription = application.subscribe()
        chat = await application.create_chat()
        send_task = asyncio.create_task(application.send_message(chat.id, "raced"))
        await asyncio.sleep(0)
        assert not send_task.done()

        await asyncio.wait_for(application.close(), timeout=1)
        with pytest.raises(StateError, match="closed"):
            await asyncio.wait_for(send_task, timeout=1)
        subscription.close()

        store = SQLiteAppStateStore.open(database)
        try:
            messages = store.list_messages(chat.id)
            attempts = store.list_generation_attempts(chat.id)
            assert messages[-1].state.value == "aborted"
            assert attempts[-1].state.value == "aborted"
        finally:
            store.close()

    asyncio.run(scenario())


def test_application_close_waits_for_an_inflight_create_command(tmp_path):
    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids, queue_size=1),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        subscription = application.subscribe()
        await application.create_chat("first")
        create_task = asyncio.create_task(application.create_chat("raced"))
        await asyncio.sleep(0)

        await asyncio.wait_for(application.close(), timeout=1)
        assert create_task.done()
        with pytest.raises(StateError, match="closed"):
            await create_task

        store = SQLiteAppStateStore.open(database)
        try:
            assert [chat.title for chat in store.list_chats()] == ["raced", "first"]
        finally:
            store.close()
        subscription.close()

    asyncio.run(scenario())


def test_restart_reconciles_a_persisted_running_generation(tmp_path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
    assistant = Message(
        "assistant",
        "chat",
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "partial",
        2,
        now,
        parent_id="user",
    )
    attempt = GenerationAttempt(
        "attempt",
        "chat",
        "user",
        "assistant",
        "fake",
        "fake-v0.1",
        AttemptState.RUNNING,
        "{}",
        now,
    )
    store = SQLiteAppStateStore.open(database)
    store.create_chat(chat)
    store.persist_generation_start(
        replace(chat, head_message_id=assistant.id, revision=1),
        user,
        assistant,
        attempt,
        expected_chat_revision=0,
    )
    store.close()

    ids = Uuid7Factory()
    application = BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(SystemClock(), ids),
        FakeStreamingBackend(),
        ids=ids,
        clock=SystemClock(),
    )
    try:
        assert application._store.get_message(assistant.id).state == MessageState.ABORTED
        assert application._store.list_generation_attempts(chat.id)[0].state == AttemptState.ABORTED
    finally:
        asyncio.run(application.close())


def test_closed_application_rejects_commands_queries_and_subscriptions(tmp_path):
    async def scenario():
        application = _application(tmp_path)
        chat = await application.create_chat()
        await application.close()

        with pytest.raises(StateError, match="closed"):
            application.subscribe()
        with pytest.raises(StateError, match="closed"):
            await application.create_chat()
        with pytest.raises(StateError, match="closed"):
            await application.list_chats()
        with pytest.raises(StateError, match="closed"):
            await application.open_chat(chat.id)

    asyncio.run(scenario())


def test_regeneration_creates_an_assistant_sibling_and_preserves_attempt(tmp_path):
    async def scenario():
        application = _application(tmp_path)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            original_attempt = await application.send_message(chat.id, "hello")
            await _wait_for_terminal(subscription, original_attempt.id)
            _, before = await application.open_chat(chat.id)
            original_assistant = before[-1]

            regenerated = await application.regenerate_message(chat.id, original_assistant.id)
            event_kinds = await _wait_for_terminal(subscription, regenerated.id)
            _, active = await application.open_chat(chat.id)
            history = await application.list_message_history(chat.id)
            revisions = await application.list_revisions(
                chat.id,
                original_assistant.lineage_id,
            )

            assert len(active) == 2
            assert active[-1].id != original_assistant.id
            assert active[-1].content == original_assistant.content
            assert active[-1].revision == 2
            assert active[-1].supersedes_id == original_assistant.id
            assert len(history) == 3
            assert [item.id for item in revisions] == [
                original_assistant.id,
                active[-1].id,
            ]
            assert regenerated.id != original_attempt.id
            assert event_kinds[-1] == "generation_completed"
            with pytest.raises(StateError, match="active branch"):
                await application.regenerate_message(chat.id, original_assistant.id)
        finally:
            await application.close()

    asyncio.run(scenario())


def test_restart_preserves_active_branch_history_and_lineage_revisions(tmp_path):
    async def scenario():
        application = _application(tmp_path)
        subscription = application.subscribe()
        chat_id: str
        try:
            chat = await application.create_chat()
            chat_id = chat.id
            first = await application.send_message(chat.id, "first")
            await _wait_for_terminal(subscription, first.id)
            second = await application.send_message(chat.id, "second")
            await _wait_for_terminal(subscription, second.id)
            _, before = await application.open_chat(chat.id)
            edited = await application.edit_message(chat.id, before[2].id, "edited")
            await _wait_for_terminal(subscription, edited.id)
            _, edited_branch = await application.open_chat(chat.id)
            regenerated = await application.regenerate_message(chat.id, edited_branch[-1].id)
            await _wait_for_terminal(subscription, regenerated.id)
            edited_user = (await application.open_chat(chat.id))[1][2]
            edited_assistant = (await application.open_chat(chat.id))[1][-1]
        finally:
            subscription.close()
            await application.close()

        restarted = _application(tmp_path)
        try:
            chat, active = await restarted.open_chat(chat_id)
            history = await restarted.list_message_history(chat_id)
            attempts = await restarted.list_generation_attempts(chat_id)
            user_revisions = await restarted.list_revisions(chat_id, edited_user.lineage_id)
            assistant_revisions = await restarted.list_revisions(
                chat_id,
                edited_assistant.lineage_id,
            )
            assert chat.head_message_id == active[-1].id
            assert chat.revision == 4
            assert [message.content for message in active] == [
                "first",
                "fake response to: first",
                "edited",
                "fake response to: edited",
            ]
            assert len(history) == 7
            assert len(attempts) == 4
            assert all(attempt.state.value == "complete" for attempt in attempts)
            assert [json.loads(attempt.request_snapshot)["prompt"] for attempt in attempts] == [
                "first",
                "second",
                "edited",
                "edited",
            ]
            assert [(item.revision, item.content) for item in user_revisions] == [
                (1, "second"),
                (2, "edited"),
            ]
            assert [(item.revision, item.content) for item in assistant_revisions] == [
                (1, "fake response to: edited"),
                (2, "fake response to: edited"),
            ]
        finally:
            await restarted.close()

    asyncio.run(scenario())
