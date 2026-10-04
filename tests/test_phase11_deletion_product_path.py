"""Phase 11 M3 deletion product path: tombstones and whole-chat deletion.

Every test here drives the REAL product API (``SQLiteAppStateStore`` /
``BotsApplication``) — no raw SQL writes establish any feature state.  Raw SQL
appears only where the SCHEMA behaviour itself is the subject (the 0017 guard
triggers), mirroring the established threat-boundary test style of this
repository.

The deletion path lives on the ``0017_phase11_integrity`` schema, so fixtures
that exercise deletion upgrade explicitly to that supported terminal revision
before opening the store.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from tests._authority_test_support import SQLiteAppStateStore, downgrade_to, upgrade_to


HEAD_16 = "0016_phase11_workspace_state"
HEAD_17 = "0017_phase11_integrity"
HEAD_18 = "0020_provider_managed_context"  # the CURRENT head the open path migrates to (amendment 0019)
NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _store(tmp_path: Path, revision: str = HEAD_17) -> SQLiteAppStateStore:
    database = tmp_path / "state.sqlite3"
    upgrade_to(database, revision)
    return SQLiteAppStateStore.open(database)


def _chat(store, chat_id: str, title: str = "Chat"):
    from bots5.domain.models import Chat

    with store.command_admission():
        store.create_chat(Chat(chat_id, title, NOW, NOW))
    return store.get_chat(chat_id)


def _settled_exchange(store, chat_id: str, suffix: str, *, parent=None, user_text="secret words"):
    """Create one settled user+assistant pair through the product write path."""
    from bots5.domain.models import (
        AttemptState,
        GenerationAttempt,
        Message,
        MessageRole,
        MessageState,
    )

    chat = store.get_chat(chat_id)
    sequence = store.next_message_sequence(chat_id)
    user = Message(
        f"user-{suffix}",
        chat_id,
        MessageRole.USER,
        MessageState.SENT,
        user_text,
        sequence,
        NOW,
        parent_id=parent.id if parent is not None else None,
    )
    streaming = Message(
        f"assistant-{suffix}",
        chat_id,
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        sequence + 1,
        NOW,
        parent_id=user.id,
    )
    attempt = GenerationAttempt(
        f"attempt-{suffix}",
        chat_id,
        user.id,
        streaming.id,
        "fake",
        "fake-v0.1",
        AttemptState.RUNNING,
        "{}",
        NOW,
    )
    updated = replace(
        chat,
        updated_at=NOW,
        head_message_id=streaming.id,
        revision=chat.revision + 1,
    )
    with store.command_admission():
        store.persist_generation_start(
            updated,
            user,
            streaming,
            attempt,
            expected_chat_revision=chat.revision,
        )
        store.finalize_generation(
            replace(streaming, state=MessageState.COMPLETE, content=f"answer {suffix}"),
            replace(attempt, state=AttemptState.COMPLETE, ended_at=NOW),
        )
    return store.get_message(f"user-{suffix}"), store.get_message(f"assistant-{suffix}")


# ---------------------------------------------------------------------------
# F7 lossless per-message tombstone
# ---------------------------------------------------------------------------


def test_delete_message_tombstones_and_keeps_lineage(tmp_path):
    """The tombstone keeps identity, lineage, sequence and revision."""
    from bots5.domain.models import MessageState

    store = _store(tmp_path)
    try:
        _chat(store, "chat-1")
        user, assistant = _settled_exchange(store, "chat-1", "1")

        with store.command_admission():
            tombstone = store.delete_message("chat-1", user.id)

        assert tombstone.state is MessageState.DELETED
        assert tombstone.content == ""
        # Lossless structure: the row keeps its identity, lineage, sequence
        # and revision so the transcript can still show that something was
        # removed without losing continuity.
        assert tombstone.id == user.id
        assert tombstone.lineage_id == user.lineage_id
        assert tombstone.sequence == user.sequence
        assert tombstone.revision == user.revision
        assert tombstone.created_at == user.created_at

        # The tombstone is durable, and the other message is untouched.
        reloaded = store.get_message(user.id)
        assert reloaded.state is MessageState.DELETED
        assert reloaded.content == ""
        assert store.get_message(assistant.id).content == "answer 1"
        # The chat itself is untouched by a per-message tombstone.
        assert store.get_chat("chat-1") is not None
    finally:
        store.close()


def test_delete_message_refuses_streaming_unknown_and_repeat(tmp_path):
    from bots5.core.errors import StateError

    store = _store(tmp_path)
    try:
        _chat(store, "chat-1")
        user, assistant = _settled_exchange(store, "chat-1", "1")

        with store.command_admission():
            with pytest.raises(StateError, match="chat not found"):
                store.delete_message("chat-missing", user.id)
            with pytest.raises(StateError, match="message not found"):
                store.delete_message("chat-1", "message-that-does-not-exist")

        # A settled message can be deleted exactly once; the tombstone is
        # final and cannot be deleted again.
        with store.command_admission():
            store.delete_message("chat-1", user.id)
        with store.command_admission():
            with pytest.raises(StateError, match="already deleted"):
                store.delete_message("chat-1", user.id)

        # A streaming assistant message is refused: a message that is still
        # generating is not deletable mid-flight.
        from bots5.domain.models import (
            AttemptState,
            GenerationAttempt,
            Message,
            MessageRole,
            MessageState,
        )

        chat = store.get_chat("chat-1")
        sequence = store.next_message_sequence("chat-1")
        next_user = Message(
            f"user-2",
            "chat-1",
            MessageRole.USER,
            MessageState.SENT,
            "second",
            sequence,
            NOW,
            parent_id=assistant.id,
        )
        streaming = Message(
            "assistant-2",
            "chat-1",
            MessageRole.ASSISTANT,
            MessageState.STREAMING,
            "",
            sequence + 1,
            NOW,
            parent_id=next_user.id,
        )
        attempt = GenerationAttempt(
            "attempt-2", "chat-1", next_user.id, streaming.id,
            "fake", "fake-v0.1", AttemptState.RUNNING, "{}", NOW,
        )
        updated = replace(
            chat,
            updated_at=NOW,
            head_message_id=streaming.id,
            revision=chat.revision + 1,
        )
        with store.command_admission():
            store.persist_generation_start(
                updated, next_user, streaming, attempt,
                expected_chat_revision=chat.revision,
            )
        with store.command_admission():
            with pytest.raises(StateError, match="still generating"):
                store.delete_message("chat-1", streaming.id)
        assert store.get_message(streaming.id).state is MessageState.STREAMING
    finally:
        store.close()


def test_delete_message_advances_one_search_receipt_and_unindexes_content(tmp_path):
    """The tombstone arms/consumes exactly one source mutation and drains it."""
    from bots5.domain.search import SearchFilters

    store = _store(tmp_path)
    try:
        _chat(store, "chat-1")
        user, _assistant = _settled_exchange(
            store, "chat-1", "1", user_text="xylophone secret words"
        )
        status_before = store.search_status()
        found = store.search("xylophone", filters=SearchFilters())
        assert any(
            result.document_id == user.id for result in found.results
        ), "the message must be searchable before deletion"

        with store.command_admission():
            store.delete_message("chat-1", user.id)

        status_after = store.search_status()
        # Exactly one Phase 7 source mutation was consumed and the receipt
        # already drained, so the index is valid and up to date.
        assert status_after.source_revision == status_before.source_revision + 1
        assert status_after.checkpoint_revision == status_after.source_revision
        assert not [
            result for result in store.search("xylophone", filters=SearchFilters()).results
            if result.document_id == user.id
        ], "deleted content must leave the derived index"
    finally:
        store.close()


def test_deletion_path_requires_the_0017_schema(tmp_path):
    """The deletion path runs only on the 0017-or-later schema.

    While 0016 was the default head this asserted the fail-closed refusal of
    the tombstone update on a 0016 store.  With 0017 as the head, the normal
    open path migrates a 0016 database to 0017 before any store exists, so
    the schema requirement is enforced by startup itself and the tombstone
    update succeeds on the migrated schema.  0018 (and any later head) keeps
    inheriting the 0017 deletion triggers, so the open path now advances the
    seed all the way to the current head.
    """
    from sqlalchemy import text

    from bots5.domain.models import MessageState

    store = _store(tmp_path, revision=HEAD_16)
    try:
        # The 0016 seed was advanced by the normal open path — the deletion
        # path's schema requirement is structural now.
        with store.command_admission(), store.engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == HEAD_18

        _chat(store, "chat-1")
        user, assistant = _settled_exchange(store, "chat-1", "1")
        with store.command_admission():
            tombstone = store.delete_message("chat-1", user.id)
        assert tombstone.state is MessageState.DELETED
        assert tombstone.content == ""
        # Nothing else was lost: the other message survived the tombstone.
        assert store.get_message(assistant.id).content == "answer 1"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# F7 whole-chat deletion
# ---------------------------------------------------------------------------


def test_describe_chat_deletion_reports_the_loss_inventory(tmp_path):
    import asyncio

    from bots5.core.application import BotsApplication
    from bots5.core.events import EventBus
    from bots5.core.provider_configuration import ProviderConfiguration
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from bots5.domain.models import MessageState
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store = _store(tmp_path)
    ids = Uuid7Factory()
    clock = SystemClock()
    application = BotsApplication(
        store,
        EventBus(clock, ids),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        # Attachments require the configured (non-legacy) generation mode.
        configuration=ProviderConfiguration(store, ids, clock),
    )

    async def scenario():
        _chat(store, "chat-1", "Doomed")
        _chat(store, "chat-2", "Survivor")
        # One real attachment sent with a real message through the REAL
        # product flow (attach → stage → send), so the inventory counts an
        # actual message-attachment link row.
        attachment_source = tmp_path / "brief.txt"
        attachment_source.write_text("attachment body", encoding="utf-8")
        attachment = await application.attach_file(attachment_source)
        await application.stage_attachment("chat-1", attachment.id)
        # Configured mode sends require the chat's explicit model selection.
        with store.command_admission():
            store.set_chat_model_selection(
                "chat-1", store.list_model_catalogue_entries()[0].id
            )
        attempt = await application.send_message("chat-1", "first with attachment")
        for _ in range(500):
            state = store.get_message(attempt.assistant_message_id).state
            if state is MessageState.COMPLETE:
                break
            await asyncio.sleep(0.01)
        assert store.get_message(attempt.assistant_message_id).state is MessageState.COMPLETE
        _settled_exchange(store, "chat-1", "2", parent=store.get_message(attempt.assistant_message_id))

        with store.command_admission():
            inventory = await application.describe_chat_deletion("chat-1")

        assert inventory.chat_id == "chat-1"
        assert inventory.title == "Doomed"
        assert inventory.message_count == 4
        assert inventory.attachment_count == 1
        assert inventory.generation_attempt_count == 2
        # The read is side-effect free: nothing was deleted.
        assert len(store.list_messages("chat-1")) == 4

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(application.close())


def test_delete_chat_removes_the_whole_chat_and_keeps_other_chats(tmp_path):
    store = _store(tmp_path)
    try:
        _chat(store, "doomed", "Doomed")
        _chat(store, "survivor", "Survivor")
        _settled_exchange(store, "doomed", "1")
        _settled_exchange(store, "survivor", "2")

        revision = store.get_chat("doomed").revision
        with store.command_admission():
            store.delete_chat("doomed")

        assert store.get_chat("doomed") is None
        assert store.list_messages("doomed") == ()
        assert [chat.id for chat in store.list_chats()] == ["survivor"]
        # The survivor is completely intact.
        assert store.get_chat("survivor").title == "Survivor"
        assert len(store.list_messages("survivor")) == 2

        # The deletion is CAS-guarded and refuses unknown chats.
        with store.command_admission():
            with pytest.raises(Exception, match="chat not found"):
                store.delete_chat("doomed")
            with pytest.raises(Exception, match="chat revision changed"):
                store.delete_chat("survivor", expected_revision=revision + 42)
        assert store.get_chat("survivor") is not None
    finally:
        store.close()


def test_delete_chat_refuses_a_running_generation(tmp_path):
    from bots5.core.errors import StateError
    from bots5.domain.models import (
        AttemptState,
        GenerationAttempt,
        Message,
        MessageRole,
        MessageState,
    )

    store = _store(tmp_path)
    try:
        _chat(store, "chat-1")
        chat = store.get_chat("chat-1")
        user = Message("u1", "chat-1", MessageRole.USER, MessageState.SENT, "hi", 1, NOW)
        streaming = Message(
            "a1", "chat-1", MessageRole.ASSISTANT, MessageState.STREAMING, "", 2, NOW,
            parent_id="u1",
        )
        attempt = GenerationAttempt(
            "g1", "chat-1", "u1", "a1", "fake", "fake-v0.1",
            AttemptState.RUNNING, "{}", NOW,
        )
        updated = replace(chat, updated_at=NOW, head_message_id="a1", revision=1)
        with store.command_admission():
            store.persist_generation_start(updated, user, streaming, attempt, expected_chat_revision=0)

        with store.command_admission():
            with pytest.raises(StateError, match="running generation"):
                store.delete_chat("chat-1")
        # The chat and both messages survived the refusal.
        assert store.get_chat("chat-1") is not None
        assert store.get_message("u1").content == "hi"
        assert store.get_message("a1").state is MessageState.STREAMING
    finally:
        store.close()


def test_delete_chat_removes_drafts_and_model_selection_rows(tmp_path):
    """Nothing chat-owned may survive the whole-chat deletion."""
    from sqlalchemy import text

    store = _store(tmp_path)
    try:
        _chat(store, "doomed")
        with store.command_admission():
            store.save_chat_draft("doomed", "unsent words")

        with store.command_admission():
            store.delete_chat("doomed")

        with store.command_admission(), store.engine.connect() as connection:
            drafts = connection.execute(
                text("SELECT count(*) FROM chat_drafts WHERE chat_id = 'doomed'")
            ).scalar_one()
            selections = connection.execute(
                text("SELECT count(*) FROM chat_model_selection WHERE chat_id = 'doomed'")
            ).scalar_one()
            messages = connection.execute(
                text("SELECT count(*) FROM messages WHERE chat_id = 'doomed'")
            ).scalar_one()
        assert drafts == 0
        assert selections == 0
        assert messages == 0
    finally:
        store.close()


def test_delete_chat_advances_one_search_receipt(tmp_path):
    from bots5.domain.search import SearchFilters

    store = _store(tmp_path)
    try:
        _chat(store, "chat-1", "Searchable chat")
        _settled_exchange(store, "chat-1", "1", user_text="xylophone words")
        status_before = store.search_status()

        with store.command_admission():
            store.delete_chat("chat-1")

        status_after = store.search_status()
        assert status_after.source_revision > status_before.source_revision
        assert status_after.checkpoint_revision == status_after.source_revision
        remaining = store.search("xylophone", filters=SearchFilters())
        assert all(result.document_id != "user-1" for result in remaining.results)
        assert all(result.document_id != "chat-1" for result in remaining.results)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Application commands publish events (E01/E04 discipline)
# ---------------------------------------------------------------------------


def test_application_m3_commands_publish_their_events(tmp_path):
    import asyncio

    from bots5.core.application import BotsApplication
    from bots5.core.events import EventBus
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from bots5.domain.models import MessageState
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store = _store(tmp_path)
    seen: list[str] = []
    ids = Uuid7Factory()
    clock = SystemClock()
    application = BotsApplication(
        store, EventBus(clock, ids), FakeStreamingBackend(), ids=ids, clock=clock,
    )

    async def scenario():
        chat = await application.create_chat("Event chat")
        # One settled exchange through the store's product write path.
        chat_row = store.get_chat(chat.id)
        sequence = store.next_message_sequence(chat.id)
        from bots5.domain.models import (
            AttemptState,
            GenerationAttempt,
            Message,
            MessageRole,
        )
        user = Message("u1", chat.id, MessageRole.USER, MessageState.SENT, "hello", sequence, NOW)
        streaming = Message(
            "a1", chat.id, MessageRole.ASSISTANT, MessageState.STREAMING, "", sequence + 1, NOW,
            parent_id="u1",
        )
        attempt = GenerationAttempt(
            "g1", chat.id, "u1", "a1", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", NOW,
        )
        updated = replace(chat_row, updated_at=NOW, head_message_id="a1", revision=1)
        with store.command_admission():
            store.persist_generation_start(updated, user, streaming, attempt, expected_chat_revision=0)
            store.finalize_generation(
                replace(streaming, state=MessageState.COMPLETE, content="done"),
                replace(attempt, state=AttemptState.COMPLETE, ended_at=NOW),
            )

        subscription = application._events.subscribe()
        try:
            folder = await application.create_folder("Work")
            await application.rename_folder(folder.id, "Work stuff")
            await application.set_chat_folder(chat.id, folder.id)
            await application.set_chat_pinned(chat.id, True)
            await application.delete_message(chat.id, "u1")
            inventory = await application.describe_chat_deletion(chat.id)
            assert inventory.message_count == 2
            await application.delete_folder(folder.id)
            await application.delete_chat(chat.id)
            # Let the awaited deliveries land on the subscription queue.
            await asyncio.sleep(0)
            while not subscription._queue.empty():
                event = subscription._queue.get_nowait()
                if event is not None:
                    seen.append(event.kind)
        finally:
            subscription.close()
        await application.close()

    asyncio.run(scenario())
    assert seen == [
        "folder_created",
        "folder_renamed",
        "chat_folder_changed",
        "chat_pin_changed",
        "message_deleted",
        "folder_deleted",
        "chat_deleted",
    ], seen


# ---------------------------------------------------------------------------
# Fail-closed guard without the 0017 tombstone admission
# ---------------------------------------------------------------------------


def test_deletion_fails_closed_without_the_0017_tombstone_admission(tmp_path):
    """Without 0017's tombstone admission, deletion must refuse with a TYPED error.

    Round 11 advanced the default head to 0017, which made the original fail-closed
    scenario (a pre-0017 store refusing the tombstone) unreachable through the normal
    open path, because opening auto-migrates.  That left the guard itself -- the
    ``StateError`` naming ``0017_phase11_integrity`` -- with no coverage at all.

    This restores it by reinstating the PRE-0017 definition of ``messages_validate_update``
    on an otherwise-0017 database, so the schema is exactly the one the guard defends
    against, and asserting the guard refuses rather than leaking a raw DB error.
    """
    import importlib.util

    migration_path = (
        Path(__file__).resolve().parent.parent
        / "src/bots5/infrastructure/persistence/migrations/versions/0017_phase11_integrity.py"
    )
    spec = importlib.util.spec_from_file_location("_m0017_integrity", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    store = _store(tmp_path)
    try:
        _chat(store, "chat-1")
        user, _assistant = _settled_exchange(store, "chat-1", "1")
    finally:
        store.close()

    # The trigger swap must happen on a RAW connection BEFORE the store is opened: the
    # rooted authority refuses DDL through a live store connection ("not authorized").
    import sqlite3

    raw = sqlite3.connect(tmp_path / "state.sqlite3")
    try:
        raw.execute("DROP TRIGGER IF EXISTS messages_validate_update")
        raw.execute(migration._ORIGINAL_VALIDATE_UPDATE)
        raw.commit()
    finally:
        raw.close()

    store = _store(tmp_path)
    try:
        with store.command_admission():
            with pytest.raises(Exception) as excinfo:
                store.delete_message("chat-1", user.id)

        message = str(excinfo.value)
        assert "0017_phase11_integrity" in message, (
            f"the guard must name the required revision, said: {message}"
        )
    finally:
        store.close()
