"""Release-critical tests for the unified authority/effect-grant protocol.

These tests record the intermediate ordering that distinguishes a deferred
terminal request from an illegal terminal-state publication.  They are kept
separate from the historical Phase 6 campaign so their F1/F2/F3 mutation
targets remain small and reviewable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from contextvars import copy_context
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import StatementError
from uuid6 import uuid7

from bots5.core.application import BotsApplication
from bots5.core.context import ContextBuildError, ContextBuilder, ContextSource
from bots5.core.errors import AuthorityError, StateError
from bots5.core.events import EventBus
from bots5.core.events import CoreEvent
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import Chat
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure import data_root_authority as authority_module
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore
from bots5.infrastructure.persistence import sqlite as sqlite_store_module
from bots5.infrastructure.rooted_sqlite_vfs import _RootedConnection, _RootedCursor
from bots5.core.provider_configuration import ProviderConfiguration


def _open_application(root: Path, *, queue_size: int = 4):
    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=queue_size)
    application = BotsApplication(
        store,
        events,
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )
    return authority, store, application, events


def _open_configured_application(root: Path):
    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=4)
    application = BotsApplication(
        store,
        events,
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=ProviderConfiguration(store, ids, clock),
    )
    return authority, store, application, events


def _corrupt_authoritative_text_representation(
    root: Path,
    attachment_id: str,
    tamper: str,
) -> tuple[bytes, bytes | None, bytes | None, str | None]:
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        blob_digest, original_representation_id, original_digest = connection.execute(
            "SELECT blob_digest, text_representation_id, text_digest "
            "FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        wrong_digest = b"\x01" * 32
        assert blob_digest != wrong_digest
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'phase6_attachment_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER phase6_attachment_immutable")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        if tamper == "representation_id_missing":
            connection.execute(
                "UPDATE attachments SET text_representation_id = ?, text_digest = ? "
                "WHERE id = ?",
                (None, original_digest, attachment_id),
            )
        elif tamper == "representation_digest_missing":
            connection.execute(
                "UPDATE attachments SET text_representation_id = ?, text_digest = ? "
                "WHERE id = ?",
                (original_representation_id, None, attachment_id),
            )
        elif tamper == "identity_digest_disagree":
            connection.execute(
                "UPDATE attachments SET text_representation_id = ?, text_digest = ? "
                "WHERE id = ?",
                (wrong_digest, original_digest, attachment_id),
            )
        elif tamper == "digest_payload_disagree":
            connection.execute(
                "UPDATE attachments SET text_representation_id = ?, text_digest = ? "
                "WHERE id = ?",
                (wrong_digest, wrong_digest, attachment_id),
            )
        elif tamper == "invalid_utf8_claimed_text":
            assert original_representation_id is None and original_digest is None
            connection.execute(
                "UPDATE attachments SET text_representation_id = blob_digest, "
                "text_digest = blob_digest, ineligibility_reason = NULL WHERE id = ?",
                (attachment_id,),
            )
        elif tamper == "nul_claimed_text":
            assert original_representation_id is None and original_digest is None
            connection.execute(
                "UPDATE attachments SET text_representation_id = blob_digest, "
                "text_digest = blob_digest, ineligibility_reason = NULL WHERE id = ?",
                (attachment_id,),
            )
        elif tamper == "valid_text_marked_ineligible":
            assert original_representation_id == original_digest == blob_digest
            connection.execute(
                "UPDATE attachments SET text_representation_id = NULL, "
                "text_digest = NULL, ineligibility_reason = 'invalid_utf8' WHERE id = ?",
                (attachment_id,),
            )
        elif tamper == "valid_text_with_ineligibility_reason":
            assert original_representation_id == original_digest == blob_digest
            connection.execute(
                "UPDATE attachments SET ineligibility_reason = 'contains_nul' WHERE id = ?",
                (attachment_id,),
            )
        elif tamper == "invalid_utf8_wrong_reason":
            assert original_representation_id is None and original_digest is None
            connection.execute(
                "UPDATE attachments SET ineligibility_reason = 'contains_nul' WHERE id = ?",
                (attachment_id,),
            )
        elif tamper == "nul_wrong_reason":
            assert original_representation_id is None and original_digest is None
            connection.execute(
                "UPDATE attachments SET ineligibility_reason = 'invalid_utf8' WHERE id = ?",
                (attachment_id,),
            )
        else:
            raise AssertionError(f"unknown representation tamper: {tamper}")
        connection.execute(trigger_sql)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'phase6_attachment_immutable'"
        ).fetchone() == (1,)
        return connection.execute(
            "SELECT blob_digest, text_representation_id, text_digest, "
            "ineligibility_reason FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()


def _relabel_attachment_to_nul_blob(
    root: Path,
    attachment_id: str,
    nul_attachment_id: str,
) -> None:
    """Install a post-plan NUL text claim while restoring the immutable trigger."""
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        nul_blob_digest, nul_representation, nul_digest, nul_reason = connection.execute(
            "SELECT blob_digest, text_representation_id, text_digest, "
            "ineligibility_reason FROM attachments WHERE id = ?",
            (nul_attachment_id,),
        ).fetchone()
        assert nul_representation is None
        assert nul_digest is None
        assert nul_reason == "contains_nul"
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'phase6_attachment_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER phase6_attachment_immutable")
        connection.execute(
            "UPDATE attachments SET blob_digest = ?, text_representation_id = ?, "
            "text_digest = ?, ineligibility_reason = NULL WHERE id = ?",
            (
                nul_blob_digest,
                nul_blob_digest,
                nul_blob_digest,
                attachment_id,
            ),
        )
        connection.execute(trigger_sql)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'phase6_attachment_immutable'"
        ).fetchone() == (1,)


def _phase6_chat_counts(root: Path, chat_id: str) -> tuple[object, ...]:
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        return (
            connection.execute(
                "SELECT revision, head_message_id FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone(),
            connection.execute(
                "SELECT count(*) FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM generation_attempts WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0],
            connection.execute("SELECT count(*) FROM context_plans").fetchone()[0],
            connection.execute("SELECT count(*) FROM message_attachments").fetchone()[0],
            connection.execute("SELECT count(*) FROM attempt_attachments").fetchone()[0],
        )


def _arm_real_fresh_view_close_failure(authority: DataRootAuthority) -> dict[str, object]:
    """Fail the selected os.close while retaining production classification."""
    original_close_claim = authority._close_claim
    evidence: dict[str, object] = {"selected": 0, "attempted": 0, "claim": None}

    def selected_close(claim):
        if claim.label.startswith("fresh-view:") and evidence["selected"] == 0:
            evidence["selected"] = 1
            evidence["claim"] = claim
            selected_fd = claim.fd
            original_os_close = os.close

            def fail_selected(fd: int) -> None:
                evidence["attempted"] = int(evidence["attempted"]) + 1
                if fd == selected_fd:
                    raise OSError("injected fresh-view close uncertainty")
                original_os_close(fd)

            os.close = fail_selected
            try:
                return original_close_claim(claim)
            finally:
                os.close = original_os_close
        return original_close_claim(claim)

    authority._close_claim = selected_close
    return evidence


def _request_terminal_from_unrelated_owner(
    authority: DataRootAuthority,
    errors: list[BaseException],
) -> threading.Thread:
    def fail_fresh_view() -> None:
        try:
            authority.fresh_directory_inventory("attachments/objects")
        except BaseException as exc:
            errors.append(exc)
        else:
            errors.append(AssertionError("fresh-view close injection did not fire"))

    worker = threading.Thread(target=fail_fresh_view, name="terminal-invalidator")
    worker.start()
    worker.join(15)
    assert not worker.is_alive()
    return worker


def _best_effort_terminal_release(authority: DataRootAuthority) -> None:
    if authority.state is AuthorityState.CLOSED:
        return
    try:
        authority.close()
    except AuthorityError:
        pass


@pytest.mark.parametrize("command", ("stage", "unstage", "create_chat"))
def test_f1_terminal_request_waits_for_unrelated_public_command_effect(
    tmp_path: Path,
    command: str,
) -> None:
    """F1: UNKNOWN is immediate evidence; FAILED_CLOSED waits for grant A."""

    async def scenario() -> None:
        authority, store, application, events = _open_application(tmp_path / command)
        source = tmp_path / f"{command}.txt"
        source.write_bytes(command.encode())
        attachment = store.ingest_attachment(source)
        chat = await application.create_chat(f"F1 {command}")
        if command == "unstage":
            await application.stage_attachment(chat.id, attachment.id)
        subscription = application.subscribe()
        evidence = _arm_real_fresh_view_close_failure(authority)
        failures: list[BaseException] = []
        try:
            with store.command_admission():
                _request_terminal_from_unrelated_owner(authority, failures)
                assert len(failures) == 1
                assert isinstance(failures[0], AuthorityError)
                claim = evidence["claim"]
                assert claim is not None and claim.status == "UNKNOWN"
                assert evidence["selected"] == 1
                assert evidence["attempted"] == 1
                assert authority.poison_pending is True
                assert authority.state is AuthorityState.READY

                if command == "stage":
                    result = await application.stage_attachment(chat.id, attachment.id)
                    expected_kind = "pending_attachments_changed"
                    assert result == (attachment.id,)
                elif command == "unstage":
                    result = await application.unstage_attachment(chat.id, attachment.id)
                    expected_kind = "pending_attachments_changed"
                    assert result == ()
                else:
                    result = await application.create_chat("F1 durable chat")
                    expected_kind = "chat_created"
                    persisted = store.get_chat(result.id)
                    assert persisted is not None
                    assert (persisted.id, persisted.title) == (result.id, result.title)

                delivered = await asyncio.wait_for(subscription.__anext__(), 5)
                assert delivered.kind == expected_kind
                assert authority.state is AuthorityState.READY
                assert authority.poison_pending is True

            assert authority.state is AuthorityState.FAILED_CLOSED
            with pytest.raises(StateError, match="not admitting work"):
                await application.create_chat("late")
        finally:
            subscription.close()
            _best_effort_terminal_release(authority)

    asyncio.run(scenario())


def test_f1_failed_enumeration_close_sibling_also_waits_for_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed-enumeration close branch obeys the same F1 ordering."""

    async def scenario() -> None:
        authority, store, application, events = _open_application(tmp_path / "root")
        chat = await application.create_chat("F1 failed enumeration")
        source = tmp_path / "sibling.txt"
        source.write_bytes(b"sibling")
        attachment = store.ingest_attachment(source)
        subscription = application.subscribe()
        evidence = _arm_real_fresh_view_close_failure(authority)
        failures: list[BaseException] = []

        def fail_enumeration(*args, **kwargs):
            del args, kwargs
            raise AuthorityError("injected fresh-view enumeration failure")

        monkeypatch.setattr(authority_module, "_check_directory", fail_enumeration)
        try:
            with store.command_admission():
                _request_terminal_from_unrelated_owner(authority, failures)
                assert len(failures) == 1
                assert isinstance(failures[0], AuthorityError)
                assert evidence["attempted"] == 1
                assert evidence["claim"].status == "UNKNOWN"
                assert authority.state is AuthorityState.READY
                assert authority.poison_pending is True
                result = await application.stage_attachment(chat.id, attachment.id)
                assert result == (attachment.id,)
                delivered = await asyncio.wait_for(subscription.__anext__(), 5)
                assert delivered.kind == "pending_attachments_changed"
                assert authority.state is AuthorityState.READY
            assert authority.state is AuthorityState.FAILED_CLOSED
        finally:
            subscription.close()
            _best_effort_terminal_release(authority)

    asyncio.run(scenario())


