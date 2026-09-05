from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import DatabaseError

from bots5.core.errors import RevisionConflict, StateError
from bots5.domain.clock import parse_utc
from bots5.domain.models import AttemptState, Chat, GenerationAttempt, Message, MessageRole, MessageState
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database
from bots5.infrastructure.persistence import migration_runner
from bots5.infrastructure.persistence.migration_runner import _create_recovery_point


REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "src/bots5/infrastructure/persistence/migrations"


def _upgrade_to(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, revision)


def test_phase2_schema_has_lineage_columns_and_is_idempotent(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        columns = {column["name"] for column in inspect(store.engine).get_columns("messages")}
        assert {"lineage_id", "revision", "supersedes_id"} <= columns
        chat_columns = {column["name"] for column in inspect(store.engine).get_columns("chats")}
        assert {"head_message_id", "revision"} <= chat_columns
        with store.engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006_phase4_workspace"
        foreign_keys = inspect(store.engine).get_foreign_keys("chats")
        assert any(
            foreign_key["referred_table"] == "messages"
            and foreign_key["constrained_columns"] == ["head_message_id"]
            for foreign_key in foreign_keys
        )
    finally:
        store.close()


def test_phase2_backfills_a_phase1_linear_database(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO chats (id, title, created_at, updated_at) VALUES (:id, :title, :created, :updated)"),
                {
                    "id": "chat",
                    "title": "Legacy",
                    "created": "2026-09-03T00:00:00.000Z",
                    "updated": "2026-09-03T00:00:00.000Z",
                },
            )
            connection.execute(
                text("INSERT INTO chats (id, title, created_at, updated_at) VALUES ('empty-chat', 'Empty', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')")
            )
            connection.execute(
                text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES (:id, :chat, :parent, :sequence, :role, :state, :content, :created)"),
                [
                    {"id": "u1", "chat": "chat", "parent": None, "sequence": 1, "role": "user", "state": "sent", "content": "one", "created": "2026-09-03T00:00:00.000Z"},
                    {"id": "a1", "chat": "chat", "parent": "u1", "sequence": 2, "role": "assistant", "state": "complete", "content": "answer one", "created": "2026-09-03T00:00:00.000Z"},
                    {"id": "u2", "chat": "chat", "parent": None, "sequence": 3, "role": "user", "state": "sent", "content": "two", "created": "2026-09-03T00:00:00.000Z"},
                    {"id": "a2", "chat": "chat", "parent": "u2", "sequence": 4, "role": "assistant", "state": "complete", "content": "answer two", "created": "2026-09-03T00:00:00.000Z"},
                ],
            )
            connection.execute(
                text("INSERT INTO generation_attempts (id, chat_id, user_message_id, assistant_message_id, backend_id, model, state, request_snapshot, started_at, ended_at) VALUES (:id, :chat, :user, :assistant, 'fake', 'fake-v0.1', 'complete', '{}', :started, :ended)"),
                [
                    {"id": "attempt-1", "chat": "chat", "user": "u1", "assistant": "a1", "started": "2026-09-03T00:00:00.000Z", "ended": "2026-09-03T00:00:01.000Z"},
                    {"id": "attempt-2", "chat": "chat", "user": "u2", "assistant": "a2", "started": "2026-09-03T00:00:00.000Z", "ended": "2026-09-03T00:00:01.000Z"},
                ],
            )
    finally:
        engine.dispose()

    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        chat, branch = (store.get_chat("chat"), store.list_branch_messages("chat"))
        assert chat is not None
        assert chat.head_message_id == "a2"
        assert chat.revision == 4
        assert [message.id for message in branch] == ["u1", "a1", "u2", "a2"]
        assert branch[2].parent_id == "a1"
        assert all(message.lineage_id == message.id for message in branch)
        assert all(message.revision == 1 for message in branch)
        assert parse_utc("2026-09-03T00:00:00.000Z") == branch[0].created_at
        assert (tmp_path / ".legacy.sqlite3.pre-migration").is_file()
        assert (tmp_path / ".legacy.sqlite3.pre-migration.json").is_file()
        empty_chat = store.get_chat("empty-chat")
        assert empty_chat is not None
        assert empty_chat.head_message_id is None
        assert empty_chat.revision == 0
    finally:
        store.close()


def test_migration_rejects_invalid_legacy_lineage_before_ddl_and_is_retryable(tmp_path: Path):
    database = tmp_path / "invalid.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO chats (id, title, created_at, updated_at) VALUES ('chat', 'Chat', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')")
            )
            connection.execute(
                text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES ('u1', 'chat', NULL, 1, 'user', 'sent', 'one', '2026-09-03T00:00:00.000Z')")
            )
            connection.execute(
                text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES ('a1', 'chat', 'u1', 2, 'assistant', 'complete', 'answer', '2026-09-03T00:00:00.000Z')")
            )
            connection.execute(
                text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES ('u2', 'chat', 'u1', 3, 'user', 'sent', 'invalid', '2026-09-03T00:00:00.000Z')")
            )
    finally:
        engine.dispose()

    for _ in range(2):
        with pytest.raises(RuntimeError, match="cannot backfill conversation lineage"):
            upgrade_database(database)
        check_engine = create_engine(f"sqlite:///{database}")
        try:
            assert {
                column["name"] for column in inspect(check_engine).get_columns("messages")
            } == {
                "id", "chat_id", "parent_id", "sequence", "role", "state", "content", "created_at"
            }
            with check_engine.connect() as connection:
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001_desktop_state"
        finally:
            check_engine.dispose()


