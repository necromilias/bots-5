"""Phase 7 migration, authority, staleness, rebuild, and fault oracles.

The integration tests use the production ``DataRootAuthority`` and rooted VFS.
Concurrency assertions use explicit events; there are no timing sleeps.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from bots5.core.errors import (
    AuthorityError,
    SearchCursorStale,
    SearchRebuilding,
    SearchStaleIndex,
    StateError,
)
from bots5.domain.models import Chat
from bots5.domain.search import SearchDocumentKind, SearchFilters, SearchIndexCondition
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.persistence import migration_runner
from bots5.infrastructure.persistence.search import ReceiptCoordinator, SearchReceipt
from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase7_source_mutation,
    clear_phase7_source_mutation,
    require_phase7_consumed,
)
from tests._authority_test_support import upgrade_to


REPO = Path(__file__).resolve().parents[1]
HEAD = "0012_phase9_archive_import"
PRIOR_REVISIONS = (
    "0001_desktop_state",
    "0002_conversation_lineage",
    "0003_integrity_boundaries",
    "0004_integrity_guard_function",
    "0005_generation_outcomes",
    "0006_phase4_workspace",
    "0007_phase5_provider_model_configuration",
    "0008_catalogue_refresh_outcomes",
    "0009_phase6_context_attachments",
    "0010_phase7_search_navigation",
)
PRIOR_MIGRATION_SHA256 = {
    "0001_desktop_state.py": "15b7a409d35e3313db288f201e67d2e23c0a89e6f90058ad367dc879034e2da1",
    "0002_conversation_lineage.py": "7ddd512e48e75518c2730b0881a49e0f639fcf6d378e6e6a68b937da2f157c74",
    "0003_integrity_boundaries.py": "2058cbaf6ed630d354d126edb58307b54ec9f2518b4d50810d4ced3025af4cd4",
    "0004_integrity_guard_function.py": "3d7187d735fd3caa6fbabcc622b47dde5209403bab886b971bf63a007437b253",
    "0005_generation_outcomes.py": "2afd16e2f7d21f27cb27aea87d7cbb135c37892900de5745eb8ae1c0dacd41c6",
    "0006_phase4_workspace.py": "b670b4d26f11e22c94bf30283ab840512d261b07fba7ae7bd92ce27ff1a821a4",
    "0007_phase5_provider_model_configuration.py": "46bc12a9cd10b4c6a262b5bc5ca0a02ecc5d60201931a45dffe7fef3cf69eea6",
    "0008_catalogue_refresh_outcomes.py": "de23ea29c3c9749adb7ef01ce5d8a4ce34425798972524d743d2b5152aac47b9",
    "0009_phase6_context_attachments.py": "f589163298085d3cd7ca9897031137230bcd834d5e955943fc08eac11692a3f4",
}


def _new_authority(root: Path) -> DataRootAuthority:
    return DataRootAuthority(root.absolute()).acquire()


def _open_store(root: Path) -> tuple[DataRootAuthority, SQLiteAppStateStore]:
    authority = _new_authority(root)
    try:
        return authority, authority.open_store()
    except BaseException:
        try:
            authority.close()
        except BaseException:
            pass
        raise


def _historical_root(root: Path, revision: str) -> None:
    seed = root.parent / f".{root.name}-{revision}.sqlite3"
    upgrade_to(seed, revision)
    os.chmod(seed, 0o600)
    authority = _new_authority(root)
    authority.close()
    os.rename(seed, root / "database" / "state.sqlite3")


def _revision(root: Path) -> str:
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def _chat(chat_id: str, title: str) -> Chat:
    now = datetime(2026, 9, 11, 1, 2, 3, tzinfo=UTC)
    return Chat(chat_id, title, now, now)


def _status_tuple(store: SQLiteAppStateStore) -> tuple[object, ...]:
    status = store.search_status()
    return (
        status.condition,
        status.source_revision,
        status.checkpoint_revision,
        status.generation,
    )


@pytest.mark.parametrize("revision", (None,) + PRIOR_REVISIONS)
def test_fresh_and_every_supported_revision_migrate_to_phase7(
    tmp_path: Path, revision: str | None
):
    root = tmp_path / ("fresh" if revision is None else revision)
    if revision is not None:
        _historical_root(root, revision)
    authority, store = _open_store(root)
    try:
        assert authority.state is AuthorityState.READY
        assert _revision(root) == HEAD
        status = store.search_status()
        assert status.schema_version == 1
        assert status.tokenizer_version == "unicode61-v1"
        if revision is None:
            assert _status_tuple(store)[:3] == (SearchIndexCondition.VALID, 0, 0)
    finally:
        store.close()
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []


def test_phase1_through_phase6_migration_bytes_are_immutable():
    directory = (
        REPO
        / "src/bots5/infrastructure/persistence/migrations/versions"
    )
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("000[1-9]_*.py"))
    }
    assert actual == PRIOR_MIGRATION_SHA256


def test_fts5_preflight_failure_leaves_original_database_and_migration_areas_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    _historical_root(root, "0009_phase6_context_attachments")
    database = root / "database" / "state.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    authority = _new_authority(root)

    def unavailable() -> None:
        raise RuntimeError("injected runtime without FTS5")

    monkeypatch.setattr(migration_runner, "_preflight_fts5", unavailable)
    try:
        with pytest.raises(RuntimeError, match="without FTS5"):
            authority.open_store()
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
        assert _revision(root) == "0009_phase6_context_attachments"
        assert list((root / "database" / "migration").iterdir()) == []
        assert list((root / "recovery").iterdir()) == []
    finally:
        try:
            authority.close()
        except BaseException:
            pass


def _run_migration_child(root: Path, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_candidate_validation_failure_preserves_original_then_clean_restart_promotes(
    tmp_path: Path
):
    root = tmp_path / "root"
    _historical_root(root, "0009_phase6_context_attachments")
    database = root / "database" / "state.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    source = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner
def fail(vfs, target):
    raise RuntimeError("injected candidate migration failure")
migration_runner._run_alembic = fail
authority = DataRootAuthority(sys.argv[1]).acquire()
try:
    authority.open_store()
except RuntimeError as exc:
    assert "injected candidate migration failure" in str(exc)
    os._exit(77)
os._exit(78)
'''
    completed = _run_migration_child(root, source)
    assert completed.returncode == 77, (completed.stdout, completed.stderr)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert _revision(root) == "0009_phase6_context_attachments"

    authority, store = _open_store(root)
    store.close()
    assert authority.state is AuthorityState.CLOSED
    assert _revision(root) == HEAD
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []


def test_promoted_validation_failure_restores_original_before_later_clean_upgrade(
    tmp_path: Path
):
    root = tmp_path / "root"
    _historical_root(root, "0009_phase6_context_attachments")
    database = root / "database" / "state.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    source = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner
original = migration_runner._verify_database
calls = 0
def fail_promoted(vfs, revision, **kwargs):
    global calls
    if revision == migration_runner._HEAD:
        calls += 1
        if calls == 2:
            raise RuntimeError("injected promoted validation failure")
    return original(vfs, revision, **kwargs)
migration_runner._verify_database = fail_promoted
authority = DataRootAuthority(sys.argv[1]).acquire()
try:
    authority.open_store()
except RuntimeError as exc:
    assert "prior source was restored" in str(exc)
    os._exit(79)
os._exit(80)
'''
    completed = _run_migration_child(root, source)
    assert completed.returncode == 79, (completed.stdout, completed.stderr)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert _revision(root) == "0009_phase6_context_attachments"
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []

    authority, store = _open_store(root)
    store.close()
    assert authority.state is AuthorityState.CLOSED
    assert _revision(root) == HEAD


def test_genuine_legacy_0009_journal_is_completed_before_distinct_0010_upgrade(
    tmp_path: Path
):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    source = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner
original = migration_runner._journal_base
def legacy(authority, transaction_id, source_kind):
    record = original(authority, transaction_id, source_kind)
    record["target_revision"] = migration_runner._LEGACY_HEAD
    return record
def die(point):
    if point == "after-candidate-revision-0009_phase6_context_attachments":
        os._exit(91)
migration_runner._journal_base = legacy
migration_runner._TEST_FAULT_HOOK = die
DataRootAuthority(sys.argv[1]).acquire().open_store()
os._exit(92)
'''
    completed = _run_migration_child(root, source)
    assert completed.returncode == 91, (completed.stdout, completed.stderr)
    journal_path = root / "database/migration/phase6-journal-v3.json"
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    assert record["target_revision"] == "0009_phase6_context_attachments"

    authority, store = _open_store(root)
    store.close()
    assert authority.state is AuthorityState.CLOSED
    assert _revision(root) == HEAD
    assert not journal_path.exists()
    assert list((root / "database" / "migration").iterdir()) == []


def test_legacy_0009_journal_rejects_a_newer_0010_source_revision(
    tmp_path: Path,
):
    root = tmp_path / "root"
    _historical_root(root, "0010_phase7_search_navigation")
    source = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner
original = migration_runner._journal_base
def legacy(authority, transaction_id, source_kind):
    record = original(authority, transaction_id, source_kind)
    record["target_revision"] = migration_runner._LEGACY_HEAD
    return record
migration_runner._journal_base = legacy
try:
    DataRootAuthority(sys.argv[1]).acquire().open_store()
except RuntimeError as exc:
    assert "source revision is unsupported" in str(exc)
    os._exit(93)
os._exit(94)
'''

    completed = _run_migration_child(root, source)

    assert completed.returncode == 93, (completed.stdout, completed.stderr)
    assert _revision(root) == "0010_phase7_search_navigation"
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []


def test_phase7_source_guard_rejects_unarmed_dml_and_consumes_once_per_transaction(
    tmp_path: Path
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    stamp = "2026-09-11T01:02:03.000Z"
    try:
        with store.command_admission():
            with store._engine.begin() as connection:
                with pytest.raises(Exception, match="Phase 7 search-visible mutation"):
                    connection.exec_driver_sql(
                        "INSERT INTO chats(id,title,created_at,updated_at,head_message_id,revision,archived_at) "
                        "VALUES ('unarmed','bad',?,?,NULL,0,NULL)",
                        (stamp, stamp),
                    )
        assert _status_tuple(store)[:3] == (SearchIndexCondition.VALID, 0, 0)

        with store.command_admission():
            with authority.transition():
                with store._engine.begin() as connection:
                    arm_phase7_source_mutation(connection, "two-row source oracle")
                    try:
                        for chat_id in ("raw-a", "raw-b"):
                            connection.exec_driver_sql(
                                "INSERT INTO chats(id,title,created_at,updated_at,head_message_id,revision,archived_at) "
                                "VALUES (?, ?, ?, ?, NULL, 0, NULL)",
                                (chat_id, chat_id, stamp, stamp),
                            )
                        assert require_phase7_consumed(connection) == 1
                    finally:
                        clear_phase7_source_mutation(connection)
                store._record_committed_search_source_revision(1)
        assert _status_tuple(store)[:3] == (SearchIndexCondition.STALE, 1, 0)
    finally:
        store.close()


def test_chat_identity_and_recency_updates_are_source_guarded_and_stale_old_cursors(
    tmp_path: Path,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    later = "2026-09-11T01:02:04.000Z"
    filters = SearchFilters(document_kinds=(SearchDocumentKind.CHAT,))
    try:
        store.create_chat(_chat("chat-a", "trigger cursor needle"))
        store.create_chat(_chat("chat-b", "trigger cursor needle"))
        first = store.search("trigger cursor needle", filters=filters, limit=1)
        assert first.next_cursor is not None
        before = _status_tuple(store)

        for statement, parameters in (
            ("UPDATE chats SET id=? WHERE id=?", ("renamed", "chat-a")),
            ("UPDATE chats SET updated_at=? WHERE id=?", (later, "chat-a")),
        ):
            with store.command_admission(), store._engine.begin() as connection:
                with pytest.raises(Exception, match="Phase 7 search-visible mutation"):
                    connection.exec_driver_sql(statement, parameters)
        assert _status_tuple(store) == before

        with store.command_admission(), authority.transition():
            with store._engine.begin() as connection:
                arm_phase7_source_mutation(connection, "updated-at cursor oracle")
                try:
                    connection.exec_driver_sql(
                        "UPDATE chats SET updated_at=? WHERE id=?",
                        (later, "chat-a"),
                    )
                    committed_revision = require_phase7_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
            store._record_committed_search_source_revision(committed_revision)
        store._accept_search_receipt(
            SearchReceipt(committed_revision, frozenset({"chat:chat-a"}))
        )
        assert _status_tuple(store)[:3] == (
            SearchIndexCondition.VALID,
            committed_revision,
            committed_revision,
        )
        with pytest.raises(SearchCursorStale):
            store.search(
                "trigger cursor needle",
                filters=filters,
                limit=1,
                cursor=first.next_cursor,
            )
    finally:
        store.close()


def test_lost_and_out_of_order_receipts_remain_stale_until_contiguous_catchup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    original = SQLiteAppStateStore._accept_search_receipt
    dropped: list[SearchReceipt] = []

    def lose(_self, receipt: SearchReceipt) -> None:
        dropped.append(receipt)

    try:
        monkeypatch.setattr(SQLiteAppStateStore, "_accept_search_receipt", lose)
        store.create_chat(_chat("lost", "lost receipt token"))
        assert _status_tuple(store)[:3] == (SearchIndexCondition.STALE, 1, 0)
        with pytest.raises(SearchStaleIndex):
            store.search("lost")

        monkeypatch.setattr(SQLiteAppStateStore, "_accept_search_receipt", original)
        store.create_chat(_chat("second", "second receipt token"))
        assert _status_tuple(store)[:3] == (SearchIndexCondition.STALE, 2, 0)

        original(store, dropped[0])
        assert _status_tuple(store)[:3] == (SearchIndexCondition.VALID, 2, 2)
        assert [result.document_id for result in store.search("receipt").results] == [
            "lost",
            "second",
        ]
    finally:
        store.close()


def test_receipt_coordinator_duplicate_and_gap_oracle():
    coordinator = ReceiptCoordinator()
    coordinator.accept(SearchReceipt(2, frozenset({"chat:b"})))
    coordinator.accept(SearchReceipt(2, frozenset({"message:m"})))
    assert coordinator.contiguous(0) is None
    coordinator.accept(SearchReceipt(1, frozenset({"chat:a"})))
    assert coordinator.contiguous(0) == (
        2,
        frozenset({"chat:a", "chat:b", "message:m"}),
    )
    coordinator.acknowledge(1)
    assert coordinator.contiguous(0) is None


def test_interrupted_rebuild_stays_rebuilding_and_repeated_rebuild_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from bots5.infrastructure.persistence import sqlite as sqlite_module

    root = tmp_path / "root"
    authority, store = _open_store(root)
    original_validate = sqlite_module.validate_phase7_rebuild
    try:
        store.create_chat(_chat("chat", "deterministic rebuild token"))
        with store.command_admission():
            with store._engine.connect() as connection:
                before = connection.exec_driver_sql(
                    "SELECT fts_rowid, document_kind, document_id FROM search_document_keys ORDER BY fts_rowid"
                ).fetchall()

        def interrupt(_connection) -> None:
            raise RuntimeError("injected rebuild interruption")

        monkeypatch.setattr(sqlite_module, "validate_phase7_rebuild", interrupt)
        with pytest.raises(RuntimeError, match="rebuild interruption"):
            store.rebuild_search_index()
        assert store.search_status().condition is SearchIndexCondition.REBUILDING
        with pytest.raises(SearchRebuilding):
            store.search("token")

        monkeypatch.setattr(sqlite_module, "validate_phase7_rebuild", original_validate)
        first = store.rebuild_search_index()
        assert first.condition is SearchIndexCondition.VALID
        with store.command_admission():
            with store._engine.connect() as connection:
                after_first = connection.exec_driver_sql(
                    "SELECT fts_rowid, document_kind, document_id FROM search_document_keys ORDER BY fts_rowid"
                ).fetchall()
        second = store.rebuild_search_index()
        with store.command_admission():
            with store._engine.connect() as connection:
                after_second = connection.exec_driver_sql(
                    "SELECT fts_rowid, document_kind, document_id FROM search_document_keys ORDER BY fts_rowid"
                ).fetchall()
        assert before == after_first == after_second
        assert second.generation == first.generation + 1
    finally:
        store.close()


def test_rebuild_lock_linearizes_writer_that_arrives_after_lock_without_sleeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    store.create_chat(_chat("before", "before rebuild"))
    rebuild_owns_lock = threading.Event()
    release_rebuild = threading.Event()
    writer_attempted = threading.Event()
    original = SQLiteAppStateStore._write_rebuild_state

    def barrier(self) -> int:
        rebuild_owns_lock.set()
        assert release_rebuild.wait(5), "rebuild barrier was not released"
        return original(self)

    monkeypatch.setattr(SQLiteAppStateStore, "_write_rebuild_state", barrier)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            rebuild = executor.submit(store.rebuild_search_index)
            assert rebuild_owns_lock.wait(5), "rebuild did not acquire serialization"

            def write() -> None:
                writer_attempted.set()
                store.create_chat(_chat("after", "after rebuild"))

            writer = executor.submit(write)
            assert writer_attempted.wait(5), "writer did not attempt serialization"
            assert not writer.done()
            release_rebuild.set()
            rebuild.result(timeout=10)
            writer.result(timeout=10)
        assert _status_tuple(store)[:3] == (SearchIndexCondition.VALID, 2, 2)
        assert store.search("after").results[0].document_id == "after"
    finally:
        release_rebuild.set()
        store.close()


def test_rebuild_lock_linearizes_writer_that_owned_lock_first_without_sleeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    writer_owns_lock = threading.Event()
    release_writer = threading.Event()
    rebuild_attempted = threading.Event()
    original_source = SQLiteAppStateStore._search_source_transaction
    original_rebuild = SQLiteAppStateStore.rebuild_search_index

    @contextmanager
    def blocked_source(self, operation, document_keys):
        with original_source(self, operation, document_keys) as connection:
            writer_owns_lock.set()
            assert release_writer.wait(5), "writer barrier was not released"
            yield connection

    def rebuild(self):
        rebuild_attempted.set()
        return original_rebuild(self)

    monkeypatch.setattr(SQLiteAppStateStore, "_search_source_transaction", blocked_source)
    monkeypatch.setattr(SQLiteAppStateStore, "rebuild_search_index", rebuild)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(store.create_chat, _chat("first", "writer first"))
            assert writer_owns_lock.wait(5), "writer did not acquire serialization"
            rebuilding = executor.submit(store.rebuild_search_index)
            assert rebuild_attempted.wait(5), "rebuild did not attempt serialization"
            assert not rebuilding.done()
            release_writer.set()
            writer.result(timeout=10)
            rebuilding.result(timeout=10)
        assert _status_tuple(store)[:3] == (SearchIndexCondition.VALID, 1, 1)
        assert store.search("writer").results[0].document_id == "first"
    finally:
        release_writer.set()
        store.close()


def test_search_snapshot_linearizes_before_concurrent_source_change_without_sleeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    store.create_chat(_chat("old", "snapshot common old"))
    query_has_snapshot = threading.Event()
    release_query = threading.Event()
    writer_attempted = threading.Event()
    original_result = SQLiteAppStateStore._search_result_from_row

    def blocked_result(self, connection, row, **kwargs):
        query_has_snapshot.set()
        assert release_query.wait(5), "query barrier was not released"
        return original_result(self, connection, row, **kwargs)

    monkeypatch.setattr(SQLiteAppStateStore, "_search_result_from_row", blocked_result)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            query = executor.submit(store.search, "snapshot")
            assert query_has_snapshot.wait(5), "query did not establish its snapshot"

            def write() -> None:
                writer_attempted.set()
                store.create_chat(_chat("new", "snapshot common new"))

            writer = executor.submit(write)
            assert writer_attempted.wait(5), "writer did not attempt source change"
            assert not writer.done()
            release_query.set()
            page = query.result(timeout=10)
            writer.result(timeout=10)
        assert [result.document_id for result in page.results] == ["old"]
        assert page.status.source_revision == page.status.checkpoint_revision == 1
        assert _status_tuple(store)[:3] == (SearchIndexCondition.VALID, 2, 2)
    finally:
        release_query.set()
        store.close()


class _FakeAuthority:
    def __init__(self):
        self.poisoned = False
        self.reasons: list[str] = []

    def poison(self, reason: str) -> None:
        self.poisoned = True
        self.reasons.append(reason)

    @contextmanager
    def application_operation(self, *, independent: bool = False):
        del independent
        if self.poisoned:
            raise AuthorityError("revoked")
        yield

    @contextmanager
    def transition(self):
        if self.poisoned:
            raise AuthorityError("revoked")
        yield


class _UnknownConnection:
    def __init__(self, failing: str):
        self.failing = failing

    def commit(self) -> None:
        if self.failing == "commit":
            raise OSError("unknown commit")

    def rollback(self) -> None:
        if self.failing == "rollback":
            raise OSError("unknown rollback")

    def close(self) -> None:
        if self.failing == "close":
            raise OSError("unknown close")


@pytest.mark.parametrize(
    "method,failing,reason",
    (
        ("_commit_search_transaction", "commit", "uncertain search transaction commit"),
        ("_rollback_search_transaction", "rollback", "search transaction rollback failed"),
        ("_close_search_connection", "close", "search connection close failed"),
    ),
)
def test_unknown_search_database_outcomes_poison_and_close_future_admission(
    method: str, failing: str, reason: str
):
    store = object.__new__(SQLiteAppStateStore)
    store._closed = False
    store._poisoned = False
    store._authority = _FakeAuthority()
    with pytest.raises(StateError):
        getattr(store, method)(_UnknownConnection(failing), "fault oracle")
    assert store._poisoned is True
    assert len(store._authority.reasons) == 1
    assert reason in store._authority.reasons[0]
    with pytest.raises(StateError, match="not admitting work"):
        store.assert_admitting()


def test_revoked_derived_grant_does_not_open_connection_or_false_report_business_failure():
    class Engine:
        connects = 0

        def connect(self):
            self.connects += 1
            raise AssertionError("revoked derived work opened a new connection")

    store = object.__new__(SQLiteAppStateStore)
    store._closed = False
    store._poisoned = False
    store._authority = _FakeAuthority()
    store._authority.poison("oracle revocation")
    store._engine = Engine()
    store._search_available = True
    store._search_receipts = ReceiptCoordinator()

    store._accept_search_receipt(SearchReceipt(1, frozenset({"chat:a"})))
    assert store._engine.connects == 0