@pytest.mark.parametrize("barrier", ("before_insert", "before_commit"))
def test_f2_direct_store_grant_survives_insert_commit_and_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
) -> None:
    """F2: direct callee ownership lasts through transaction settlement."""
    root = tmp_path / barrier
    authority, store, _, _ = _open_application(root)
    reached = threading.Event()
    release = threading.Event()
    observed: dict[str, object] = {}
    original_execute = Connection.execute
    original_commit = _RootedConnection.commit

    def pause_insert(self, statement, *args, **kwargs):
        if barrier == "before_insert" and not reached.is_set() and "INSERT INTO chats" in str(statement):
            observed["active_before_insert"] = authority._active_operations
            reached.set()
            assert release.wait(15)
        return original_execute(self, statement, *args, **kwargs)

    def pause_commit(self):
        if barrier == "before_commit" and not reached.is_set():
            observed["active_before_commit"] = authority._active_operations
            reached.set()
            assert release.wait(15)
        return original_commit(self)

    monkeypatch.setattr(Connection, "execute", pause_insert)
    monkeypatch.setattr(_RootedConnection, "commit", pause_commit)
    chat = Chat(str(uuid7()), f"F2 {barrier}", datetime.now(UTC), datetime.now(UTC))
    outcome: list[BaseException | None] = []

    def writer() -> None:
        try:
            store.create_chat(chat)
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=writer, name=f"direct-writer-{barrier}")
    worker.start()
    try:
        assert reached.wait(15)
        assert authority._active_operations == 1
        authority.poison(f"external invalidation at {barrier}")
        assert authority.state is AuthorityState.READY
        assert authority.poison_pending is True
        release.set()
        worker.join(15)
        assert not worker.is_alive()
        assert outcome == [None]
        assert authority.state is AuthorityState.POISONED
        with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
            assert connection.execute(
                "SELECT title FROM chats WHERE id = ?", (chat.id,)
            ).fetchone() == (chat.title,)
    finally:
        release.set()
        worker.join(15)
        _best_effort_terminal_release(authority)


def test_f2_invalidation_rejects_before_creator_and_private_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, store, _, _ = _open_application(tmp_path / "root")
    creator_calls = 0
    dbapi_connect_calls = 0
    original_creator = store._engine.pool._creator
    original_dbapi_connect = sqlite3.connect
    vfs = authority._vfs
    assert vfs is not None
    native_open_count = vfs.open_count

    def counted_creator(*args, **kwargs):
        nonlocal creator_calls
        creator_calls += 1
        return original_creator(*args, **kwargs)

    def counted_dbapi_connect(*args, **kwargs):
        nonlocal dbapi_connect_calls
        dbapi_connect_calls += 1
        return original_dbapi_connect(*args, **kwargs)

    monkeypatch.setattr(store._engine.pool, "_creator", counted_creator)
    monkeypatch.setattr(sqlite3, "connect", counted_dbapi_connect)
    try:
        with pytest.raises(AuthorityError, match="no forward grant"):
            store._engine.connect()
        # The private SQLAlchemy factory can be invoked, but the rooted VFS
        # rejects before the real DBAPI creator/native open boundary.
        assert creator_calls == 1
        assert dbapi_connect_calls == 0
        assert vfs.open_count == native_open_count

        authority.poison("invalidation wins before connection creation")
        assert authority.state is AuthorityState.POISONED
        with pytest.raises(StateError, match="not admitting work"):
            store.list_chats()
        with pytest.raises(StateError, match="not admitting work"):
            store.set_application_default_model("missing-model")
        assert creator_calls == 1
        assert dbapi_connect_calls == 0
        assert vfs.open_count == native_open_count
    finally:
        _best_effort_terminal_release(authority)