def test_migration_rejects_duplicate_generation_attempts_before_ddl(tmp_path: Path):
    database = tmp_path / "duplicate-attempts.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO chats (id, title, created_at, updated_at) VALUES ('chat', 'Chat', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')")
            )
            connection.execute(
                text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES ('u1', 'chat', NULL, 1, 'user', 'sent', 'one', '2026-09-03T00:00:00.000Z')")
            )
            connection.execute(
                text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES ('a1', 'chat', 'u1', 2, 'assistant', 'complete', 'answer', '2026-09-03T00:00:00.000Z')")
            )
            for attempt_id in ("attempt-1", "attempt-2"):
                    connection.execute(
                        text("INSERT INTO generation_attempts (id, chat_id, user_message_id, assistant_message_id, backend_id, model, state, request_snapshot, started_at, ended_at) VALUES (:id, 'chat', 'u1', 'a1', 'fake', 'fake-v0.1', 'complete', '{}', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:01.000Z')"),
                    {"id": attempt_id},
                )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="multiple generation attempts"):
        upgrade_database(database)
    check_engine = create_engine(f"sqlite:///{database}")
    try:
        assert {
            column["name"] for column in inspect(check_engine).get_columns("messages")
        } == {
            "id", "chat_id", "parent_id", "sequence", "role", "state", "content", "created_at"
        }
    finally:
        check_engine.dispose()


def test_upgrade_refuses_an_unknown_newer_schema(tmp_path: Path):
    database = tmp_path / "future.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '9999_future'"))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="newer or unsupported"):
        upgrade_database(database)


def test_stale_recovery_point_is_rejected(tmp_path: Path):
    database = tmp_path / "stale.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO chats (id, title, created_at, updated_at) VALUES ('before', 'Before', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')")
            )
    finally:
        engine.dispose()
    _create_recovery_point(database)

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO chats (id, title, created_at, updated_at) VALUES ('after', 'After', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')")
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="does not match"):
        _create_recovery_point(database)


def test_database_rejects_cross_chat_lineage_reference(tmp_path: Path):
    database = tmp_path / "cross-chat.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        with pytest.raises(DatabaseError, match="same chat"):
            with store.engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES ('chat-1', 'One', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0), ('chat-2', 'Two', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0)")
                )
                connection.execute(
                    text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) VALUES ('m1', 'chat-1', NULL, 1, 'user', 'sent', 'one', '2026-09-03T00:00:00.000Z', 'm1', 1)")
                )
                connection.execute(
                    text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) VALUES ('m2', 'chat-2', 'm1', 1, 'user', 'sent', 'two', '2026-09-03T00:00:00.000Z', 'm2', 1)")
                )
    finally:
        store.close()


