"""Deterministic regression oracles for the fresh Phase 7 V3 findings."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection

from bots5.core.errors import (
    AuthorityError,
    SearchCursorStale,
    SearchIndexInvalid,
    SearchInvalidQuery,
    SearchStaleIndex,
    StateError,
)
from bots5.domain.models import (
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)
from bots5.domain.search import SearchFilters, SearchIndexCondition
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase7_source_mutation,
    clear_phase7_source_mutation,
)


NOW = datetime(2026, 9, 11, 4, 5, 6, tzinfo=UTC)


def _open_store(root: Path):
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        return authority, authority.open_store()
    except BaseException:
        authority.close()
        raise


def _chat(chat_id: str = "chat", title: str = "v4 repair needle") -> Chat:
    return Chat(chat_id, title, NOW, NOW)


def _bypass_source_update_guard(connection) -> None:
    """Inject already-persisted authoritative corruption without altering schema."""
    connection.connection.driver_connection.create_function(
        "bots5_phase7_source_revision_update_allowed",
        4,
        lambda old_id, new_id, old_revision, new_revision: 1,
    )


def _attempt(
    chat_id: str,
    user_id: str,
    assistant_id: str,
    suffix: str,
    when: datetime,
) -> GenerationAttempt:
    return GenerationAttempt(
        id=f"attempt-{suffix}",
        chat_id=chat_id,
        user_message_id=user_id,
        assistant_message_id=assistant_id,
        backend_id="fake",
        model="fake-v0.1",
        state=AttemptState.RUNNING,
        request_snapshot=json.dumps(
            {
                "attempt_id": f"attempt-{suffix}",
                "chat_id": chat_id,
                "user_message_id": user_id,
                "backend_id": "fake",
                "model": "fake-v0.1",
                "prompt": "deterministic v4 oracle",
            },
            sort_keys=True,
        ),
        started_at=when,
    )


def _generation_objects(chat: Chat, suffix: str, when: datetime):
    user = Message(
        f"user-{suffix}",
        chat.id,
        MessageRole.USER,
        MessageState.SENT,
        f"user {suffix}",
        1,
        when,
    )
    assistant = Message(
        f"assistant-{suffix}",
        chat.id,
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        2,
        when,
        parent_id=user.id,
    )
    attempt = _attempt(chat.id, user.id, assistant.id, suffix, when)
    updated_chat = replace(
        chat,
        updated_at=when,
        head_message_id=assistant.id,
        revision=chat.revision + 1,
    )
    return updated_chat, user, assistant, attempt


def _seed_terminal_turn(store, chat: Chat):
    updated, user, streaming, attempt = _generation_objects(
        chat, "seed", NOW + timedelta(seconds=1)
    )
    store.persist_generation_start(
        updated,
        user,
        streaming,
        attempt,
        expected_chat_revision=chat.revision,
    )
    terminal = replace(
        streaming,
        state=MessageState.COMPLETE,
        content="terminal seed answer",
    )
    store.finalize_generation(
        terminal,
        replace(attempt, state=AttemptState.COMPLETE, ended_at=attempt.started_at),
    )
    return updated, user, terminal


def test_known_authoritative_commit_then_unknown_derived_commit_returns_business_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    original_commit = Connection.commit
    commit_calls = 0

    def fail_second_commit(self, *args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            raise OSError("injected unknown derived receipt commit")
        return original_commit(self, *args, **kwargs)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(Connection, "commit", fail_second_commit)
            # The source commit is known successful.  The second commit is the
            # derived receipt transaction and must not escape as business failure.
            assert store.create_chat(_chat()) is None
            assert commit_calls == 2
            assert store._poisoned is True
            assert authority.state is AuthorityState.POISONED
            with pytest.raises(StateError, match="not admitting work"):
                store.assert_admitting()
    finally:
        authority.close()

    restarted_authority, restarted = _open_store(root)
    try:
        assert restarted.get_chat("chat") is not None
        status = restarted.search_status()
        assert status.condition is SearchIndexCondition.STALE
        assert status.source_revision == 1
        assert status.checkpoint_revision == 0
        with pytest.raises(SearchStaleIndex):
            restarted.search("v4 repair needle")
    finally:
        restarted_authority.close()


def test_known_commit_then_revoked_derived_admission_does_not_false_report_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority, store = _open_store(tmp_path / "root")

    def revoked_derived_admission() -> None:
        raise AuthorityError("injected revoked derived grant")

    try:
        monkeypatch.setattr(store, "_drain_search_receipts", revoked_derived_admission)
        assert store.create_chat(_chat()) is None
        assert store.get_chat("chat") is not None
        status = store.search_status()
        assert status.condition is SearchIndexCondition.STALE
        assert status.source_revision == 1
        assert status.checkpoint_revision == 0
    finally:
        authority.close()


@pytest.mark.parametrize(
    "operation",
    ("create", "archive", "generation", "regeneration"),
)
def test_unknown_authoritative_commit_is_classified_and_restart_exposes_durable_stale_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    root = tmp_path / operation
    authority, store = _open_store(root)
    chat = _chat()
    durable_message_id: str | None = None

    if operation != "create":
        store.create_chat(chat)
    if operation in {"archive", "regeneration"}:
        chat, user, terminal = _seed_terminal_turn(store, chat)

    if operation == "create":
        invoke = lambda: store.create_chat(chat)
    elif operation == "archive":
        archived_at = NOW + timedelta(seconds=2)
        invoke = lambda: store.archive_chat(chat.id, archived_at)
    elif operation == "generation":
        updated, user, assistant, attempt = _generation_objects(
            chat, "target", NOW + timedelta(seconds=2)
        )
        durable_message_id = assistant.id
        invoke = lambda: store.persist_generation_start(
            updated,
            user,
            assistant,
            attempt,
            expected_chat_revision=chat.revision,
        )
    else:
        assistant = Message(
            "assistant-regenerated",
            chat.id,
            MessageRole.ASSISTANT,
            MessageState.STREAMING,
            "",
            store.next_message_sequence(chat.id),
            NOW + timedelta(seconds=2),
            parent_id=user.id,
            lineage_id=terminal.lineage_id,
            revision=terminal.revision + 1,
            supersedes_id=terminal.id,
        )
        attempt = _attempt(
            chat.id,
            user.id,
            assistant.id,
            "regenerated",
            NOW + timedelta(seconds=2),
        )
        updated = replace(
            chat,
            updated_at=NOW + timedelta(seconds=2),
            head_message_id=assistant.id,
            revision=chat.revision + 1,
        )
        durable_message_id = assistant.id
        invoke = lambda: store.persist_regeneration_start(
            updated,
            assistant,
            attempt,
            expected_chat_revision=chat.revision,
        )

    original_commit = Connection.commit
    commit_calls = 0

    def commit_then_report_unknown(self, *args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        original_commit(self, *args, **kwargs)
        raise OSError(f"injected unknown {operation} commit acknowledgement")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(Connection, "commit", commit_then_report_unknown)
            with pytest.raises(StateError, match="outcome is uncertain"):
                invoke()
            assert commit_calls == 1
            assert store._poisoned is True
            assert authority.state is AuthorityState.POISONED
            with pytest.raises(StateError, match="not admitting work"):
                store.assert_admitting()
    finally:
        authority.close()

    restarted_authority, restarted = _open_store(root)
    try:
        persisted = restarted.get_chat("chat")
        assert persisted is not None
        if operation == "archive":
            assert persisted.archived_at == archived_at
        elif operation in {"generation", "regeneration"}:
            assert persisted.head_message_id == durable_message_id
            assert restarted.get_message(durable_message_id) is not None

        status = restarted.search_status()
        assert status.condition is SearchIndexCondition.STALE
        assert status.source_revision == status.checkpoint_revision + 1
    finally:
        restarted_authority.close()


def test_missing_derived_state_is_typed_invalid_and_explicit_rebuild_recreates_it(
    tmp_path: Path,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    try:
        store.create_chat(_chat())
        store.create_chat(_chat("chat-2"))
        initial_rebuild = store.rebuild_search_index()
        assert initial_rebuild.generation == 1
        old_page = store.search("v4 repair needle", limit=1)
        assert old_page.next_cursor is not None
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM search_index_state WHERE singleton_id=1"
            )

        status = store.search_status()
        assert status.condition is SearchIndexCondition.INVALID
        assert status.source_revision == 2
        assert status.checkpoint_revision is None
        assert status.generation is None
        with pytest.raises(SearchIndexInvalid, match="derived search index state is missing"):
            store.search("v4 repair needle")

        diagnosed = store.diagnose_search_index()
        assert diagnosed.condition is SearchIndexCondition.INVALID
        assert diagnosed.source_revision == 2
        assert diagnosed.checkpoint_revision is None
        assert diagnosed.generation is None

        rebuilt = store.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
        assert rebuilt.source_revision == rebuilt.checkpoint_revision == 2
        assert rebuilt.generation == 2
        with pytest.raises(SearchCursorStale):
            store.search(
                "v4 repair needle",
                limit=1,
                cursor=old_page.next_cursor,
            )
        assert [
            result.document_id
            for result in store.search("v4 repair needle").results
        ] == ["chat", "chat-2"]
    finally:
        authority.close()


def test_missing_derived_state_survives_restart_and_rebuild_rejects_old_cursor(
    tmp_path: Path,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    try:
        store.create_chat(_chat())
        store.create_chat(_chat("chat-2"))
        assert store.rebuild_search_index().generation == 1
        old_page = store.search("v4 repair needle", limit=1)
        assert old_page.next_cursor is not None
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM search_index_state WHERE singleton_id=1"
            )
    finally:
        authority.close()

    authority, store = _open_store(root)
    try:
        assert authority.state is AuthorityState.READY
        assert store.get_chat("chat") is not None
        assert store.get_chat("chat-2") is not None
        status = store.search_status()
        assert status.condition is SearchIndexCondition.INVALID
        assert status.source_revision == 2
        assert status.checkpoint_revision is None
        assert status.generation is None
        with pytest.raises(SearchIndexInvalid, match="derived search index state is missing"):
            store.search("v4 repair needle")
        assert store.diagnose_search_index().condition is SearchIndexCondition.INVALID

        rebuilt = store.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
        assert rebuilt.source_revision == rebuilt.checkpoint_revision == 2
        assert rebuilt.generation == 1
        # The missing row no longer carries its old integer generation. The
        # process epoch in the cursor fingerprint prevents cross-restart ABA.
        with pytest.raises(SearchCursorStale):
            store.search(
                "v4 repair needle",
                limit=1,
                cursor=old_page.next_cursor,
            )
    finally:
        authority.close()


@pytest.mark.parametrize(
    "assignment, expected_schema, expected_tokenizer",
    (
        ("schema_version=2", 2, "unicode61-v1"),
        ("tokenizer_version='alien-v9'", 1, "alien-v9"),
    ),
)
def test_search_refuses_mismatched_schema_or_tokenizer_until_rebuild(
    tmp_path: Path,
    assignment: str,
    expected_schema: int,
    expected_tokenizer: str,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        store.create_chat(_chat())
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                f"UPDATE search_index_state SET {assignment} WHERE singleton_id=1"
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

        status = store.search_status()
        assert status.condition is SearchIndexCondition.INVALID
        assert status.schema_version == expected_schema
        assert status.tokenizer_version == expected_tokenizer
        assert status.detail == "derived search schema or tokenizer version is invalid"
        with pytest.raises(
            SearchIndexInvalid,
            match="derived search schema or tokenizer version is invalid",
        ):
            store.search("v4 repair needle")

        authority.close()
        authority, store = _open_store(tmp_path / "root")
        status = store.search_status()
        assert status.condition is SearchIndexCondition.INVALID
        assert status.schema_version == expected_schema
        assert status.tokenizer_version == expected_tokenizer
        with pytest.raises(
            SearchIndexInvalid,
            match="derived search schema or tokenizer version is invalid",
        ):
            store.search("v4 repair needle")

        rebuilt = store.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
        assert rebuilt.schema_version == 1
        assert rebuilt.tokenizer_version == "unicode61-v1"
        assert [item.document_id for item in store.search("v4 repair needle").results] == [
            "chat"
        ]
    finally:
        authority.close()


@pytest.mark.parametrize(
    "assignment",
    (
        "schema_version='malformed'",
        "tokenizer_version=X'616c69656e'",
    ),
)
def test_restart_rejects_malformed_search_version_storage_types(
    tmp_path: Path,
    assignment: str,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    try:
        store.create_chat(_chat())
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                f"UPDATE search_index_state SET {assignment} WHERE singleton_id=1"
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    finally:
        authority.close()

    with pytest.raises(
        RuntimeError,
        match="current Phase 7 index singleton values are malformed",
    ):
        _open_store(root)


@pytest.mark.parametrize(
    "assignment,column,storage_type",
    (
        ("generation='not-an-integer'", "generation", "text"),
        ("checkpoint_revision='not-an-integer'", "checkpoint_revision", "text"),
        ("generation=1e400", "generation", "real"),
        ("condition=X'56414c4944'", "condition", "blob"),
        ("schema_version=X'31'", "schema_version", "blob"),
        ("tokenizer_version=X'756e69636f646536312d7631'", "tokenizer_version", "blob"),
        ("detail=X'666f72676564'", "detail", "blob"),
    ),
    ids=(
        "generation-text",
        "checkpoint-text",
        "generation-nonrepresentable",
        "condition-blob",
        "schema-version-blob",
        "tokenizer-version-blob",
        "detail-blob",
    ),
)
def test_malformed_live_derived_metadata_is_typed_nonpoisoning_and_rebuildable(
    tmp_path: Path,
    assignment: str,
    column: str,
    storage_type: str,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        store.create_chat(_chat())
        with store.command_admission(), store._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                f"UPDATE search_index_state SET {assignment} WHERE singleton_id=1"
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
            assert connection.exec_driver_sql(
                f"SELECT typeof({column}) FROM search_index_state WHERE singleton_id=1"
            ).scalar_one() == storage_type

        try:
            status = store.search_status()
        except (ValueError, TypeError, OverflowError):
            pytest.fail("raw derived metadata conversion escaped")
        assert status.condition is SearchIndexCondition.INVALID
        with pytest.raises(
            SearchIndexInvalid,
            match="derived search index coordination state is malformed",
        ):
            store.search("v4 repair needle")
        assert store.diagnose_search_index().condition is SearchIndexCondition.INVALID
        assert authority.state is AuthorityState.READY
        assert store._poisoned is False

        rebuilt = store.rebuild_search_index()
        assert rebuilt.condition is SearchIndexCondition.VALID
        assert rebuilt.source_revision == rebuilt.checkpoint_revision == 1
        assert type(rebuilt.generation) is int
        assert rebuilt.generation >= 1
        assert rebuilt.schema_version == 1
        assert rebuilt.tokenizer_version == "unicode61-v1"
        assert [result.document_id for result in store.search(
            "v4 repair needle",
            filters=SearchFilters(),
        ).results] == ["chat"]
        assert authority.state is AuthorityState.READY
    finally:
        authority.close()


def test_malformed_live_authoritative_source_revision_is_fail_closed(
    tmp_path: Path,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        with store.command_admission(), store._engine.begin() as connection:
            _bypass_source_update_guard(connection)
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                "UPDATE search_source_state SET source_revision='not-an-integer' "
                "WHERE singleton_id=1"
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
            assert connection.exec_driver_sql(
                "SELECT typeof(source_revision) FROM search_source_state "
                "WHERE singleton_id=1"
            ).scalar_one() == "text"
        assert authority.state is AuthorityState.READY

        try:
            with pytest.raises(
                StateError,
                match="authoritative search source state is malformed",
            ):
                store.search_status()
        except (ValueError, TypeError, OverflowError):
            pytest.fail("raw authoritative metadata conversion escaped")
        assert authority.state is AuthorityState.POISONED, (
            "malformed authoritative search state did not poison authority"
        )
        assert authority._active_operations == 0
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(_chat("after-poison", "must not be admitted"))
    finally:
        authority.close()


def test_malformed_authoritative_source_is_rejected_before_mutation_laundering(
    tmp_path: Path,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        with store.command_admission(), store._engine.begin() as connection:
            _bypass_source_update_guard(connection)
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                "UPDATE search_source_state SET source_revision='not-an-integer' "
                "WHERE singleton_id=1"
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(
            StateError,
            match="authoritative search source state is malformed",
        ):
            store.create_chat(_chat("laundered", "must roll back"))
        assert authority.state is AuthorityState.POISONED, (
            "malformed authoritative search state did not poison authority"
        )
        assert authority._active_operations == 0
        with pytest.raises(StateError, match="not admitting work"):
            store.assert_admitting()
    finally:
        authority.close()


def test_live_source_revision_regression_poison_prevents_revision_reuse_laundering(
    tmp_path: Path,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        store.create_chat(_chat("first", "first monotonic needle"))
        before = store.search_status()
        assert before.condition is SearchIndexCondition.VALID
        assert before.source_revision == before.checkpoint_revision == 1

        with store.command_admission(), store._engine.begin() as connection:
            _bypass_source_update_guard(connection)
            connection.exec_driver_sql(
                "UPDATE search_source_state SET source_revision=0 "
                "WHERE singleton_id=1"
            )

        with pytest.raises(
            StateError,
            match="authoritative search source revision regressed or advanced",
        ):
            store.search_status()
        assert authority.state is AuthorityState.POISONED
        assert authority._active_operations == 0
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(_chat("laundered", "must never reach revision one"))
    finally:
        authority.close()


def test_live_source_revision_regression_above_checkpoint_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        for index in range(5):
            store.create_chat(_chat(f"indexed-{index}", f"indexed {index}"))
        indexed = store.search_status()
        assert indexed.source_revision == indexed.checkpoint_revision == 5

        monkeypatch.setattr(store, "_accept_search_receipt", lambda receipt: None)
        for index in range(5, 10):
            store.create_chat(_chat(f"unindexed-{index}", f"unindexed {index}"))
        stale = store.search_status()
        assert stale.condition is SearchIndexCondition.STALE
        assert stale.source_revision == 10
        assert stale.checkpoint_revision == 5
        assert authority.state is AuthorityState.READY

        with store.command_admission(), store._engine.begin() as connection:
            _bypass_source_update_guard(connection)
            connection.exec_driver_sql(
                "UPDATE search_source_state SET source_revision=9 "
                "WHERE singleton_id=1"
            )
        with pytest.raises(
            StateError,
            match="authoritative search source revision regressed or advanced",
        ):
            store.search_status()
        assert authority.state is AuthorityState.POISONED
    finally:
        authority.close()


@pytest.mark.parametrize("target", [5, 6, 7])
def test_direct_source_revision_rewrite_is_rejected_without_poison(
    tmp_path: Path,
    target: int,
):
    authority, store = _open_store(tmp_path / f"root-{target}")
    try:
        for index in range(5):
            store.create_chat(_chat(f"chat-{index}", f"chat {index}"))
        with store.command_admission(), pytest.raises(
            Exception,
            match="search source revision transition is unauthorized",
        ):
            with store._engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE search_source_state SET source_revision=? "
                    "WHERE singleton_id=1",
                    (target,),
                )
        status = store.search_status()
        assert status.condition is SearchIndexCondition.VALID
        assert status.source_revision == status.checkpoint_revision == 5
        assert authority.state is AuthorityState.READY
    finally:
        authority.close()


@pytest.mark.parametrize("adjustment", [-1, 1])
def test_armed_source_revision_transition_must_be_exactly_one(
    tmp_path: Path,
    adjustment: int,
):
    authority, store = _open_store(tmp_path / f"root-{adjustment}")
    try:
        store.create_chat(_chat("seed", "seed"))
        with store.command_admission(), authority.transition():
            with store._engine.begin() as connection:
                arm_phase7_source_mutation(
                    connection,
                    "invalid exact transition oracle",
                    expected_revision=1,
                )
                try:
                    with pytest.raises(
                        Exception,
                        match="search source revision transition is unauthorized",
                    ):
                        connection.exec_driver_sql(
                            "UPDATE search_source_state SET source_revision="
                            "source_revision + bots5_phase7_consume_source_revision() + ? "
                            "WHERE singleton_id=1",
                            (adjustment,),
                        )
                finally:
                    clear_phase7_source_mutation(connection)
        status = store.search_status()
        assert status.condition is SearchIndexCondition.VALID
        assert status.source_revision == status.checkpoint_revision == 1
        assert authority.state is AuthorityState.READY
    finally:
        authority.close()


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "DELETE FROM search_source_state WHERE singleton_id=1",
            "search source singleton cannot be deleted directly",
        ),
        (
            "INSERT OR REPLACE INTO search_source_state"
            "(singleton_id, source_revision) VALUES (1, 99)",
            "search source singleton cannot be inserted directly",
        ),
    ],
)
def test_direct_source_singleton_replacement_is_rejected(
    tmp_path: Path,
    statement: str,
    message: str,
):
    authority, store = _open_store(tmp_path / "root")
    try:
        with store.command_admission(), pytest.raises(Exception, match=message):
            with store._engine.begin() as connection:
                connection.exec_driver_sql(statement)
        status = store.search_status()
        assert status.condition is SearchIndexCondition.VALID
        assert status.source_revision == status.checkpoint_revision == 0
        assert authority.state is AuthorityState.READY
    finally:
        authority.close()


def test_legal_source_mutations_advance_exactly_one_and_restart_monotonically(
    tmp_path: Path,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    try:
        for revision in range(1, 4):
            store.create_chat(_chat(f"chat-{revision}", f"chat {revision}"))
            status = store.search_status()
            assert status.condition is SearchIndexCondition.VALID
            assert status.source_revision == status.checkpoint_revision == revision
    finally:
        authority.close()

    restarted_authority, restarted = _open_store(root)
    try:
        status = restarted.search_status()
        assert status.source_revision == status.checkpoint_revision == 3
        restarted.create_chat(_chat("chat-4", "chat 4"))
        status = restarted.search_status()
        assert status.condition is SearchIndexCondition.VALID
        assert status.source_revision == status.checkpoint_revision == 4
    finally:
        restarted_authority.close()


def test_headless_archive_unarchive_preserves_conversation_revision_across_restarts(
    tmp_path: Path,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    archived_at = NOW + timedelta(seconds=1)
    try:
        store.create_chat(_chat())
        archived = store.archive_chat("chat", archived_at, expected_revision=0)
        assert archived.archived_at == archived_at
        assert archived.head_message_id is None
        assert archived.revision == 0
        status = store.search_status()
        assert status.condition is SearchIndexCondition.VALID
        assert status.source_revision == status.checkpoint_revision == 2
    finally:
        authority.close()

    authority, store = _open_store(root)
    try:
        persisted = store.get_chat("chat")
        assert persisted is not None
        assert persisted.archived_at == archived_at
        assert persisted.head_message_id is None
        assert persisted.revision == 0

        unarchived = store.archive_chat("chat", None, expected_revision=0)
        assert unarchived.archived_at is None
        assert unarchived.head_message_id is None
        assert unarchived.revision == 0
        status = store.search_status()
        assert status.condition is SearchIndexCondition.VALID
        assert status.source_revision == status.checkpoint_revision == 3
    finally:
        authority.close()

    authority, store = _open_store(root)
    try:
        persisted = store.get_chat("chat")
        assert persisted is not None
        assert persisted.archived_at is None
        assert persisted.head_message_id is None
        assert persisted.revision == 0
        assert store.search_status().condition is SearchIndexCondition.VALID
    finally:
        authority.close()


@pytest.mark.parametrize(
    "field,value",
    (
        ("backend_ids", "fake"),
        ("provider_ids", ["fake"]),
        ("models", (7,)),
        ("connection_ids", ("fake", None)),
        ("model_entry_ids", {"fake"}),
        ("document_kinds", ("chat",)),
        ("roles", ("user",)),
        ("message_states", ("sent",)),
        ("chat_id", 7),
    ),
)
def test_malformed_structured_filter_containers_and_values_are_typed(
    tmp_path: Path,
    field: str,
    value: object,
):
    authority, store = _open_store(tmp_path / f"root-{field}")
    try:
        filters = replace(SearchFilters(), **{field: value})
        with pytest.raises(SearchInvalidQuery):
            store.search("needle", filters=filters)
    finally:
        authority.close()