@pytest.mark.parametrize("surviving_owner", (False, True))
def test_f3_self_failed_grant_cannot_reenter_while_caught(
    tmp_path: Path,
    surviving_owner: bool,
) -> None:
    """F3: per-grant revocation is enforced even while public state is READY."""

    async def scenario() -> None:
        authority, store, application, events = _open_application(tmp_path / str(surviving_owner))
        source = tmp_path / f"self-{surviving_owner}.txt"
        source.write_bytes(b"self failure")
        attachment = store.ingest_attachment(source)
        chat = await application.create_chat("F3")
        await application.stage_attachment(chat.id, attachment.id)
        selection_before = application._pending_attachment_ids[chat.id]
        event_sequence_before = events._sequence
        evidence = _arm_real_fresh_view_close_failure(authority)
        other_entered = threading.Event()
        release_other = threading.Event()
        other_result: list[BaseException | None] = []

        def other_owner() -> None:
            try:
                with store.command_admission():
                    other_entered.set()
                    assert release_other.wait(15)
                    authority.database_durability_fence()
            except BaseException as exc:
                other_result.append(exc)
            else:
                other_result.append(None)

        other = threading.Thread(target=other_owner, name="surviving-owner")
        try:
            with store.command_admission():
                if surviving_owner:
                    other.start()
                    assert other_entered.wait(15)
                connection = store._engine.connect() if surviving_owner else None
                try:
                    with pytest.raises(AuthorityError, match="close outcome is uncertain"):
                        authority.fresh_directory_inventory("attachments/objects")
                    assert evidence["attempted"] == 1
                    if surviving_owner:
                        assert authority.state is AuthorityState.READY
                    else:
                        assert authority.state is AuthorityState.FAILED_CLOSED
                    assert authority.poison_pending is True

                    with pytest.raises(StateError, match="not admitting work"):
                        await application.stage_attachment(chat.id, attachment.id)
                    with pytest.raises(StateError, match="not admitting work"):
                        await application.unstage_attachment(chat.id, attachment.id)
                    with pytest.raises(StateError, match="not admitting work"):
                        await application.create_chat("nested")
                    with pytest.raises(StateError, match="not admitting work"):
                        await events.publish("nested_event")
                    with pytest.raises(AuthorityError, match="revoked"):
                        authority.database_durability_fence()
                    if connection is not None:
                        with pytest.raises((AuthorityError, StateError, StatementError)):
                            connection.exec_driver_sql("SELECT 1")
                    assert application._pending_attachment_ids[chat.id] == selection_before
                    assert events._sequence == event_sequence_before
                finally:
                    if connection is not None:
                        connection.close()

            if surviving_owner:
                assert authority.state is AuthorityState.READY
                release_other.set()
                other.join(15)
                assert not other.is_alive()
                assert other_result == [None]
            assert authority.state is AuthorityState.FAILED_CLOSED
        finally:
            release_other.set()
            if surviving_owner:
                other.join(15)
            _best_effort_terminal_release(authority)

    asyncio.run(scenario())


def test_healthy_nested_app_store_event_and_database_share_one_grant(tmp_path: Path) -> None:
    async def scenario() -> None:
        authority, store, application, events = _open_application(tmp_path / "root")
        subscription = application.subscribe()
        try:
            with store.command_admission():
                grant = authority._grant_context.get()
                assert grant is not None
                assert authority._active_operations == 1
                chat = await application.create_chat("healthy nesting")
                assert authority._grant_context.get() is grant
                assert authority._active_operations == 1
                delivered = await asyncio.wait_for(subscription.__anext__(), 5)
                assert delivered.kind == "chat_created"
                persisted = store.get_chat(chat.id)
                assert persisted is not None
                assert (persisted.id, persisted.title) == (chat.id, chat.title)
                assert authority._grant_context.get() is grant
            assert authority._active_operations == 0
            assert authority.state is AuthorityState.READY
        finally:
            subscription.close()
            _best_effort_terminal_release(authority)

    asyncio.run(scenario())


def test_expired_grant_cannot_drive_checked_out_connection(tmp_path: Path) -> None:
    authority, store, _, _ = _open_application(tmp_path / "root")
    connection = None
    try:
        with authority.operation():
            connection = store._engine.connect()
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        authority.poison("external invalidation while database resource remains")
        assert authority.state is AuthorityState.READY
        assert authority.poison_pending is True
        with pytest.raises((AuthorityError, StateError, StatementError)):
            connection.exec_driver_sql("SELECT 1")
        connection.close()
        connection = None
        assert authority.state is AuthorityState.POISONED
    finally:
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