def test_store_rejects_contradictory_supersedes_lineage_revision(tmp_path: Path):
    database = tmp_path / "invalid-lineage.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
    assistant = Message(
        "assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "answer", 2, now, parent_id="user"
    )
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.COMPLETE, "{}", now, ended_at=now
    )
    store.create_chat(chat)
    store.persist_generation_start(
        replace(chat, head_message_id="assistant", revision=1),
        user,
        replace(assistant, state=MessageState.STREAMING),
        replace(attempt, state=AttemptState.RUNNING, ended_at=None),
        expected_chat_revision=0,
    )
    invalid = Message(
        "replacement",
        "chat",
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        3,
        now,
        parent_id="user",
        lineage_id="different-lineage",
        revision=2,
        supersedes_id="assistant",
    )
    invalid_attempt = GenerationAttempt(
        "invalid-attempt", "chat", "user", "replacement", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    try:
        with pytest.raises(StateError, match="share the message lineage"):
            store.persist_regeneration_start(
                replace(chat, head_message_id="replacement", revision=2),
                invalid,
                invalid_attempt,
                expected_chat_revision=1,
            )
        assert [message.id for message in store.list_messages("chat")] == ["user", "assistant"]
    finally:
        store.close()


def test_store_rejects_role_swapped_generation_attempt(tmp_path: Path):
    database = tmp_path / "invalid-attempt.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    assistant_as_user = Message(
        "user", "chat", MessageRole.ASSISTANT, MessageState.SENT, "hello", 1, now
    )
    user_as_assistant = Message(
        "assistant", "chat", MessageRole.USER, MessageState.STREAMING, "", 2, now, parent_id="user"
    )
    swapped_attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    try:
        store.create_chat(chat)
        with pytest.raises(StateError, match="user message"):
            store.persist_generation_start(
                replace(chat, head_message_id="assistant", revision=1),
                assistant_as_user,
                user_as_assistant,
                swapped_attempt,
                expected_chat_revision=0,
            )
        assert store.list_messages("chat") == ()
        assert store.list_generation_attempts("chat") == ()
    finally:
        store.close()


