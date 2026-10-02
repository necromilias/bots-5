"""
Phase 11 M3b: Duplicate chat with admission guard (A-1b Amendment B).

Tests for the amended duplicate_chat implementation that:
- Preserves message states without generation attempts
- Uses phase11_duplicate_messages admission guard
- Refuses chats with running (streaming) generations
- Does NOT copy generation_attempts/attempt_attachments/context_plans
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Set Qt platform before any Qt imports
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REPO = Path(__file__).resolve().parents[1]
HEAD = "0018_phase11_search_state"
PRIOR_HEAD = "0015_phase11_duplicate_admission"
# The revision that introduced the duplicate-admission guard.  Downgrading to the *previous*
# revision (the 0014 tombstone) is what must restore the pre-guard trigger text.
TOMBSTONE_HEAD = "0014_phase11_message_tombstone"


def _upgrade_to_revision(database: Path, revision: str) -> None:
    """Upgrade database to specified revision using raw alembic."""
    from tests._authority_test_support import upgrade_to
    upgrade_to(database, revision)


def _engine_with_transition_guard(database: Path):
    """Create an engine with the transition guard functions registered."""
    from sqlalchemy import create_engine, event
    from bots5.infrastructure.persistence.transition_guard import install_transition_guard

    engine = create_engine(f"sqlite:///{database}", future=True)
    event.listen(engine, "connect", install_transition_guard)
    return engine


def test_duplicate_chat_migration_and_schema_validation():
    """Test 1: Migration to 0015 and schema validation work correctly."""
    import tempfile
    import sqlite3
    with tempfile.TemporaryDirectory() as tmpdir:
        database = Path(tmpdir) / "state.sqlite3"

        # Upgrade to head (0015)
        _upgrade_to_revision(database, HEAD)

        # Verify the trigger exists and contains bots5_duplicate_message_allowed
        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_validate_insert'"
            )
            row = cursor.fetchone()
            assert row is not None, "messages_validate_insert trigger not found"
            trigger_sql = row[0]
            assert "bots5_duplicate_message_allowed" in trigger_sql, (
                f"Trigger should reference bots5_duplicate_message_allowed: {trigger_sql}"
            )
        finally:
            conn.close()

        # Verify Phase 9 schema validation passes
        engine = _engine_with_transition_guard(database)
        with engine.connect() as conn:
            from bots5.infrastructure.persistence.phase9_schema import validate_phase9_schema
            validate_phase9_schema(conn)

        engine.dispose()


def test_downgrade_restores_original_trigger():
    """Test 2: Downgrade from 0015 to 0014 restores original trigger."""
    import tempfile
    import sqlite3
    from sqlalchemy import create_engine
    from alembic import command
    from alembic.config import Config

    with tempfile.TemporaryDirectory() as tmpdir:
        database = Path(tmpdir) / "state.sqlite3"

        # Upgrade to head (0015)
        _upgrade_to_revision(database, HEAD)

        # Verify 0015 trigger has duplicate admission
        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_validate_insert'"
            )
            row = cursor.fetchone()
            assert row is not None
            trigger_0015 = row[0]
            assert "bots5_duplicate_message_allowed" in trigger_0015
        finally:
            conn.close()

        # Downgrade using alembic directly
        engine = create_engine(f"sqlite:///{database}", future=True)
        config = Config()
        config.set_main_option(
            "script_location",
            str(REPO / "src/bots5/infrastructure/persistence/migrations")
        )
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, TOMBSTONE_HEAD)
        engine.dispose()

        # Verify trigger no longer has duplicate admission
        conn = sqlite3.connect(database)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='messages_validate_insert'"
            )
            row = cursor.fetchone()
            assert row is not None, "messages_validate_insert trigger not found after downgrade"
            trigger_sql = row[0]
            assert "bots5_duplicate_message_allowed" not in trigger_sql, (
                f"Trigger should NOT reference bots5_duplicate_message_allowed after downgrade: {trigger_sql}"
            )
            # Should still have 'deleted' state from 0014
            assert "deleted" in trigger_sql
        finally:
            conn.close()

        # Note: validate_phase9_schema compares against phase9_schema.DDL which
        # contains the current (0015) schema. After downgrade to 0014, the database
        # schema no longer matches the current DDL, so we only verify the trigger
        # text directly as specified in the migration proof requirements.


# =============================================================================
# Amendment B: the actual duplicate contract (added by the supervisor)
#
# The tests below are the reason the 0015 migration exists. They duplicate a
# chat that really contains a terminal assistant message and assert the amended
# contract directly, rather than asserting it in a docstring.
# =============================================================================


def _application(tmp_path: Path, backend):
    from bots5.core.application import BotsApplication
    from bots5.core.events import EventBus
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from tests._authority_test_support import SQLiteAppStateStore, upgrade_database

    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    store = SQLiteAppStateStore.open(database)
    application = BotsApplication(
        store, EventBus(clock, ids, queue_size=64), backend, ids=ids, clock=clock
    )
    return store, application


class _BlockingBackend:
    """Streams one delta then blocks, so the assistant stays 'streaming'."""

    def __init__(self) -> None:
        import asyncio

        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request):
        from bots5.core.generation import GenerationDelta

        yield GenerationDelta(attempt_id=request.attempt_id, text="partial")
        self.started.set()
        await self.release.wait()


_TERMINAL_ATTEMPT_STATES = None


def _terminal_attempt_states():
    global _TERMINAL_ATTEMPT_STATES
    if _TERMINAL_ATTEMPT_STATES is None:
        from bots5.domain.models import AttemptState

        _TERMINAL_ATTEMPT_STATES = {
            AttemptState.COMPLETE,
            AttemptState.FAILED,
            AttemptState.INCOMPLETE,
            AttemptState.ABORTED,
        }
    return _TERMINAL_ATTEMPT_STATES


async def _await_terminal(application, chat_id: str):
    import asyncio

    for _ in range(500):
        rows = await application.list_generation_attempts(chat_id)
        if rows and rows[0].state in _terminal_attempt_states():
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError("generation never reached a terminal state")


async def _await_all_terminal(application, chat_id: str):
    """Wait until EVERY attempt of the chat reached a terminal state."""
    import asyncio

    for _ in range(500):
        rows = await application.list_generation_attempts(chat_id)
        if rows and all(row.state in _terminal_attempt_states() for row in rows):
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError("generation attempts never all reached a terminal state")


def test_duplicate_chat_preserves_message_contract_and_copies_no_attempts(tmp_path):
    """The amended contract: state preserved, and NO attempt rows created."""
    import asyncio

    from bots5.domain.models import AttemptState, MessageRole
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Source")
        await application.send_message(chat.id, "hello")
        attempt = await _await_terminal(application, chat.id)
        assert attempt.state is AttemptState.COMPLETE

        source_messages = store.list_messages(chat.id)
        assert [message.role for message in source_messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]
        assert source_messages[1].state.value == "complete"
        assert source_messages[1].content != ""

        new_chat, new_messages = await application.duplicate_chat(chat.id)

        assert new_chat.id != chat.id
        assert new_chat.title == "Source (copy)"

        # role/state/content/sequence/revision preserved, identities fresh
        assert len(new_messages) == len(source_messages)
        for source, duplicate in zip(source_messages, new_messages):
            assert duplicate.role is source.role
            assert duplicate.state is source.state
            assert duplicate.content == source.content
            assert duplicate.sequence == source.sequence
            assert duplicate.revision == source.revision
            assert duplicate.id != source.id
            assert duplicate.lineage_id != source.lineage_id
            assert duplicate.chat_id == new_chat.id

        # parent chain is remapped inside the copy
        assert new_messages[0].parent_id is None
        assert new_messages[1].parent_id == new_messages[0].id

        # the entire point of amendment B
        assert await application.list_generation_attempts(new_chat.id) == ()
        # ...while the source keeps its own attempt untouched
        assert len(await application.list_generation_attempts(chat.id)) == 1
        return new_chat

    new_chat = asyncio.run(scenario())

    with store.command_admission():
        with store.engine.connect() as connection:
            probes = {
                "generation_attempts": (
                    "SELECT COUNT(*) FROM generation_attempts WHERE chat_id = ?"
                ),
                "attempt_attachments": (
                    "SELECT COUNT(*) FROM attempt_attachments aa "
                    "JOIN generation_attempts ga ON ga.id = aa.attempt_id "
                    "WHERE ga.chat_id = ?"
                ),
                "context_plans": (
                    "SELECT COUNT(*) FROM context_plans cp "
                    "JOIN generation_attempts ga ON ga.id = cp.attempt_id "
                    "WHERE ga.chat_id = ?"
                ),
            }
            for label, sql in probes.items():
                count = connection.exec_driver_sql(sql, (new_chat.id,)).scalar_one()
                assert count == 0, f"{label} rows must not be copied"
    store.close()


def test_duplicate_chat_refuses_running_generation(tmp_path):
    """A chat with a streaming assistant message must be refused."""
    import asyncio

    from bots5.core.errors import StateError

    backend = _BlockingBackend()
    store, application = _application(tmp_path, backend)

    async def scenario():
        chat = await application.create_chat("Running")
        await application.send_message(chat.id, "hi")
        await backend.started.wait()
        try:
            with pytest.raises(StateError, match="running generation"):
                await application.duplicate_chat(chat.id)
        finally:
            backend.release.set()
            await _await_terminal(application, chat.id)

    try:
        asyncio.run(scenario())
    finally:
        store.close()


# =============================================================================
# F17 duplicate_chat contract cases (A-1b).
#
# These cases exercise sealed §4.9 / fork-register behaviour that the tests
# above do not cover: full-branch-tree copying with remapped relations, source
# immutability, Phase 5 per-chat configuration copy, list/rename visibility of
# the copy, the archived-source fork (A-1b-F2) and the unknown-id refusal.
# =============================================================================


def test_duplicate_full_branch_tree_copies_every_branch_with_remapped_relations(tmp_path):
    """The full-tree copy contains every branch and remaps parents/supersedes/lineages."""
    import asyncio

    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Branched")
        await application.send_message(chat.id, "q1")
        await _await_all_terminal(application, chat.id)
        _, active = await application.open_chat(chat.id)
        await application.regenerate_message(chat.id, active[-1].id)
        await _await_all_terminal(application, chat.id)
        await application.send_message(chat.id, "q2")
        await _await_all_terminal(application, chat.id)
        _, active = await application.open_chat(chat.id)
        history = await application.list_message_history(chat.id)
        assert len(history) > len(active)

        tree_chat, tree = await application.duplicate_chat(
            chat.id, include_full_branch_tree=True
        )

        # Feature-positive: the copy contains EVERY branch — including the
        # superseded first answer that is not on the active path.
        assert len(tree) == len(history)
        assert len(tree) > len(active)

        id_map = {source.id: copy.id for source, copy in zip(history, tree)}
        lineage_map: dict[str, str] = {}
        for source, copy in zip(history, tree):
            assert copy.id == id_map[source.id]
            assert copy.chat_id == tree_chat.id
            assert copy.role is source.role
            assert copy.state is source.state
            assert copy.content == source.content
            assert copy.sequence == source.sequence
            assert copy.parent_id == (
                None if source.parent_id is None else id_map[source.parent_id]
            )
            assert copy.supersedes_id == (
                None if source.supersedes_id is None else id_map[source.supersedes_id]
            )
            # A-1b-F5: lineage ids are fresh, and one source lineage stays one
            # (fresh) lineage in the copy.
            source_lineage = source.lineage_id or source.id
            if source_lineage in lineage_map:
                assert lineage_map[source_lineage] == copy.lineage_id
            else:
                lineage_map[source_lineage] = copy.lineage_id
            assert copy.lineage_id != source_lineage
        assert len(set(lineage_map.values())) == len(lineage_map)

        # The copy still carries no generation attempts.
        assert await application.list_generation_attempts(tree_chat.id) == ()

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_duplicate_leaves_source_chat_messages_attempts_and_search_state_unchanged(tmp_path):
    """Duplicate is non-destructive: source chat, messages, attempts and search docs stay put."""
    import asyncio

    from bots5.domain.search import SearchDocumentKind, SearchFilters
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Source Under Test")
        await application.send_message(chat.id, "source needle alpha")
        await _await_terminal(application, chat.id)
        source_chat = (await application.open_chat(chat.id))[0]
        source_messages = store.list_messages(chat.id)
        source_attempts = await application.list_generation_attempts(chat.id)
        assert source_attempts

        await application.duplicate_chat(chat.id)

        # Feature-positive: every piece of source state survives untouched.
        assert (await application.open_chat(chat.id))[0] == source_chat
        assert store.list_messages(chat.id) == source_messages
        assert await application.list_generation_attempts(chat.id) == source_attempts

        # The source's search documents are still found under the source ids.
        page = store.search(
            "alpha",
            filters=SearchFilters(document_kinds=(SearchDocumentKind.MESSAGE,)),
        )
        found = {result.document_id: result.chat_id for result in page.results}
        assert source_messages[0].id in found
        assert found[source_messages[0].id] == chat.id

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_duplicate_copies_model_selection_and_generation_config(tmp_path):
    """The per-chat Phase 5 selection and generation config ride along to the copy."""
    import asyncio

    from bots5.domain.provider import GenerationSettings
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Configured")
        entry_id = store.list_model_catalogue_entries()[0].id
        store.set_chat_model_selection(chat.id, entry_id)
        settings = GenerationSettings(
            temperature=0.25,
            max_output_tokens=256,
            reasoning_effort=None,
            timeout_seconds=None,
        )
        store.set_chat_model_generation_settings(chat.id, entry_id, settings)

        new_chat, _copied = await application.duplicate_chat(chat.id)

        # Feature-positive: the copy carries the same per-chat selection and
        # generation config.
        copied_selection = store.get_chat_model_selection(new_chat.id)
        assert copied_selection is not None
        assert copied_selection.model_entry_id == entry_id
        assert not copied_selection.selection_required
        assert store.get_chat_model_generation_settings(new_chat.id, entry_id) == settings

        # ...and the source still owns its own selection and config.
        assert store.get_chat_model_selection(chat.id).model_entry_id == entry_id
        assert store.get_chat_model_generation_settings(chat.id, entry_id) == settings

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_duplicate_appears_in_list_chats_and_copy_is_independently_renameable(tmp_path):
    """The duplicate is a first-class chat and renaming it never touches the source."""
    import asyncio

    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Original Name")
        new_chat, _copied = await application.duplicate_chat(chat.id)

        chats = await application.list_chats()
        # Feature-positive: list_chats exposes both chats with their own titles.
        assert {item.id for item in chats} == {chat.id, new_chat.id}
        titles = {item.id: item.title for item in chats}
        assert titles[chat.id] == "Original Name"
        assert titles[new_chat.id] == "Original Name (copy)"

        renamed = await application.rename_chat(new_chat.id, "Renamed Copy")
        assert renamed.title == "Renamed Copy"
        titles = {item.id: item.title for item in await application.list_chats()}
        assert titles[new_chat.id] == "Renamed Copy"
        assert titles[chat.id] == "Original Name"

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_duplicate_of_archived_chat_yields_active_copy_and_inherits_message_timestamps(tmp_path):
    """A-1b-F2/F8: archived source yields an active copy; message stamps are inherited."""
    import asyncio

    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Archived Source")
        await application.send_message(chat.id, "hello")
        await _await_terminal(application, chat.id)
        source_messages = store.list_messages(chat.id)
        await application.archive_chat(chat.id)

        new_chat, copied = await application.duplicate_chat(chat.id)

        # Feature-positive: the copy is active even though the source is archived.
        assert new_chat.archived_at is None

        # The source stays archived, exactly as it was.
        assert (await application.open_chat(chat.id))[0].archived_at is not None

        # The copy's creation stamp is fresh (not inherited from the source).
        assert new_chat.created_at > chat.created_at

        # A-1b-F8: copied messages inherit their historical timestamps, and
        # the copy's head follows the landed advance discipline (the copied
        # head message's own stamp).
        assert len(copied) == len(source_messages)
        for source, copy in zip(source_messages, copied):
            assert copy.created_at == source.created_at
        assert new_chat.updated_at == copied[-1].created_at

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_duplicate_refuses_unknown_chat_id_without_side_effects(tmp_path):
    """Duplicating an unknown chat id is refused and creates nothing."""
    import asyncio

    from bots5.core.errors import StateError
    from bots5.infrastructure.generation.fake import FakeStreamingBackend

    store, application = _application(tmp_path, FakeStreamingBackend())

    async def scenario():
        chat = await application.create_chat("Only Chat")

        # Feature-positive: the unknown id is refused with the typed error.
        with pytest.raises(StateError, match="chat not found"):
            await application.duplicate_chat("chat-that-does-not-exist")

        # The refusal created nothing and changed nothing.
        chats = await application.list_chats()
        assert [item.id for item in chats] == [chat.id]
        assert [item.title for item in chats] == ["Only Chat"]

    try:
        asyncio.run(scenario())
    finally:
        store.close()