def test_b1_rooted_connection_convenience_methods_return_guarded_cursors(
    tmp_path: Path,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursors: list[sqlite3.Cursor] = []
    try:
        with authority.operation():
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()

            explicit = connection.cursor()
            cursors.append(explicit)
            assert isinstance(explicit, _RootedCursor)
            assert explicit.execute("SELECT 1").fetchone() == (1,)

            created = connection.execute(
                "CREATE TEMP TABLE b1_convenience(value INTEGER)"
            )
            cursors.append(created)
            assert isinstance(created, _RootedCursor)

            inserted = connection.executemany(
                "INSERT INTO b1_convenience(value) VALUES (?)",
                ((1,), (2,)),
            )
            cursors.append(inserted)
            assert isinstance(inserted, _RootedCursor)

            scripted = connection.executescript(
                "INSERT INTO b1_convenience(value) VALUES (3);"
                "INSERT INTO b1_convenience(value) VALUES (4);"
            )
            cursors.append(scripted)
            assert isinstance(scripted, _RootedCursor)

            selected = connection.execute(
                "SELECT value FROM b1_convenience ORDER BY value"
            )
            cursors.append(selected)
            assert isinstance(selected, _RootedCursor)
            assert selected.fetchall() == [(1,), (2,), (3,), (4,)]

            for factory in (sqlite3.Cursor, type("CustomCursor", (_RootedCursor,), {})):
                with pytest.raises(
                    AuthorityError, match="raw database cursor factories"
                ):
                    connection.cursor(factory)
    finally:
        for cursor in reversed(cursors):
            cursor.close()
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


def test_b1_post_revocation_convenience_cursor_rejects_forward_work(
    tmp_path: Path,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursor = None
    try:
        with authority.operation():
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            cursor = connection.execute(
                "CREATE TEMP TABLE b1_revoked(value INTEGER)"
            )
            assert isinstance(cursor, _RootedCursor)
            changes = connection.total_changes
            grant = authority._grant_context.get()
            assert grant is not None and grant.status.value == "FORWARD"

            authority.poison("B1 revoke retained convenience cursor")
            assert grant.status.value == "CLEANUP"
            assert authority.poison_pending is True
            rejected = False
            try:
                cursor.execute("INSERT INTO b1_revoked(value) VALUES (1)")
            except AuthorityError as exc:
                rejected = True
                assert "owning grant" in str(exc)
            if not rejected:
                assert connection.total_changes == changes, (
                    "revoked convenience cursor performed SQLite DML"
                )
            assert rejected is True

            cursor.close()
            cursor = None
            connection.close()
            connection = None
            assert authority.state is AuthorityState.POISONED
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


@pytest.mark.parametrize("step", ("fetchone", "fetchmany", "fetchall", "next"))
def test_b1_cursor_stepping_rejects_before_sqlite_after_revocation(
    tmp_path: Path,
    step: str,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / step)
    connection = None
    cursor = None
    observed: list[int] = []
    try:
        with authority.operation():
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            connection.create_function(
                "b1_observe",
                1,
                lambda value: observed.append(value) or value,
            )
            cursor = connection.execute(
                "WITH RECURSIVE values_(value) AS ("
                "VALUES(1) UNION ALL SELECT value + 1 FROM values_ WHERE value < 4"
                ") SELECT b1_observe(value) FROM values_"
            )
            observed.clear()
            authority.poison(f"B1 revoke before {step}")

            operation = next if step == "next" else getattr(cursor, step)
            rejected = False
            try:
                operation(cursor) if step == "next" else operation()
            except AuthorityError as exc:
                rejected = True
                assert "owning grant" in str(exc)
            if not rejected:
                assert observed == [], "revoked cursor advanced SQLite"
            assert rejected is True
            assert observed == []

            cursor.close()
            cursor = None
            connection.close()
            connection = None
            assert authority.state is AuthorityState.POISONED
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


def test_b1_cursor_rejects_foreign_released_and_unrelated_grants(
    tmp_path: Path,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursor = None
    observed: list[int] = []
    try:
        with authority.operation():
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            connection.create_function(
                "b1_observe",
                1,
                lambda value: observed.append(value) or value,
            )
            cursor = connection.execute("SELECT b1_observe(1)")
            observed.clear()
            copied = copy_context()
            foreign_result: list[BaseException | None] = []

            def foreign_step() -> None:
                try:
                    copied.run(cursor.fetchone)
                except BaseException as exc:
                    foreign_result.append(exc)
                else:
                    foreign_result.append(None)

            worker = threading.Thread(target=foreign_step, name="b1-foreign-cursor")
            worker.start()
            worker.join(15)
            assert not worker.is_alive()
            assert len(foreign_result) == 1
            assert isinstance(foreign_result[0], AuthorityError)
            assert "another executor" in str(foreign_result[0])
            assert observed == []

        with pytest.raises(AuthorityError, match="owning grant"):
            cursor.fetchone()
        with authority.operation():
            with pytest.raises(AuthorityError, match="owning grant"):
                cursor.fetchone()
        assert observed == []

        cursor.close()
        cursor = None
        connection.close()
        connection = None
        assert authority.state is AuthorityState.READY
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


def test_b1_cursor_and_connection_cleanup_remain_release_only_after_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursor = None
    forward_checks: list[None] = []
    try:
        with authority.operation():
            vfs = authority._vfs
            assert vfs is not None
            assert vfs._thread_unknown_close_generation == 0
            connection = vfs.connect()
            cursor = connection.execute("SELECT 1")
            original_before_effect = _RootedConnection._bots5_before_effect

            def counted_before_effect(self) -> None:
                forward_checks.append(None)
                original_before_effect(self)

            monkeypatch.setattr(
                _RootedConnection,
                "_bots5_before_effect",
                counted_before_effect,
            )
            authority.poison("B1 revoke before cleanup")
            monkeypatch.setattr(
                type(vfs),
                "_thread_unknown_close_generation",
                property(lambda self: 1),
            )

            cursor.close()
            cursor = None
            assert forward_checks == []
            assert authority._pending_invalidation is AuthorityState.FAILED_CLOSED
            assert any(
                label.endswith(":native-open-files") and status == "UNKNOWN"
                for label, status in authority.claim_inventory
            )

            connection.close()
            connection = None
            assert forward_checks == []
            assert authority.state is AuthorityState.FAILED_CLOSED
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


def test_b1_explicit_convenience_and_fetch_paths_share_native_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursors: list[sqlite3.Cursor] = []
    calls: list[str] = []
    try:
        with authority.operation():
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            original_handoff = _RootedConnection._bots5_handoff

            def record_handoff(self, operation: str) -> None:
                calls.append(operation)
                original_handoff(self, operation)

            monkeypatch.setattr(_RootedConnection, "_bots5_handoff", record_handoff)

            explicit = connection.cursor()
            cursors.append(explicit)
            assert calls == ["cursor creation"]
            calls.clear()
            explicit.execute("SELECT 1")
            assert calls == ["cursor execute"]
            calls.clear()
            assert explicit.fetchone() == (1,)
            assert calls == ["cursor fetchone"]

            calls.clear()
            convenience = connection.execute("SELECT 2")
            cursors.append(convenience)
            assert calls == ["cursor creation", "cursor execute"]
            calls.clear()
            assert convenience.fetchone() == (2,)
            assert calls == ["cursor fetchone"]
            assert authority.state is AuthorityState.READY
    finally:
        for cursor in reversed(cursors):
            cursor.close()
        if connection is not None:
            connection.close()
        _best_effort_terminal_release(authority)


@pytest.mark.parametrize("order", ("cursor_first", "connection_first"))
def test_b1_rooted_child_cursor_lifetime_matches_parent_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / order)
    connection = None
    cursor = None
    try:
        with authority.operation():
            grant = authority._grant_context.get()
            assert grant is not None
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            lease = connection._bots5_lease
            cursor = connection.execute("SELECT 1 UNION ALL SELECT 2")
            release_boundary: list[tuple[bool, int, int]] = []
            original_release = type(lease).release

            def observe_parent_release(self) -> None:
                if self is lease:
                    release_boundary.append(
                        (
                            cursor._bots5_closed,
                            len(connection._bots5_live_cursors),
                            vfs.open_count,
                        )
                    )
                original_release(self)

            monkeypatch.setattr(type(lease), "release", observe_parent_release)

            assert len(connection._bots5_live_cursors) == 1
            assert cursor._bots5_closed is False
            assert lease.released is False
            assert len(grant.resources) == 1
            assert vfs.open_count == 1

            if order == "cursor_first":
                cursor.close()
                assert cursor._bots5_closed is True
                assert connection._bots5_live_cursors == {}
                assert lease.released is False
                assert len(grant.resources) == 1
                assert vfs.open_count == 1

            connection.close()
            assert cursor._bots5_closed is True
            assert connection._bots5_live_cursors == {}
            assert lease.released is True
            assert len(grant.resources) == 0
            assert vfs.open_count == 0
            assert release_boundary == [(True, 0, 0)]
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False

            # SQLAlchemy or an explicit owner may still hold the cursor object.
            # Its release-only close remains deterministic and idempotent.
            cursor.close()
            assert vfs.open_count == 0
        assert grant.status.value == "RELEASED"
        assert authority.state is AuthorityState.READY
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        _best_effort_terminal_release(authority)


@pytest.mark.parametrize("order", ("result_first", "connection_first"))
def test_b1_sqlalchemy_result_cleanup_orders_settle_before_parent_release(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    order: str,
) -> None:
    authority, store, _, _ = _open_application(tmp_path / order)
    connection = None
    result = None
    caplog.set_level(logging.ERROR)
    try:
        with authority.operation():
            grant = authority._grant_context.get()
            assert grant is not None
            connection = store._engine.connect()
            result = connection.exec_driver_sql("SELECT 1 UNION ALL SELECT 2")
            raw = connection.connection.driver_connection
            cursor = result.cursor
            lease = raw._bots5_lease
            assert len(raw._bots5_live_cursors) == 1
            assert cursor._bots5_closed is False
            assert authority._checked_out_connections == 1

            if order == "result_first":
                result.close()
                assert cursor._bots5_closed is True
                assert raw._bots5_live_cursors == {}
                assert lease.released is False
                assert len(grant.resources) == 1

            caplog.clear()
            connection.close()
            assert cursor._bots5_closed is True
            assert raw._bots5_live_cursors == {}
            assert lease.released is True
            assert len(grant.resources) == 0
            assert authority._checked_out_connections == 0
            assert authority._vfs.open_count == 0

            if order == "connection_first":
                assert result.closed is False
                result.close()
                assert result.closed is True
            assert not any(
                record.getMessage() == "Error closing cursor"
                for record in caplog.records
            )
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False
    finally:
        if result is not None:
            try:
                result.close()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        _best_effort_terminal_release(authority)


def test_b1_multiple_child_cursors_keep_parent_owned_until_mixed_cleanup(
    tmp_path: Path,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursors: list[_RootedCursor] = []
    try:
        with authority.operation():
            grant = authority._grant_context.get()
            assert grant is not None
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            lease = connection._bots5_lease
            first = connection.execute("SELECT 1 UNION ALL SELECT 2")
            second = connection.execute("SELECT 3 UNION ALL SELECT 4")
            cursors.extend((first, second))
            assert len(connection._bots5_live_cursors) == 2

            first.close()
            assert first._bots5_closed is True
            assert second._bots5_closed is False
            assert len(connection._bots5_live_cursors) == 1
            assert lease.released is False
            assert len(grant.resources) == 1
            assert vfs.open_count == 1

            connection.close()
            assert second._bots5_closed is True
            assert connection._bots5_live_cursors == {}
            assert lease.released is True
            assert len(grant.resources) == 0
            assert vfs.open_count == 0

            first.close()
            second.close()
            assert vfs.open_count == 0
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False
    finally:
        for cursor in reversed(cursors):
            try:
                cursor.close()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        _best_effort_terminal_release(authority)


def test_b1_revoked_owner_connection_first_cleanup_drains_without_forward_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursors: list[_RootedCursor] = []
    try:
        with authority.operation():
            grant = authority._grant_context.get()
            assert grant is not None
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            lease = connection._bots5_lease
            cursors.extend(
                (
                    connection.execute("SELECT 1 UNION ALL SELECT 2"),
                    connection.execute("SELECT 3 UNION ALL SELECT 4"),
                )
            )
            connect_calls = 0
            original_connect = sqlite3.connect

            def counted_connect(*args, **kwargs):
                nonlocal connect_calls
                connect_calls += 1
                return original_connect(*args, **kwargs)

            monkeypatch.setattr(sqlite3, "connect", counted_connect)
            authority.poison("B1 revoke retained child resources")
            assert grant.status.value == "CLEANUP"
            with pytest.raises(AuthorityError, match="owning grant"):
                cursors[0].fetchone()
            with pytest.raises(AuthorityError, match="owning grant"):
                connection.execute("SELECT 5")

            connection.close()
            assert all(cursor._bots5_closed for cursor in cursors)
            assert connection._bots5_live_cursors == {}
            assert lease.released is True
            assert len(grant.resources) == 0
            assert connect_calls == 0
            assert vfs.open_count == 0
            assert authority.state is AuthorityState.POISONED

            for cursor in cursors:
                cursor.close()
            assert connect_calls == 0
            assert vfs.open_count == 0
    finally:
        for cursor in reversed(cursors):
            try:
                cursor.close()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        _best_effort_terminal_release(authority)


def test_b1_child_cleanup_unknown_is_classified_before_parent_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursor = None
    observed: list[tuple[bool, int, int, bool]] = []
    try:
        with authority.operation():
            grant = authority._grant_context.get()
            assert grant is not None
            vfs = authority._vfs
            assert vfs is not None
            assert vfs._thread_unknown_close_generation == 0
            connection = vfs.connect()
            lease = connection._bots5_lease
            cursor = connection.execute("SELECT 1 UNION ALL SELECT 2")
            original_request = authority._request_invalidation

            def observe_invalidation(target, message):
                if "native rooted VFS close outcome" in message:
                    observed.append(
                        (
                            lease.released,
                            len(grant.resources),
                            len(connection._bots5_live_cursors),
                            cursor._bots5_closed,
                        )
                    )
                return original_request(target, message)

            authority._request_invalidation = observe_invalidation
            monkeypatch.setattr(
                type(vfs),
                "_thread_unknown_close_generation",
                property(lambda self: 1),
            )

            connection.close()
            assert observed == [(False, 1, 1, False)]
            assert cursor._bots5_closed is True
            assert connection._bots5_live_cursors == {}
            assert lease.released is True
            assert len(grant.resources) == 0
            assert vfs.open_count == 0
            assert any(
                label.endswith(":native-open-files") and status == "UNKNOWN"
                for label, status in authority.claim_inventory
            )
            assert authority._pending_invalidation is AuthorityState.FAILED_CLOSED
            assert authority.state is AuthorityState.FAILED_CLOSED

            cursor.close()
            with pytest.raises(AuthorityError, match="released"):
                cursor.fetchone()
            assert vfs.open_count == 0
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        _best_effort_terminal_release(authority)


def test_b1_child_close_exception_retains_parent_for_release_only_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A classified child error cannot strand native state outside its lease."""
    authority, _, _, _ = _open_application(tmp_path / "root")
    connection = None
    cursor = None
    try:
        with authority.operation():
            grant = authority._grant_context.get()
            assert grant is not None
            vfs = authority._vfs
            assert vfs is not None
            connection = vfs.connect()
            lease = connection._bots5_lease
            cursor = connection.execute("SELECT 1 UNION ALL SELECT 2")
            original_close = _RootedCursor.close
            failures = 0

            def fail_once(self) -> None:
                nonlocal failures
                if self is cursor and failures == 0:
                    failures += 1
                    self._bots5_connection._bots5_vfs._report_database_uncertainty(
                        "cursor close outcome"
                    )
                    raise sqlite3.OperationalError(
                        "injected consequential cursor close uncertainty"
                    )
                original_close(self)

            monkeypatch.setattr(_RootedCursor, "close", fail_once)
            with pytest.raises(
                sqlite3.OperationalError,
                match="injected consequential cursor close uncertainty",
            ):
                connection.close()
            assert failures == 1
            assert connection._bots5_released is False
            assert connection._bots5_closing is False
            assert cursor._bots5_closed is False
            assert len(connection._bots5_live_cursors) == 1
            assert lease.released is False
            assert len(grant.resources) == 1
            assert vfs.open_count == 1
            assert grant.status.value == "CLEANUP"
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is True
            with pytest.raises(AuthorityError, match="owning grant"):
                cursor.fetchone()

            # The same parent bearer may retry release-only settlement after
            # revocation; no new connection/grant or forward SQL is involved.
            monkeypatch.setattr(_RootedCursor, "close", original_close)
            connection.close()
            assert connection._bots5_released is True
            assert cursor._bots5_closed is True
            assert connection._bots5_live_cursors == {}
            assert lease.released is True
            assert len(grant.resources) == 0
            assert vfs.open_count == 0
            assert authority.state is AuthorityState.POISONED
            cursor.close()
            assert vfs.open_count == 0
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except BaseException:
                pass
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        _best_effort_terminal_release(authority)


def test_public_store_api_coverage_has_callee_grant_wrapper() -> None:
    public_families = {
        *sqlite_store_module._SQLITE_OPERATION_METHODS,
        *sqlite_store_module._PHASE5_OPERATION_METHODS,
    }
    for name in public_families:
        method = getattr(SQLiteAppStateStore, name)
        assert hasattr(method, "__wrapped__"), name

    facade_methods = {
        name
        for name, value in vars(ProviderConfiguration).items()
        if callable(value) and not name.startswith("_")
    }
    for name in facade_methods:
        assert hasattr(getattr(ProviderConfiguration, name), "__wrapped__"), name


def test_logical_claim_bind_failure_retains_uncertain_provisional_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedSocket:
        def bind(self, value) -> None:
            del value
            raise OSError("injected logical bind failure")

        def close(self) -> None:
            raise OSError("injected provisional socket close uncertainty")

    monkeypatch.setattr(authority_module.socket, "socket", lambda *args: FailedSocket())
    authority = DataRootAuthority((tmp_path / "root").absolute())
    with pytest.raises(AuthorityError, match="provisional logical-root close outcome"):
        authority.acquire()
    assert authority.state is AuthorityState.FAILED_CLOSED
    assert ("provisional:logical-root", "UNKNOWN") in authority.claim_inventory
    assert authority.claim_inventory[-1] == ("logical-root", "UNKNOWN")


def test_copied_child_thread_and_stale_context_cannot_borrow_grant(tmp_path: Path) -> None:
    async def scenario() -> None:
        authority, store, application, _ = _open_application(tmp_path / "root")
        try:
            with store.command_admission():
                stale = copy_context()
                child = asyncio.create_task(application.create_chat("copied child"))
                child_result, = await asyncio.gather(child, return_exceptions=True)
                assert isinstance(child_result, StateError)

                def borrowed_thread() -> BaseException | None:
                    try:
                        store.list_chats()
                    except BaseException as exc:
                        return exc
                    return None

                thread_result = await asyncio.to_thread(borrowed_thread)
                assert isinstance(thread_result, StateError)
            with pytest.raises(AuthorityError, match="revoked"):
                stale.run(lambda: authority.operation().__enter__())
            assert authority.state is AuthorityState.READY
            assert store.list_chats() == ()
        finally:
            _best_effort_terminal_release(authority)

    asyncio.run(scenario())


def test_real_full_queue_blocks_delivery_until_owner_settles(tmp_path: Path) -> None:
    async def command_wins() -> None:
        authority, _, application, events = _open_application(
            tmp_path / "wins", queue_size=1
        )
        subscription = application.subscribe()
        try:
            await events.publish("filler")
            assert subscription._queue.maxsize == 1
            assert subscription._queue.full()
            blocked = asyncio.create_task(events.publish("blocked"))
            for _ in range(1000):
                if subscription._queue._putters:
                    break
                await asyncio.sleep(0)
            assert subscription._queue.full()
            assert len(subscription._queue._putters) == 1
            assert not blocked.done()
            authority.poison("external invalidation during blocked delivery")
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is True
            first = await subscription.__anext__()
            assert first.kind == "filler"
            delivered = await asyncio.wait_for(blocked, 5)
            assert delivered.kind == "blocked"
            assert authority.state is AuthorityState.POISONED
            historical = [await subscription.__anext__() for _ in range(events._queue_size)]
            assert historical[-1].kind == "blocked"
        finally:
            subscription.close()
            _best_effort_terminal_release(authority)

    async def cancellation() -> None:
        authority, _, application, events = _open_application(
            tmp_path / "cancel", queue_size=1
        )
        subscription = application.subscribe()
        try:
            for index in range(events._queue_size):
                await events.publish("filler", index=index)
            assert subscription._queue.full()
            blocked = asyncio.create_task(events.publish("cancelled"))
            for _ in range(1000):
                if subscription._queue._putters:
                    break
                await asyncio.sleep(0)
            assert len(subscription._queue._putters) == 1
            blocked.cancel()
            result, = await asyncio.gather(blocked, return_exceptions=True)
            assert isinstance(result, asyncio.CancelledError)
            assert len(subscription._queue._putters) == 0
            assert authority._active_operations == 0
            assert authority.state is AuthorityState.READY
        finally:
            subscription.close()
            _best_effort_terminal_release(authority)

    asyncio.run(command_wins())
    asyncio.run(cancellation())


def test_historical_consumer_and_private_delivery_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        authority, _, application, events = _open_application(tmp_path / "root")
        subscription = application.subscribe()
        try:
            published = await events.publish("historical", value=1)
            authority.poison("invalidate after producer settlement")
            assert authority.state is AuthorityState.POISONED
            consumed = await subscription.__anext__()
            assert consumed == published
            forged = CoreEvent(
                "forged",
                999,
                "forged",
                datetime.now(UTC),
                {},
            )
            with pytest.raises(StateError, match="no issued effect grant"):
                await subscription._deliver(forged)
        finally:
            subscription.close()
            _best_effort_terminal_release(authority)

    asyncio.run(scenario())


def test_required_attachment_payload_integrity_revokes_future_writes(
    tmp_path: Path,
) -> None:
    """I21: detected loss of authority-owned bytes is not a local read error."""
    root = tmp_path / "root"
    authority, store, _, _ = _open_application(root)
    source = tmp_path / "required.txt"
    source.write_bytes(b"required durable payload")
    attachment = store.ingest_attachment(source)
    object_path = root / "attachments" / "objects" / attachment.blob_digest
    object_path.write_bytes(b"corrupt replacement")
    rejected_chat = Chat(
        str(uuid7()),
        "must not persist after I21",
        datetime.now(UTC),
        datetime.now(UTC),
    )
    try:
        with pytest.raises(
            StateError, match="attachment payload does not match durable identity"
        ):
            store.read_attachment_bytes(attachment.id)
        assert authority.state is AuthorityState.POISONED
        assert authority.poison_pending is True
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(rejected_chat)
    finally:
        _best_effort_terminal_release(authority)
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT 1 FROM chats WHERE id = ?", (rejected_chat.id,)
        ).fetchone() is None


@pytest.mark.parametrize(
    "tamper,expected_message",
    (
        (
            "representation_id_missing",
            "selected attachment text representation metadata is inconsistent",
        ),
        (
            "representation_digest_missing",
            "attachment text representation is malformed",
        ),
        (
            "identity_digest_disagree",
            "selected attachment text representation identity disagrees with its digest",
        ),
        (
            "digest_payload_disagree",
            "selected attachment text representation digest mismatches its payload",
        ),
        (
            "invalid_utf8_claimed_text",
            "selected attachment is not valid UTF-8: {attachment_id}",
        ),
        (
            "nul_claimed_text",
            "selected attachment text representation contains NUL: {attachment_id}",
        ),
        (
            "valid_text_marked_ineligible",
            "selected attachment ineligibility metadata disagrees with its payload",
        ),
        (
            "valid_text_with_ineligibility_reason",
            "selected attachment text representation conflicts with ineligibility metadata",
        ),
        (
            "invalid_utf8_wrong_reason",
            "selected attachment ineligibility metadata disagrees with its payload",
        ),
        (
            "nul_wrong_reason",
            "selected attachment ineligibility metadata disagrees with its payload",
        ),
    ),
    ids=(
        "representation_id_missing",
        "representation_digest_missing",
        "identity_digest_disagree",
        "digest_payload_disagree",
        "invalid_utf8_claimed_text",
        "nul_claimed_text",
        "valid_text_marked_ineligible",
        "valid_text_with_ineligibility_reason",
        "invalid_utf8_wrong_reason",
        "nul_wrong_reason",
    ),
)
def test_authoritative_representation_corruption_matrix_revokes_owner_and_future_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    expected_message: str,
) -> None:
    """Every intrinsic persisted representation contradiction revokes its owner."""

    async def scenario() -> tuple[
        Path,
        str,
        str,
        str,
        str,
        tuple[bytes, bytes | None, bytes | None, str | None],
    ]:
        root = tmp_path / tamper
        authority, store, application, events = _open_configured_application(root)
        source = tmp_path / f"{tamper}.bin"
        if tamper in {"invalid_utf8_claimed_text", "invalid_utf8_wrong_reason"}:
            source.write_bytes(b"authoritative invalid UTF-8: \xff\xfe")
        elif tamper in {"nul_claimed_text", "nul_wrong_reason"}:
            source.write_bytes(b"authoritative UTF-8 with NUL\x00inside")
        else:
            source.write_text("authoritative E13 payload", encoding="utf-8")
        chat = await application.create_chat(f"E13 {tamper}")
        attachment = await application.attach_file(source)
        await application.stage_attachment(chat.id, attachment.id)
        if tamper in {"nul_claimed_text", "nul_wrong_reason"}:
            assert attachment.text_representation_id is None
            assert attachment.text_digest is None
            assert attachment.ineligibility_reason == "contains_nul"
        corrupted_row = _corrupt_authoritative_text_representation(
            root,
            attachment.id,
            tamper,
        )

        requests: list[tuple[AuthorityState | str, str]] = []
        original_request = authority._request_invalidation

        def record_request(target: AuthorityState | str, message: str) -> None:
            requests.append((target, message))
            original_request(target, message)

        monkeypatch.setattr(authority, "_request_invalidation", record_request)
        late_title = f"late after {tamper}"
        late_store_chat = Chat(
            str(uuid7()),
            f"late direct store after {tamper}",
            datetime.now(UTC),
            datetime.now(UTC),
        )
        event_sequence = events._sequence
        selection = application._pending_attachment_ids[chat.id]
        checked_out = None
        try:
            with store.command_admission():
                grant = authority._grant_context.get()
                assert grant is not None and grant.status.value == "FORWARD"
                checked_out = store._engine.connect()
                assert checked_out.exec_driver_sql("SELECT 1").scalar_one() == 1

                with pytest.raises(StateError) as detected:
                    await application.send_message(chat.id, "detect E13 corruption")
                assert str(detected.value) == expected_message.format(
                    attachment_id=attachment.id
                )
                if tamper == "invalid_utf8_claimed_text":
                    assert isinstance(detected.value.__cause__, UnicodeDecodeError)
                assert requests == [
                    (
                        AuthorityState.POISONED,
                        "required attachment text representation integrity failure",
                    )
                ]
                assert authority._pending_invalidation is AuthorityState.POISONED
                assert authority.poison_pending is True
                # The held database resource defers terminal publication, but
                # the discovering logical grant has already lost permission.
                assert authority.state is AuthorityState.READY
                assert authority._grant_context.get() is grant
                assert grant.status.value == "CLEANUP"
                assert authority._has_application_admission() is False

                with pytest.raises(StateError, match="not admitting work"):
                    store.create_chat(late_store_chat)
                with pytest.raises(StateError, match="not admitting work"):
                    await application.create_chat(late_title)
                with pytest.raises(StateError, match="not admitting work"):
                    await events.publish("late_e13_event")
                with pytest.raises(AuthorityError, match="revoked"):
                    authority.database_durability_fence()
                with pytest.raises((AuthorityError, StateError, StatementError)):
                    checked_out.exec_driver_sql("SELECT 1")
                assert application._pending_attachment_ids[chat.id] == selection
                assert application._pending_generations == {}
                assert events._sequence == event_sequence

                checked_out.close()
                checked_out = None
                assert authority.state is AuthorityState.POISONED

            with pytest.raises(StateError, match="not admitting work"):
                await application.create_chat(late_title)
        finally:
            if checked_out is not None:
                checked_out.close()
            try:
                await application.close()
            except BaseException:
                pass
            _best_effort_terminal_release(authority)
        return (
            root,
            chat.id,
            attachment.id,
            late_store_chat.id,
            late_title,
            corrupted_row,
        )

    (
        root,
        chat_id,
        attachment_id,
        late_store_chat_id,
        late_title,
        corrupted_row,
    ) = asyncio.run(scenario())
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT revision, head_message_id FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone() == (0, None)
        assert connection.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM generation_attempts WHERE chat_id = ?",
            (chat_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM chats WHERE title = ?",
            (late_title,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM chats WHERE id = ?",
            (late_store_chat_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT blob_digest, text_representation_id, text_digest, "
            "ineligibility_reason FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone() == corrupted_row


def test_e13_late_representation_corruption_preserves_known_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-plan E13 contradiction poisons without suppressing T5 rollback."""

    async def scenario() -> tuple[Path, str]:
        root = tmp_path / "late-e13"
        authority, store, application, _ = _open_configured_application(root)
        source = tmp_path / "late-e13.txt"
        source.write_text("late authoritative E13 payload", encoding="utf-8")
        chat = await application.create_chat("late E13 rollback")
        attachment = await application.attach_file(source)
        await application.stage_attachment(chat.id, attachment.id)
        original_persist = store.persist_generation_start

        def corrupt_then_persist(*args, **kwargs):
            _corrupt_authoritative_text_representation(
                root,
                attachment.id,
                "digest_payload_disagree",
            )
            return original_persist(*args, **kwargs)

        monkeypatch.setattr(store, "persist_generation_start", corrupt_then_persist)
        try:
            with pytest.raises(
                StateError,
                match="text representation digest mismatches its payload",
            ):
                await application.send_message(chat.id, "late E13 corruption")
            assert authority.state is AuthorityState.POISONED
            assert authority.poison_pending is True
        finally:
            try:
                await application.close()
            except BaseException:
                pass
            _best_effort_terminal_release(authority)
        return root, chat.id

    root, chat_id = asyncio.run(scenario())
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT revision, head_message_id FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone() == (0, None)
        assert connection.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM generation_attempts WHERE chat_id = ?",
            (chat_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("operation", ("T5", "T6"))
def test_e13_post_plan_nul_relabel_preserves_t5_t6_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """The shared semantic classifier is repeated at both E13 write paths."""

    async def scenario() -> tuple[Path, str, tuple[object, ...], str]:
        root = tmp_path / operation
        authority, store, application, events = _open_configured_application(root)
        valid_source = tmp_path / f"{operation}-valid.txt"
        nul_source = tmp_path / f"{operation}-nul.bin"
        valid_source.write_text("post-plan valid text", encoding="utf-8")
        nul_source.write_bytes(b"post-plan UTF-8 NUL\x00payload")
        chat = await application.create_chat(f"{operation} NUL rollback")
        attachment = await application.attach_file(valid_source)
        nul_attachment = await application.attach_file(nul_source)
        assert nul_attachment.text_representation_id is None
        assert nul_attachment.text_digest is None
        assert nul_attachment.ineligibility_reason == "contains_nul"
        await application.stage_attachment(chat.id, attachment.id)

        initial_attempt = None
        if operation == "T6":
            initial_attempt = await application.send_message(chat.id, "initial")
            task = application._generation_tasks.get(initial_attempt.id)
            if task is not None:
                await task

        baseline = _phase6_chat_counts(root, chat.id)
        requests: list[tuple[AuthorityState | str, str]] = []
        original_request = authority._request_invalidation

        def record_request(target: AuthorityState | str, message: str) -> None:
            requests.append((target, message))
            original_request(target, message)

        monkeypatch.setattr(authority, "_request_invalidation", record_request)
        method_name = (
            "persist_generation_start"
            if operation == "T5"
            else "persist_regeneration_start"
        )
        original_persist = getattr(store, method_name)

        def corrupt_then_persist(*args, **kwargs):
            _relabel_attachment_to_nul_blob(
                root,
                attachment.id,
                nul_attachment.id,
            )
            return original_persist(*args, **kwargs)

        monkeypatch.setattr(store, method_name, corrupt_then_persist)
        event_sequence = events._sequence
        late_title = f"forbidden after {operation} NUL relabel"
        try:
            with pytest.raises(
                StateError,
                match="text representation contains NUL",
            ):
                if operation == "T5":
                    await application.send_message(chat.id, "post-plan NUL")
                else:
                    assert initial_attempt is not None
                    await application.regenerate_message(
                        chat.id,
                        initial_attempt.assistant_message_id,
                    )
            assert requests == [
                (
                    AuthorityState.POISONED,
                    "required attachment text representation integrity failure",
                )
            ]
            assert authority.state is AuthorityState.POISONED
            assert authority.poison_pending is True
            assert events._sequence == event_sequence
            with pytest.raises(StateError, match="not admitting work"):
                await application.create_chat(late_title)
        finally:
            try:
                await application.close()
            except BaseException:
                pass
            _best_effort_terminal_release(authority)
        return root, chat.id, baseline, late_title

    root, chat_id, baseline, late_title = asyncio.run(scenario())
    assert _phase6_chat_counts(root, chat_id) == baseline
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM chats WHERE title = ?",
            (late_title,),
        ).fetchone() == (0,)


def test_bad_external_and_unknown_attachment_input_do_not_invalidate_authority(
    tmp_path: Path,
) -> None:
    """Bad external paths and unknown IDs remain ordinary request errors."""
    authority, store, _, _ = _open_application(tmp_path / "root")
    missing_id = str(uuid7())
    accepted_chat = Chat(
        str(uuid7()),
        "ordinary error remains admitting",
        datetime.now(UTC),
        datetime.now(UTC),
    )
    try:
        with pytest.raises(StateError):
            store.ingest_attachment(tmp_path / "missing-external-input.txt")
        assert authority.state is AuthorityState.READY
        assert authority.poison_pending is False
        with pytest.raises(StateError, match=f"attachment not found: {missing_id}"):
            store.read_attachment_bytes(missing_id)
        assert authority.state is AuthorityState.READY
        assert authority.poison_pending is False
        store.create_chat(accepted_chat)
        persisted = store.get_chat(accepted_chat.id)
        assert persisted is not None
        assert (persisted.id, persisted.title) == (accepted_chat.id, accepted_chat.title)
    finally:
        _best_effort_terminal_release(authority)


@pytest.mark.parametrize(
    "payload,reason",
    (
        (b"ordinary binary \xff", "invalid_utf8"),
        (b"ordinary text with NUL\x00", "contains_nul"),
    ),
)
def test_absent_text_representation_and_malformed_request_remain_admitting(
    tmp_path: Path,
    payload: bytes,
    reason: str,
) -> None:
    """Ordinary ineligible bytes and malformed request input never poison."""

    async def scenario() -> None:
        root = tmp_path / reason
        authority, store, application, _ = _open_configured_application(root)
        source = tmp_path / f"{reason}.bin"
        source.write_bytes(payload)
        late_title = f"healthy after {reason}"
        try:
            chat = await application.create_chat(f"ordinary {reason}")
            attachment = await application.attach_file(source)
            assert attachment.text_representation_id is None
            assert attachment.text_digest is None
            assert attachment.ineligibility_reason == reason
            assert store.read_attachment_bytes(attachment.id) == payload
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False

            await application.stage_attachment(chat.id, attachment.id)
            with pytest.raises(StateError, match=f"ineligible \\({reason}\\)"):
                await application.send_message(chat.id, "ordinary ineligible input")
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False

            with pytest.raises(StateError, match="message text must not be empty"):
                await application.send_message(chat.id, "  \t")
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False

            accepted = await application.create_chat(late_title)
            assert accepted.title == late_title
        finally:
            await application.close()

    asyncio.run(scenario())


def test_valid_text_and_malformed_context_remain_admitting(tmp_path: Path) -> None:
    """The canonical valid-text branch and ordinary context errors stay healthy."""

    async def scenario() -> None:
        root = tmp_path / "valid-text"
        authority, store, application, _ = _open_configured_application(root)
        source = tmp_path / "ordinary-valid.txt"
        payload = b"ordinary valid UTF-8 text"
        source.write_bytes(payload)
        try:
            chat = await application.create_chat("ordinary valid text")
            attachment = await application.attach_file(source)
            assert attachment.text_representation_id == attachment.blob_digest
            assert attachment.text_digest == attachment.blob_digest
            assert attachment.ineligibility_reason is None
            assert store.read_attachment_bytes(attachment.id) == payload
            assert store.list_attachments() == (attachment,)

            await application.stage_attachment(chat.id, attachment.id)
            attempt = await application.send_message(chat.id, "ordinary valid send")
            task = application._generation_tasks.get(attempt.id)
            if task is not None:
                await task
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False

            with pytest.raises(ContextBuildError, match="context window"):
                await application.build_context_plan(
                    chat.id,
                    parent_message_id=None,
                    current_user=ContextSource(
                        "current",
                        "current_user",
                        "user",
                        "ordinary malformed context",
                    ),
                    builder=ContextBuilder(),
                    context_window=0,
                    context_window_provenance="I21/E13 negative control",
                    output_reserve=0,
                )
            assert authority.state is AuthorityState.READY
            assert authority.poison_pending is False

            accepted = await application.create_chat("healthy after valid text")
            assert accepted.title == "healthy after valid text"
        finally:
            await application.close()

    asyncio.run(scenario())


def test_shared_nul_semantics_reject_false_text_claim_during_startup(
    tmp_path: Path,
) -> None:
    """Startup derives the same contains-NUL truth as ingest and runtime."""
    root = tmp_path / "startup-nul"
    source = tmp_path / "startup-nul.bin"
    source.write_bytes(b"startup UTF-8 with NUL\x00inside")
    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    attachment = store.ingest_attachment(source)
    assert attachment.text_representation_id is None
    assert attachment.text_digest is None
    assert attachment.ineligibility_reason == "contains_nul"
    authority.close()

    _corrupt_authoritative_text_representation(
        root,
        attachment.id,
        "nul_claimed_text",
    )
    reopened = DataRootAuthority(root.absolute()).acquire()
    try:
        with pytest.raises(
            RuntimeError,
            match="durable Phase 6 attachment storage failed verification",
        ):
            reopened.open_store()
        assert reopened.state is AuthorityState.FAILED_STARTUP
    finally:
        reopened.close()


def test_pending_target_escalates_and_cannot_downgrade(tmp_path: Path) -> None:
    authority, store, _, _ = _open_application(tmp_path / "root")
    other_entered = threading.Event()
    release_other = threading.Event()
    other_result: list[BaseException | None] = []

    def other_owner() -> None:
        try:
            with authority.operation():
                other_entered.set()
                assert release_other.wait(15)
        except BaseException as exc:
            other_result.append(exc)
        else:
            other_result.append(None)

    other = threading.Thread(target=other_owner, name="escalation-survivor")
    try:
        with store.command_admission():
            other.start()
            assert other_entered.wait(15)
            authority.poison("ordinary self-failure")
            assert authority._pending_invalidation is AuthorityState.POISONED
            assert authority.state is AuthorityState.READY
            authority._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "terminal cleanup uncertainty",
            )
            authority.poison("attempted downgrade")
            assert authority._pending_invalidation is AuthorityState.FAILED_CLOSED
            assert authority.state is AuthorityState.READY
            release_other.set()
            other.join(15)
            assert other_result == [None]
            assert authority.state is AuthorityState.FAILED_CLOSED
    finally:
        release_other.set()
        other.join(15)
        _best_effort_terminal_release(authority)


def test_invalidated_close_performs_no_new_database_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, store, _, _ = _open_application(tmp_path / "root")
    creator_calls = 0
    original_creator = store._engine.pool._creator

    def counted_creator(*args, **kwargs):
        nonlocal creator_calls
        creator_calls += 1
        return original_creator(*args, **kwargs)

    monkeypatch.setattr(store._engine.pool, "_creator", counted_creator)
    authority.poison("pre-close invalidation")
    assert authority.state is AuthorityState.POISONED
    authority.close()
    assert creator_calls == 0
    assert authority.state is AuthorityState.CLOSED


def test_thread_attributed_native_unknown_handoff_revokes_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _, _, _ = _open_application(tmp_path / "root")
    vfs = authority._vfs
    assert vfs is not None
    monkeypatch.setattr(
        type(vfs),
        "_thread_unknown_close_generation",
        property(lambda self: 1),
    )
    try:
        with authority.operation():
            assert vfs._handoff_unknown_since(0, "test native close") == 1
            assert authority.state is AuthorityState.FAILED_CLOSED
            assert authority.poison_pending is True
            assert any(
                label.endswith(":native-open-files") and status == "UNKNOWN"
                for label, status in authority.claim_inventory
            )
    finally:
        _best_effort_terminal_release(authority)