def test_current_head_rejects_a_lineage_cycle_on_open(tmp_path: Path):
    database = tmp_path / "lineage-cycle.sqlite3"
    upgrade_database(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    store = SQLiteAppStateStore.open(database)
    chat = Chat("chat", "Chat", now, now)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
    assistant = Message(
        "assistant",
        "chat",
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        2,
        now,
        parent_id="user",
    )
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    store.create_chat(chat)
    store.persist_generation_start(
        replace(chat, head_message_id="assistant", revision=1),
        user,
        assistant,
        attempt,
        expected_chat_revision=0,
    )
    store.finalize_generation(
        replace(assistant, state=MessageState.COMPLETE, content="answer"),
        replace(attempt, state=AttemptState.COMPLETE, ended_at=now),
    )
    store.close()

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER messages_parent_same_chat")
        connection.execute("DROP TRIGGER messages_validate_insert")
        connection.execute("DROP TRIGGER messages_timestamps_insert")
        connection.execute("DROP TRIGGER messages_active_head_parent_guard")
        connection.execute(
            "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) "
            "VALUES ('cycle', 'chat', 'cycle', 3, 'user', 'sent', 'bad', "
            "'2026-09-03T00:00:00.000Z', 'cycle', 1)"
        )

    with pytest.raises(RuntimeError, match="cannot parent itself"):
        SQLiteAppStateStore.open(database)


def test_current_head_revision_shape_is_rejected_on_open(tmp_path: Path):
    now = datetime(2026, 9, 3, tzinfo=UTC)

    def seed_database(database: Path) -> None:
        upgrade_database(database)
        store = SQLiteAppStateStore.open(database)
        chat = Chat("chat", "Chat", now, now)
        user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
        assistant = Message(
            "assistant",
            "chat",
            MessageRole.ASSISTANT,
            MessageState.STREAMING,
            "",
            2,
            now,
            parent_id="user",
        )
        attempt = GenerationAttempt(
            "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
        )
        store.create_chat(chat)
        store.persist_generation_start(
            replace(chat, head_message_id="assistant", revision=1),
            user,
            assistant,
            attempt,
            expected_chat_revision=0,
        )
        store.finalize_generation(
            replace(assistant, state=MessageState.COMPLETE, content="answer"),
            replace(attempt, state=AttemptState.COMPLETE, ended_at=now),
        )
        store.create_chat(Chat("empty", "Empty", now, now))
        store.close()

    cases = {
        "nonnull-head-revision-zero": (
            "active head with zero revision",
            ("DROP TRIGGER chats_revision_monotonic", "UPDATE chats SET revision = 0 WHERE id = 'chat'"),
        ),
        "null-head-revision-one": (
            "nonzero revision without an active head",
            (
                "DROP TRIGGER chats_head_update_guard",
                "UPDATE chats SET head_message_id = NULL WHERE id = 'chat'",
            ),
        ),
        "empty-chat-revision-one": (
            "nonzero revision without an active head",
            (
                "DROP TRIGGER chats_revision_monotonic",
                "UPDATE chats SET revision = 1 WHERE id = 'empty'",
            ),
        ),
    }
    for name, (error, statements) in cases.items():
        database = tmp_path / f"{name}.sqlite3"
        seed_database(database)
        with sqlite3.connect(database) as connection:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        with pytest.raises(RuntimeError, match=error):
            SQLiteAppStateStore.open(database)


def test_terminal_message_cannot_be_rewritten(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO chats (id, title, created_at, updated_at, revision) VALUES ('chat', 'Chat', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z', 0)")
                )
                connection.execute(
                    text("INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) VALUES ('message', 'chat', NULL, 1, 'user', 'sent', 'original', '2026-09-03T00:00:00.000Z', 'message', 1)")
                )
                connection.execute(
                    text("UPDATE messages SET content = 'rewritten' WHERE id = 'message'")
                )
    finally:
        store.close()


def test_stale_chat_revision_rolls_back_the_new_lineage_nodes(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    first_user = Message(
        "u1", "chat", MessageRole.USER, MessageState.SENT, "one", 1, now
    )
    first_assistant = Message(
        "a1",
        "chat",
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        2,
        now,
        parent_id="u1",
    )
    first_attempt = GenerationAttempt(
        "attempt-1",
        "chat",
        "u1",
        "a1",
        "fake",
        "fake-v0.1",
        AttemptState.RUNNING,
        "{}",
        now,
    )
    try:
        store.create_chat(chat)
        store.persist_generation_start(
            replace(chat, head_message_id="a1", revision=1),
            first_user,
            first_assistant,
            first_attempt,
            expected_chat_revision=0,
        )
        second_user = Message(
            "u2", "chat", MessageRole.USER, MessageState.SENT, "two", 3, now, parent_id="a1"
        )
        second_assistant = Message(
            "a2",
            "chat",
            MessageRole.ASSISTANT,
            MessageState.STREAMING,
            "",
            4,
            now,
            parent_id="u2",
        )
        second_attempt = GenerationAttempt(
            "attempt-2",
            "chat",
            "u2",
            "a2",
            "fake",
            "fake-v0.1",
            AttemptState.RUNNING,
            "{}",
            now,
        )
        with pytest.raises(StateError, match="active generation"):
            store.persist_generation_start(
                replace(chat, head_message_id="a2", revision=1),
                second_user,
                second_assistant,
                second_attempt,
                expected_chat_revision=0,
            )
        assert [message.id for message in store.list_messages("chat")] == ["u1", "a1"]
        assert [attempt.id for attempt in store.list_generation_attempts("chat")] == [
            "attempt-1"
        ]
    finally:
        store.close()


def test_terminal_message_and_attempt_finalize_in_one_transaction(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
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
    store.create_chat(chat)
    store.persist_generation_start(
        replace(chat, head_message_id="assistant", revision=1),
        user,
        assistant,
        attempt,
        expected_chat_revision=0,
    )
    terminal_message = replace(assistant, state=MessageState.COMPLETE, content="answer")
    terminal_attempt = replace(attempt, state=AttemptState.COMPLETE, ended_at=now)

    def fail_attempt_update(connection, cursor, statement, parameters, context, executemany):
        if "UPDATE generation_attempts" in statement:
            raise RuntimeError("injected attempt update failure")

    event.listen(store.engine, "before_cursor_execute", fail_attempt_update)
    try:
        with pytest.raises(RuntimeError, match="injected attempt update failure"):
            store.finalize_generation(terminal_message, terminal_attempt)
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_attempt_update)

    assert store.get_message("assistant").state == MessageState.STREAMING
    assert store.get_message("assistant").content == "partial"
    assert store.list_generation_attempts("chat")[0].state == AttemptState.RUNNING

    store.finalize_generation(terminal_message, terminal_attempt)
    assert store.get_message("assistant").state == MessageState.COMPLETE
    assert store.list_generation_attempts("chat")[0].state == AttemptState.COMPLETE
    store.close()


def test_sqlite_rejects_invalid_states_sequences_and_chat_revision_jumps(tmp_path: Path):
    database = tmp_path / "integrity.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
    assistant = Message(
        "assistant", "chat", MessageRole.ASSISTANT, MessageState.STREAMING, "", 2, now, parent_id="user"
    )
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    try:
        store.create_chat(chat)
        store.persist_generation_start(
            replace(chat, head_message_id="assistant", revision=1),
            user,
            assistant,
            attempt,
            expected_chat_revision=0,
        )
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) "
                        "VALUES ('bad', 'chat', NULL, 0, 'user', 'sent', '', '2026-09-03T00:00:00.000Z', 'bad', 1)"
                    )
                )
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(text("UPDATE chats SET revision = 9 WHERE id = 'chat'"))
        assert store.get_chat("chat").revision == 1
    finally:
        store.close()


def test_sqlite_rejects_unpaired_terminal_transitions(tmp_path: Path):
    database = tmp_path / "lifecycle.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
    assistant = Message(
        "assistant", "chat", MessageRole.ASSISTANT, MessageState.STREAMING, "", 2, now, parent_id="user"
    )
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    try:
        store.create_chat(chat)
        store.persist_generation_start(
            replace(chat, head_message_id="assistant", revision=1),
            user,
            assistant,
            attempt,
            expected_chat_revision=0,
        )
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(text("UPDATE messages SET state = 'complete' WHERE id = 'assistant'"))
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE generation_attempts SET state = 'complete', ended_at = '2026-09-03T00:00:01.000Z' "
                        "WHERE id = 'attempt'"
                    )
                )
        assert store.get_message("assistant").state == MessageState.STREAMING
        assert store.list_generation_attempts("chat")[0].state == AttemptState.RUNNING
    finally:
        store.close()


def test_raw_sqlite_cannot_arm_or_bypass_the_transition_guard(tmp_path: Path):
    database = tmp_path / "raw-guard.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, now)
    assistant = Message(
        "assistant", "chat", MessageRole.ASSISTANT, MessageState.STREAMING, "", 2, now, parent_id="user"
    )
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    store.create_chat(chat)
    store.persist_generation_start(
        replace(chat, head_message_id=assistant.id, revision=1),
        user,
        assistant,
        attempt,
        expected_chat_revision=0,
    )
    with pytest.raises(DatabaseError):
        with store.engine.begin() as connection:
            connection.execute(
                text("UPDATE chats SET updated_at = '2026-09-03T24:01:00.000Z' WHERE id = 'chat'")
            )
    with pytest.raises(DatabaseError, match="active head"):
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) "
                    "VALUES ('raw-user', 'chat', 'assistant', 3, 'user', 'sent', 'bad', "
                    "'2026-09-03T00:00:00.000Z', 'raw-user', 1)"
                )
            )
    assert store.get_message("raw-user") is None
    store.close()

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE generation_attempts SET state='complete', ended_at='2026-09-03T00:00:01.000Z' "
                "WHERE id='attempt'"
            )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE messages SET state='complete' WHERE id='assistant'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO messages (id, chat_id, parent_id, sequence, role, state, content, created_at, lineage_id, revision) "
                "VALUES ('other-assistant', 'chat', 'user', 3, 'assistant', 'streaming', '', "
                "'2026-09-03T00:00:00.000Z', 'other-assistant', 1)"
            )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM generation_attempts WHERE id = 'attempt'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM messages WHERE id = 'assistant'")


def test_finalize_generation_rejects_a_message_from_another_attempt(tmp_path: Path):
    database = tmp_path / "paired-finalize.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    now = datetime(2026, 9, 3, tzinfo=UTC)
    chat = Chat("chat", "Chat", now, now)
    first_user = Message("u1", "chat", MessageRole.USER, MessageState.SENT, "one", 1, now)
    first_assistant = Message(
        "a1", "chat", MessageRole.ASSISTANT, MessageState.STREAMING, "", 2, now, parent_id="u1"
    )
    first_attempt = GenerationAttempt(
        "g1", "chat", "u1", "a1", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    second_user = Message("u2", "chat", MessageRole.USER, MessageState.SENT, "two", 3, now, parent_id="a1")
    second_assistant = Message(
        "a2", "chat", MessageRole.ASSISTANT, MessageState.STREAMING, "", 4, now, parent_id="u2"
    )
    second_attempt = GenerationAttempt(
        "g2", "chat", "u2", "a2", "fake", "fake-v0.1", AttemptState.RUNNING, "{}", now
    )
    try:
        store.create_chat(chat)
        store.persist_generation_start(
            replace(chat, head_message_id="a1", revision=1),
            first_user,
            first_assistant,
            first_attempt,
            expected_chat_revision=0,
        )
        store.finalize_generation(
            replace(first_assistant, state=MessageState.COMPLETE, content="done"),
            replace(first_attempt, state=AttemptState.COMPLETE, ended_at=now, finish_reason="stop"),
        )
        store.persist_generation_start(
            replace(chat, head_message_id="a2", revision=2),
            second_user,
            second_assistant,
            second_attempt,
            expected_chat_revision=1,
        )
        with pytest.raises(StateError, match="message is not streaming|does not belong"):
            store.finalize_generation(
                replace(first_assistant, state=MessageState.COMPLETE),
                replace(second_attempt, state=AttemptState.COMPLETE, ended_at=now),
            )
        assert store.get_message("a1").state == MessageState.COMPLETE
        assert store.list_generation_attempts("chat")[1].state == AttemptState.RUNNING
    finally:
        store.close()


def test_upgrade_from_existing_phase2_revision_installs_integrity_boundary(tmp_path: Path):
    database = tmp_path / "phase2.sqlite3"
    _upgrade_to(database, "0002_conversation_lineage")
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        with store.engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006_phase4_workspace"
            assert connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name = 'messages_validate_insert'")
            ).scalar_one() == 1
    finally:
        store.close()


def test_failed_integrity_migration_restores_the_recovery_point_for_retry(tmp_path: Path):
    database = tmp_path / "retry.sqlite3"
    _upgrade_to(database, "0002_conversation_lineage")
    seen = False

    def fail_integrity_trigger(connection, cursor, statement, parameters, context, executemany):
        nonlocal seen
        if not seen and "CREATE TRIGGER generation_attempt_validate_insert" in statement:
            seen = True
            raise RuntimeError("injected integrity migration failure")

    event.listen(Engine, "before_cursor_execute", fail_integrity_trigger)
    try:
        with pytest.raises(RuntimeError, match="injected integrity migration failure"):
            upgrade_database(database)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_integrity_trigger)

    check_engine = create_engine(f"sqlite:///{database}")
    try:
        with check_engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_conversation_lineage"
            assert "lifecycle_transitions" not in inspect(check_engine).get_table_names()
    finally:
        check_engine.dispose()
    upgrade_database(database)


def test_recovery_point_reconstructs_metadata_after_second_replace_failure(tmp_path: Path, monkeypatch):
    database = tmp_path / "recovery.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at) VALUES "
                    "('chat', 'Chat', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')"
                )
            )
    finally:
        engine.dispose()

    original_replace = migration_runner.os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected metadata replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(migration_runner.os, "replace", fail_second_replace)
    with pytest.raises(RuntimeError, match="cannot create verified recovery point"):
        _create_recovery_point(database)
    recovery_point = tmp_path / ".recovery.sqlite3.pre-migration"
    metadata_path = tmp_path / ".recovery.sqlite3.pre-migration.json"
    temporary_metadata = tmp_path / "..recovery.sqlite3.pre-migration.json.tmp"
    assert recovery_point.is_file()
    assert not metadata_path.exists()
    assert temporary_metadata.is_file()

    monkeypatch.setattr(migration_runner.os, "replace", original_replace)
    _create_recovery_point(database)
    assert metadata_path.is_file()
    assert not temporary_metadata.exists()


def test_recovery_point_retries_after_first_replace_failure(tmp_path: Path, monkeypatch):
    database = tmp_path / "first-replace.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at) VALUES "
                    "('chat', 'Chat', '2026-09-03T00:00:00.000Z', '2026-09-03T00:00:00.000Z')"
                )
            )
    finally:
        engine.dispose()

    original_replace = migration_runner.os.replace
    calls = 0

    def fail_first_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected recovery replace failure")
        return original_replace(source, destination)

    recovery_point = tmp_path / ".first-replace.sqlite3.pre-migration"
    metadata_path = tmp_path / ".first-replace.sqlite3.pre-migration.json"
    temporary_metadata = tmp_path / "..first-replace.sqlite3.pre-migration.json.tmp"
    monkeypatch.setattr(migration_runner.os, "replace", fail_first_replace)
    with pytest.raises(RuntimeError, match="cannot create verified recovery point"):
        _create_recovery_point(database)
    assert not recovery_point.exists()
    assert not metadata_path.exists()
    assert not temporary_metadata.exists()

    monkeypatch.setattr(migration_runner.os, "replace", original_replace)
    _create_recovery_point(database)
    assert recovery_point.is_file()
    assert metadata_path.is_file()
