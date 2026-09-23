from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import sqlite3
import threading
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import create_engine

from bots5.core.import_queue import ImportQueueError, ImportQueueState, OwnedImportWorkers, QueueItem, can_cancel, reorder, transition
from bots5.core.errors import RevisionConflict, StateError
from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence.archive_import_store import JournalIntent, advance_journal, begin_staging, claim, enqueue, queue_item
from bots5.infrastructure.persistence.transition_guard import arm_phase9_import_continuation_rows, arm_phase9_import_graph, clear_phase9_import_graph
from bots5.domain.models import Chat
from bots5.domain.search import (
    SearchBranchState, SearchDocumentKind, SearchFilters, SearchIndexCondition,
)
from bots5.domain.provider import (
    BackendType, CapabilityKey, CapabilityOverride, CapabilityState,
    GenerationSettings, ProviderProfile,
)
from bots5.providers.discovery import DiscoveredModel, FakeModelDiscoverer
from tests._authority_test_support import upgrade_to
from tests.test_phase9_archive_v2 import _archive, archive_with_continuation_branch, missing_external_archive
from bots5.infrastructure.archive_package import ArchivePackageError, archive_bytes, validate_archive
from bots5.infrastructure.archive_v2 import FEATURES, archive_v2_bytes
from bots5.core.interchange import canonical_json_bytes, canonical_jsonl_bytes, parse_jsonl
import bots5.infrastructure.persistence.sqlite as sqlite
import bots5.infrastructure.attachments as attachment_fs
from bots5.infrastructure.persistence.phase5_validation import validate_phase5_snapshot
from bots5.core.export import (
    ArchiveVersionRequired, AttachmentPolicy, ExportError, build_archive_projection, build_archive_v2_projection,
)
from bots5.core.archive_import import capture_validated_archive, close_payload_snapshots, source_fingerprint
import bots5.core.archive_import as archive_import_core
from tests.test_phase9_archive import _attachment_projection, _repack_canonical_archive
from tests.test_phase6_context_attachments import _configured_application, _finish


def _item(identifier="a", ordinal=0):
    return QueueItem(identifier, 1, ordinal, ImportQueueState.QUEUED, (1, 2, 3, 4, 5))


def test_cancel_and_cutoff_are_revision_linearized():
    item = _item()
    preflight = transition(item, 1, ImportQueueState.PREFLIGHTING)
    cancelled = transition(preflight, 2, ImportQueueState.CANCELLED)
    assert cancelled.state is ImportQueueState.CANCELLED
    cutoff = transition(preflight, 2, ImportQueueState.STAGING, operation_id="operation")
    assert not can_cancel(cutoff)
    with pytest.raises(ImportQueueError, match="revision conflict"):
        transition(cutoff, 2, ImportQueueState.CANCELLED)


def test_sync_constructed_application_drains_persisted_queue_when_loop_starts(tmp_path):
    """Desktop builds synchronously; its first loop turn must resume work."""
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "queued.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=datetime(2026, 9, 20, tzinfo=UTC))
        def current():
            return next(item for item in store.list_archive_imports(limit=100).items if item.id == queued.id)
        assert current().state is ImportQueueState.QUEUED
        async def activate():
            try:
                app._ensure_import_scheduler()
                for _ in range(200):
                    item = current()
                    if item.state is ImportQueueState.COMPLETED:
                        return item
                    await asyncio.sleep(0.01)
                raise AssertionError("synchronous bootstrap queue was not drained")
            finally:
                await app.close()
        assert asyncio.run(activate()).state is ImportQueueState.COMPLETED
    finally:
        if not app._closed:
            store.close()


def test_active_import_grant_rejects_tampered_row_with_a_valid_planned_identity(tmp_path):
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        store.create_chat(Chat("chat", "title", now, now))
        timestamp = "2026-09-19T00:00:00.000Z"
        grant = (
            "planned", "chat", None, 1, "user", "sent", "sealed body",
            timestamp, "lineage", 1, None,
        )
        with store.command_admission():
            with store._engine.begin() as connection:
                arm_phase9_import_graph(
                    connection, "operation", (("chat", "planned-chat"), ("message", "planned")),
                    messages=(grant,), chats=(("planned-chat", "sealed title", timestamp, timestamp, None, 0, None),),
                    links=(("message-attachment", "planned", "planned-ref", 0),),
                    attempts=((
                        "planned-attempt", "chat", "planned-user", "planned-assistant", "source-attempt", "planned-node",
                        "complete", timestamp, timestamp,
                        '{"evidence":"sealed"}', '{"bindings":["sealed"]}',
                    ),),
                    contexts=((
                        "planned-attempt", '{"plan":"sealed"}', "sealed-digest",
                        '{"local_attempt_id":"planned-attempt","source_attempt_id":"source-attempt"}',
                    ),),
                    attachments=((
                        "planned-ref", "operation", "source-ref", "planned-node", None, b"x" * 32, 0,
                        '{"source":"sealed"}', "MISSING_EXTERNAL", timestamp, None,
                    ),),
                    sources=((
                        "planned-chat", "operation", "source-chat", 1, None, "planned-node", timestamp,
                        '{"configuration":"sealed"}', '{"history":"sealed"}',
                    ),),
                    nodes=(("planned-node", "message", "archive", "sealed-digest", "source-message", timestamp, 2, None),),
                    continuation_anchors=(("planned-chat", "empty", None, 1, '{"source":"sealed"}', "UNRESOLVED", '{"evidence":"sealed"}'),),
                    continuation_choices=((
                        "planned-chat", "empty", 1, None, None,
                        '{"max_output_tokens":null,"reasoning_effort":null,"temperature":null,"timeout_seconds":null}',
                        0, "[]", "equivalent", '{"target":"sealed"}', timestamp,
                    ),),
                )
                arm_phase9_import_continuation_rows(
                    connection,
                    requirements=((
                        "planned-chat", "empty", 0, "planned-attempt", None,
                        b"d" * 32, 7, b"r" * 32, "identity", None, "planned-ref",
                    ),),
                    candidates=(("planned-chat", "empty", 0, "planned-ref", None, "planned-ref"),),
                )
                try:
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO chats(id,title,created_at,updated_at,head_message_id,revision,archived_at) "
                            "VALUES ('planned-chat','tampered title',?,?,NULL,0,NULL)",
                            (timestamp, timestamp),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_imported_attempts("
                            "id,chat_id,user_message_id,assistant_message_id,source_attempt_id,source_node_id,"
                            "state,started_at,ended_at,source_attempt,source_evidence_binding) "
                            "VALUES ('planned-attempt','chat','planned-user','planned-assistant','source-attempt',"
                            "'wrong-node','complete',?,?, '{\"evidence\":\"sealed\"}', '{\"bindings\":[\"sealed\"]}')",
                            (timestamp, timestamp),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO messages(id,chat_id,parent_id,sequence,role,state,content,created_at,lineage_id,revision,supersedes_id) "
                            "VALUES ('planned','chat',NULL,1,'user','sent','tampered body',?, 'lineage',1,NULL)",
                            (timestamp,),
                        )
                    for base_key, revision, configuration in (
                        ("empty", 1, '{"source":"tampered"}'),
                        ("wrong-base", 1, '{"source":"sealed"}'),
                        ("empty", 2, '{"source":"sealed"}'),
                    ):
                        with pytest.raises(Exception, match="continuation anchor differs"):
                            connection.exec_driver_sql(
                                "INSERT INTO archive_continuation_anchors(chat_id,base_key,base_message_id,revision,source_configuration,resolution,resolution_evidence) "
                                "VALUES ('planned-chat',?,NULL,?,?,'UNRESOLVED','{\"evidence\":\"sealed\"}')",
                                (base_key, revision, configuration),
                            )
                    for settings, descriptor in (
                        ('{"max_output_tokens":1,"reasoning_effort":null,"temperature":null,"timeout_seconds":null}', '{"target":"sealed"}'),
                        ('{"max_output_tokens":null,"reasoning_effort":null,"temperature":null,"timeout_seconds":null}', '{"target":"tampered"}'),
                    ):
                        with pytest.raises(Exception, match="continuation choice differs"):
                            connection.exec_driver_sql(
                                "INSERT INTO archive_continuation_choices(chat_id,base_key,choice_revision,local_connection_id,local_model_entry_id,explicit_settings,degraded,excluded_refs,decision_kind,safe_target_descriptor,created_at) "
                                "VALUES ('planned-chat','empty',1,NULL,NULL,?,0,'[]','equivalent',?,?)",
                                (settings, descriptor, timestamp),
                            )
                    for digest, representation, source_imported, source_native, bound_ref in (
                        (b"x" * 32, b"r" * 32, "planned-attempt", None, "planned-ref"),
                        (b"d" * 32, b"x" * 32, "planned-attempt", None, "planned-ref"),
                        (b"d" * 32, b"r" * 32, None, "native-attempt", "planned-ref"),
                        (b"d" * 32, b"r" * 32, "planned-attempt", None, "wrong-ref"),
                    ):
                        with pytest.raises(Exception, match="continuation requirement differs"):
                            connection.exec_driver_sql(
                                "INSERT INTO archive_continuation_requirements("
                                "chat_id,base_key,ordinal,source_imported_attempt_id,source_native_attempt_id,expected_digest,expected_size,representation_digest,binding_kind,bound_native_attachment_id,bound_imported_ref_id) "
                                "VALUES ('planned-chat','empty',0,?,?,?,7,?,'identity',NULL,?)",
                                (source_imported, source_native, digest, representation, bound_ref),
                            )
                    for ordinal, candidate_id, native_id, imported_id in (
                        (1, "planned-ref", None, "planned-ref"),
                        (0, "wrong-ref", None, "planned-ref"),
                        (0, "planned-ref", "native-ref", None),
                    ):
                        with pytest.raises(Exception, match="continuation candidate differs"):
                            connection.exec_driver_sql(
                                "INSERT INTO archive_continuation_requirement_candidates("
                                "chat_id,base_key,ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id) "
                                "VALUES ('planned-chat','empty',?,?,?,?)",
                                (ordinal, candidate_id, native_id, imported_id),
                            )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_import_chats("
                            "chat_id,operation_id,source_chat_id,source_chat_revision,source_archived_at,"
                            "source_node_id,imported_at,source_configuration,source_continuation_history) "
                            "VALUES ('planned-chat','operation','source-chat',1,NULL,'wrong-node',?,"
                            "'{\"configuration\":\"sealed\"}','{\"history\":\"sealed\"}')",
                            (timestamp,),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_lineage_nodes("
                            "id,object_kind,archive_id,logical_content_digest,source_object_id,imported_at,source_format,prior_node_id) "
                            "VALUES ('planned-node','message','archive','tampered-digest','source-message',?,2,NULL)",
                            (timestamp,),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_import_chats("
                            "chat_id,operation_id,source_chat_id,source_chat_revision,source_archived_at,"
                            "source_node_id,imported_at,source_configuration,source_continuation_history) "
                            "VALUES ('planned-chat','operation','source-chat',1,NULL,'planned-node',?,"
                            "'{\"configuration\":\"tampered\"}','{\"history\":\"sealed\"}')",
                            (timestamp,),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_import_attachment_refs("
                            "id,operation_id,source_attachment_id,source_node_id,attachment_id,expected_digest,"
                            "expected_size,source_metadata,availability,created_at,healed_at) "
                            "VALUES ('planned-ref','operation','source-ref','planned-node',NULL,zeroblob(32),0,"
                            "'{\"source\":\"tampered\"}','MISSING_EXTERNAL',?,NULL)",
                            (timestamp,),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_import_message_attachment_refs(message_id,attachment_ref_id,ordinal) "
                            "VALUES ('planned','planned-ref',1)"
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_imported_attempts("
                            "id,chat_id,user_message_id,assistant_message_id,source_attempt_id,source_node_id,"
                            "state,started_at,ended_at,source_attempt,source_evidence_binding) "
                            "VALUES ('planned-attempt','chat','planned-user','planned-assistant','source-attempt',"
                            "'planned-node','complete',?,?, '{\"evidence\":\"tampered\"}', '{\"bindings\":[\"sealed\"]}')",
                            (timestamp, timestamp),
                        )
                    with pytest.raises(Exception, match="sealed graph"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_imported_context_plans(attempt_id,source_plan,source_plan_digest,local_bindings) "
                            "VALUES ('planned-attempt','{\"plan\":\"tampered\"}','sealed-digest',"
                            "'{\"local_attempt_id\":\"planned-attempt\",\"source_attempt_id\":\"source-attempt\"}')"
                        )
                finally:
                    clear_phase9_import_graph(connection)
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_reorder_is_dense_and_refuses_nonwaiting_or_stale_rows():
    first, second = _item("a", 0), _item("b", 1)
    reordered = reorder((first, second), ("b", "a"))
    assert [(item.id, item.ordinal, item.revision) for item in reordered] == [("b", 0, 2), ("a", 1, 2)]
    active = transition(first, 1, ImportQueueState.PREFLIGHTING)
    with pytest.raises(ImportQueueError, match="reorderable"):
        reorder((active, second), ("a", "b"))


def test_durable_cutoff_writes_operation_journal_and_reservations_in_one_transaction(tmp_path):
    database = tmp_path / "state.sqlite3"
    upgrade_to(database, "0012_phase9_archive_import")
    engine = create_engine(f"sqlite:///{database}", future=True)
    try:
        with engine.begin() as connection:
            item = _item()
            enqueue(connection, item, source_path="/temporary/archive.botsarchive", resolver_roots=("/temporary",), options={"import_as_archived": False}, enqueued_at="2026-09-19T00:00:00Z")
            claimed = claim(connection, "a", 1, now="2026-09-19T00:00:01Z", owner_epoch="epoch")
            staged = begin_staging(connection, "a", claimed.revision, JournalIntent("operation", "archive", 2, "a" * 64, "source-chat", 2, b"x" * 32, 3, "2026-09-19T00:00:02Z", "2026-09-19T00:00:02Z", b"y" * 32, (), ({"digest": b"z" * 32, "size": 4},)))
            assert staged.state is ImportQueueState.STAGING
            advance_journal(connection, "operation", "PAYLOADS_STAGED", updated_at="2026-09-19T00:00:03Z")
        with engine.connect() as connection:
            assert queue_item(connection, "a").state is ImportQueueState.STAGING
            assert connection.exec_driver_sql("SELECT phase,sequence FROM archive_import_journal").fetchone() == ("PAYLOADS_STAGED", 2)
            assert connection.exec_driver_sql("SELECT publication_state FROM archive_import_payload_reservations").fetchone() == ("PLANNED",)
    finally:
        engine.dispose()


def test_authority_owned_intake_capture_validation_and_durable_cutoff(tmp_path):
    """Exercise the production authority/store boundary, not a helper Engine."""
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(
            source,
            resolver_roots=(intake_root,),
            now=now,
            queue_id="authority-queue",
        )
        assert queued.state is ImportQueueState.QUEUED
        staged = store.stage_archive_import(
            queued.id,
            now=now,
            operation_id="authority-operation",
        )
        assert staged.state is ImportQueueState.STAGING
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,archive_version,source_chat_id FROM archive_import_operations"
                ).fetchone() == ("STAGING", 2, "chat")
                assert connection.exec_driver_sql(
                    "SELECT phase,sequence FROM archive_import_journal"
                ).fetchone() == ("PREFLIGHT_SEALED", 1)
                inventory = json.loads(connection.exec_driver_sql(
                    "SELECT graph_inventory FROM archive_import_journal"
                ).scalar_one())
                assert [(row["object_kind"], row["source_id"]) for row in inventory["inventory"]] == [
                    ("chat", "chat"), ("message", "assistant"), ("message", "user"),
                    ("lineage", "assistant"), ("lineage", "user"), ("attempt", "attempt"),
                ]
                assert len({row["local_id"] for row in inventory["inventory"]}) == len(inventory["inventory"])
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_stage_failure_closes_all_owned_payload_snapshots(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "payload.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=b"stage snapshot")))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = None
    closed: list[bool] = []
    try:
        store = authority.open_store(); now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        def fail(*args, **kwargs):
            raise sqlite.ArchiveImportStoreError("injected stage failure")
        original = sqlite.close_payload_snapshots
        def observe(plan):
            original(plan); closed.extend(payload.handle.closed for _, payload in plan.payloads)
        monkeypatch.setattr(sqlite, "begin_staging", fail)
        monkeypatch.setattr(sqlite, "close_payload_snapshots", observe)
        with pytest.raises(StateError, match="injected stage failure"):
            store.stage_archive_import(queued.id, now=now)
        assert closed and all(closed)
    finally:
        if store is not None: store.close()
        else: authority.close()


def test_exact_plan_publishes_one_complete_imported_graph_and_terminal_marker(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake_root,), now=now, queue_id="complete-queue"
        )
        chat_id = store.execute_archive_import(
            queued.id, now=now, operation_id="complete-operation"
        )
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,local_chat_id FROM archive_import_operations"
                ).fetchone() == ("COMMITTED", chat_id)
                assert connection.exec_driver_sql(
                    "SELECT state FROM archive_import_queue WHERE id=?", (queued.id,)
                ).fetchone() == ("COMPLETED",)
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM generation_attempts"
                ).scalar_one() == 0
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_imported_attempts"
                ).scalar_one() == 1
                assert connection.exec_driver_sql(
                    "SELECT head_message_id,revision FROM chats WHERE id=?", (chat_id,)
                ).fetchone()[1] == 1
                anchors = connection.exec_driver_sql(
                    "SELECT base_key,base_message_id,revision,resolution,resolution_evidence "
                    "FROM archive_continuation_anchors WHERE chat_id=? ORDER BY base_key", (chat_id,)
                ).fetchall()
                assert [row[0] for row in anchors] == [store.get_chat(chat_id).head_message_id, "empty"]
                anchor = anchors[0]
                assert anchor[0] == store.get_chat(chat_id).head_message_id
                assert anchor[1] is not None and anchor[2:4] == (1, "UNRESOLVED")
                assert json.loads(anchor[4])["kind"] == "imported-source"
        assert store.recover_archive_imports(now=now) == (("complete-operation", "COMMITTED"),)
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_interrupted_preflight_releases_claim_to_queued_but_invalid_archive_fails(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    try:
        interrupted = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        with pytest.raises(StateError, match="RESOLUTION_CANCELLED"):
            store.preflight_archive_import(interrupted.id, cancelled=lambda: True)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,started_at,finished_at,failure_code FROM archive_import_queue WHERE id=?", (interrupted.id,),
                ).one() == ("QUEUED", None, None, None)
                assert connection.exec_driver_sql(
                    "SELECT claimed_queue_id,owner_epoch FROM archive_import_queue_control"
                ).one() == (None, None)
        invalid_source = intake / "invalid.botsarchive"; invalid_source.write_bytes(b"not an archive")
        invalid = store.enqueue_archive_import(invalid_source, resolver_roots=(intake,), now=now)
        with pytest.raises(StateError, match="ARCHIVE_INVALID"):
            store.preflight_archive_import(invalid.id)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,failure_code FROM archive_import_queue WHERE id=?", (invalid.id,),
                ).one() == ("FAILED", "ARCHIVE_INVALID")
                assert connection.exec_driver_sql(
                    "SELECT claimed_queue_id,owner_epoch FROM archive_import_queue_control"
                ).one() == (None, None)
    finally:
        store.close()


def test_queue_page_revision_rejects_stale_reorder_after_concurrent_enqueue(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        first = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="first")
        second = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="second")
        first_page = store.list_archive_imports(limit=1)
        second_page = store.list_archive_imports(limit=1, cursor=first_page.next_cursor)
        assert first_page.next_cursor is not None
        assert first_page.queue_revision == second_page.queue_revision
        third = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="third")
        with pytest.raises(StateError, match="reorder revision conflict"):
            store.reorder_archive_imports(first_page.queue_revision, (second.id, first.id), now=now)
        fresh = store.list_archive_imports()
        reordered = store.reorder_archive_imports(
            fresh.queue_revision, (third.id, second.id, first.id), now=now,
        )
        assert [item.id for item in reordered] == [third.id, second.id, first.id]
        assert store.list_archive_imports().queue_revision == fresh.queue_revision + 1
    finally:
        store.close()


def test_clear_rejects_an_active_claim_and_startup_reclaims_only_orphaned_preflight(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    preflight = None
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="orphan")
        preflight = store.preflight_archive_import(queued.id)
        with pytest.raises(StateError, match="only terminal import history may clear"):
            store.clear_archive_import_history((queued.id,))
        preflight.close()
    finally:
        store.close()
    reopened_authority = DataRootAuthority(root).acquire(); reopened = reopened_authority.open_store()
    try:
        page = reopened.list_archive_imports()
        item = next(item for item in page.items if item.id == "orphan")
        assert item.state is ImportQueueState.QUEUED
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT claimed_queue_id,owner_epoch FROM archive_import_queue_control"
                ).one() == (None, None)
    finally:
        reopened.close()


def test_graph_commit_return_uncertainty_poisoned_without_graph_replay(tmp_path, monkeypatch):
    """Commit may have reached SQLite when the caller loses its return path."""
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire()
    store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="uncertain-queue")

    def die(point):
        if point == "after-attachment-commit:archive import graph commit":
            raise OSError("injected post-commit return loss")

    monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", die)
    try:
        with pytest.raises(StateError, match="outcome is uncertain"):
            store.execute_archive_import(queued.id, now=now, operation_id="uncertain-operation")
        with pytest.raises(StateError, match="requires controlled restart|not admitting"):
            store.recover_archive_imports(now=now)
    finally:
        monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", None)
        store.close()

    reopened_authority = DataRootAuthority(root).acquire()
    reopened = reopened_authority.open_store()
    try:
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,local_chat_id FROM archive_import_operations WHERE id='uncertain-operation'"
                ).fetchone()[0] == "COMMITTED"
                assert connection.exec_driver_sql(
                    "SELECT state FROM archive_import_queue WHERE id='uncertain-queue'"
                ).scalar_one() == "COMPLETED"
        assert reopened.recover_archive_imports(now=now) == (("uncertain-operation", "COMMITTED"),)
    finally:
        reopened.close()


def test_graph_commit_close_uncertainty_poisoned_without_graph_replay(tmp_path, monkeypatch):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire()
    store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="close-queue")

    def die(point):
        if point == "after-attachment-close:archive import graph commit":
            raise OSError("injected post-close return loss")

    monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", die)
    try:
        with pytest.raises(StateError, match="close is uncertain"):
            store.execute_archive_import(queued.id, now=now, operation_id="close-operation")
        with pytest.raises(StateError, match="requires controlled restart|not admitting"):
            store.recover_archive_imports(now=now)
    finally:
        monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", None)
        store.close()
    reopened_authority = DataRootAuthority(root).acquire()
    reopened = reopened_authority.open_store()
    try:
        assert reopened.recover_archive_imports(now=now) == (("close-operation", "COMMITTED"),)
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("fault_point", "error"),
    (
        ("after-attachment-commit:archive import durable cutoff", "outcome is uncertain"),
        ("after-attachment-close:archive import durable cutoff", "close is uncertain"),
    ),
    ids=("commit-return", "connection-close"),
)
def test_cutoff_close_uncertainty_closes_embedded_snapshots_and_recovers_known_no_graph(
    tmp_path, monkeypatch, fault_point, error,
):
    """A lost cutoff commit or close never transfers live handles to recovery."""
    intake_root = tmp_path / "intake"; intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=b"cutoff close snapshot")))
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = None
    closed: list[bool] = []
    original_close = sqlite.close_payload_snapshots

    def observe(plan):
        original_close(plan)
        closed.extend(snapshot.handle.closed for _, snapshot in plan.payloads)

    def die(point):
        if point == fault_point:
            raise OSError("injected post-cutoff durable return loss")

    try:
        store = authority.open_store()
        now = datetime(2026, 9, 20, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="cutoff-close")
        monkeypatch.setattr(sqlite, "close_payload_snapshots", observe)
        monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", die)
        with pytest.raises(StateError, match=error):
            store.execute_archive_import(queued.id, now=now, operation_id="cutoff-close-operation")
        assert closed and all(closed)
        with pytest.raises(StateError, match="requires controlled restart|not admitting"):
            store.recover_archive_imports(now=now)
    finally:
        monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", None)
        if store is not None: store.close()
        if authority is not None: authority.close()

    reopened_authority = DataRootAuthority(root).acquire()
    reopened = reopened_authority.open_store()
    try:
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM chats").scalar_one() == 0
                assert connection.exec_driver_sql(
                    "SELECT state,failure_code FROM archive_import_operations WHERE id='cutoff-close-operation'"
                ).one() == ("FAILED", "RECOVERED_NO_GRAPH")
                assert connection.exec_driver_sql(
                    "SELECT phase FROM archive_import_journal WHERE operation_id='cutoff-close-operation'"
                ).scalar_one() == "FAILED"
    finally:
        reopened.close()
        reopened_authority.close()


def test_cutoff_rollback_failure_still_closes_embedded_snapshots(tmp_path, monkeypatch):
    """A rollback classification failure cannot bypass capture-resource cleanup."""
    intake_root = tmp_path / "intake"; intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=b"rollback snapshot")))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = None
    closed: list[bool] = []
    original_close = sqlite.close_payload_snapshots

    def observe(plan):
        original_close(plan)
        closed.extend(snapshot.handle.closed for _, snapshot in plan.payloads)

    try:
        store = authority.open_store()
        now = datetime(2026, 9, 20, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="rollback-close")
        preflight = store.preflight_archive_import(queued.id, owner_epoch="rollback-close-operation")
        def stage_failure(*args, **kwargs):
            raise sqlite.ArchiveImportStoreError("injected staging rejection")
        def rollback_failure(connection, operation):
            store._poison_attachment_lifecycle("injected rollback uncertainty")
            raise StateError("injected rollback uncertainty")
        monkeypatch.setattr(sqlite, "begin_staging", stage_failure)
        monkeypatch.setattr(store, "_rollback_attachment_transaction", rollback_failure)
        monkeypatch.setattr(sqlite, "close_payload_snapshots", observe)
        with pytest.raises(StateError, match="injected rollback uncertainty"):
            store.cross_archive_import_cutoff(
                preflight, now=now, operation_id="rollback-close-operation",
            )
        assert closed and all(closed)
        with pytest.raises(StateError, match="requires controlled restart|not admitting"):
            store.recover_archive_imports(now=now)
    finally:
        if store is not None: store.close()
        authority.close()


def test_graph_close_uncertainty_closes_embedded_snapshots_and_recovers_once(tmp_path, monkeypatch):
    """A committed graph with a lost close return remains recoverable without replaying bytes."""
    intake_root = tmp_path / "intake"; intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=b"graph close snapshot")))
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = None
    closed: list[bool] = []
    original_close = sqlite.close_payload_snapshots

    def observe(plan):
        original_close(plan)
        closed.extend(snapshot.handle.closed for _, snapshot in plan.payloads)

    def die(point):
        if point == "after-attachment-close:archive import graph commit":
            raise OSError("injected post-graph-close return loss")

    try:
        store = authority.open_store()
        now = datetime(2026, 9, 20, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="graph-close")
        monkeypatch.setattr(sqlite, "close_payload_snapshots", observe)
        monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", die)
        with pytest.raises(StateError, match="close is uncertain"):
            store.execute_archive_import(queued.id, now=now, operation_id="graph-close-operation")
        assert closed and all(closed)
        with pytest.raises(StateError, match="requires controlled restart|not admitting"):
            store.recover_archive_imports(now=now)
    finally:
        monkeypatch.setattr(sqlite, "_TEST_FAULT_HOOK", None)
        if store is not None: store.close()
        if authority is not None: authority.close()

    reopened_authority = DataRootAuthority(root).acquire()
    reopened = reopened_authority.open_store()
    try:
        assert reopened.recover_archive_imports(now=datetime(2026, 9, 20, tzinfo=UTC)) == (
            ("graph-close-operation", "COMMITTED"),
        )
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM chats").scalar_one() == 1
                assert connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM archive_import_operations WHERE id='graph-close-operation'"
                ).scalar_one() == 1
    finally:
        reopened.close()
        reopened_authority.close()


def test_import_reopens_after_legal_archive_without_losing_source_snapshot(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire()
    store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="evolve-queue")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="evolve-operation")
        archived = store.archive_chat(chat_id, now)
        assert archived.archived_at == now
    finally:
        store.close()
    reopened_authority = DataRootAuthority(root).acquire()
    reopened = reopened_authority.open_store()
    try:
        assert reopened.recover_archive_imports(now=now) == (("evolve-operation", "COMMITTED"),)
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                source_id, source_revision, source_history = connection.exec_driver_sql(
                    "SELECT source_chat_id,source_chat_revision,source_continuation_history "
                    "FROM archive_import_chats WHERE chat_id=?", (chat_id,)
                ).fetchone()
                assert (source_id, source_revision) == ("chat", 1)
                assert json.loads(source_history)["active_head_message_id"] == "assistant"
    finally:
        reopened.close()


def test_continuation_choice_rejects_malformed_settings_and_exclusions(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="choice-queue")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="choice-operation")
        with pytest.raises(StateError, match="temperature"):
            store.admit_import_continuation_choice(chat_id, "empty", expected_choice_revision=0,
                connection_id="none", model_entry_id="none",
                explicit_settings={"temperature": 3, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None},
                excluded_refs=(), now=now)
        with pytest.raises(StateError, match="malformed"):
            store.admit_import_continuation_choice(chat_id, "empty", expected_choice_revision=0,
                connection_id="none", model_entry_id="none",
                explicit_settings={"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None},
                excluded_refs=({"bad": "shape"},), now=now)
    finally:
        store.close()


def test_continuation_choice_is_append_only_and_reopens_without_provider_mutation(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    settings = {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        providers_before = tuple(store.list_provider_connections())
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="choice-success")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="choice-success-op")
        assert store.admit_import_continuation_choice(chat_id, "empty", expected_choice_revision=0,
            connection_id=connection.id, model_entry_id=model.id, explicit_settings=settings, excluded_refs=(), now=now) == 1
        assert store.admit_import_continuation_choice(chat_id, "empty", expected_choice_revision=1,
            connection_id=connection.id, model_entry_id=model.id, explicit_settings=settings, excluded_refs=(), now=now) == 2
        assert tuple(store.list_provider_connections()) == providers_before
        with pytest.raises(RevisionConflict):
            store.admit_import_continuation_choice(chat_id, "empty", expected_choice_revision=0,
                connection_id=connection.id, model_entry_id=model.id, explicit_settings=settings, excluded_refs=(), now=now)
        with pytest.raises(StateError, match="runnable"):
            store.admit_import_continuation_choice(chat_id, "empty", expected_choice_revision=2,
                connection_id="missing", model_entry_id=model.id, explicit_settings=settings, excluded_refs=(), now=now)
    finally:
        store.close()
    reopened_authority = DataRootAuthority(root).acquire(); reopened = reopened_authority.open_store()
    try:
        with reopened.command_admission():
            with reopened._engine.connect() as db:
                rows = db.exec_driver_sql(
                    "SELECT choice_revision,local_connection_id,local_model_entry_id FROM archive_continuation_choices WHERE base_key='empty' ORDER BY choice_revision"
                ).fetchall()
                assert [row[0] for row in rows] == [1, 2]
                assert all(row[1:] == (connection.id, model.id) for row in rows)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_application_reads_and_chooses_import_continuation_without_generation(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    root = tmp_path / "root"; authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    settings = {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="app-choice")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="app-choice-op")
        app = BotsApplication(store, EventBus(SystemClock(), Uuid7Factory()), object())
        empty = await app.import_continuation_readiness(chat_id, "empty")
        terminal = await app.import_continuation_readiness(chat_id, store.get_chat(chat_id).head_message_id)
        assert empty is not None and terminal is not None and empty.choice_revision == 0
        messages_before = len(store.list_messages(chat_id)); attempts_before = len(store.list_generation_attempts(chat_id)); providers_before = tuple(store.list_provider_connections())
        connection = providers_before[0]; model = store.list_model_catalogue_entries(connection.id)[0]
        assert await app.choose_import_continuation(chat_id, "empty", expected_choice_revision=0, connection_id=connection.id, model_entry_id=model.id, explicit_settings=settings, excluded_refs=()) == 1
        assert len(store.list_messages(chat_id)) == messages_before and len(store.list_generation_attempts(chat_id)) == attempts_before and tuple(store.list_provider_connections()) == providers_before
    finally:
        store.close()


@pytest.mark.asyncio
async def test_application_queue_scheduler_executes_without_public_lifecycle_command(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    try:
        first = await app.enqueue_archive_import(source)
        archived = await app.enqueue_archive_import(source, import_as_archived=True)
        assert not hasattr(app, "execute_archive_import")
        for _ in range(200):
            page = store.list_archive_imports()
            states = {item.id: item.state for item in page.items}
            if states.get(first.id) is ImportQueueState.COMPLETED and states.get(archived.id) is ImportQueueState.COMPLETED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("private queue scheduler did not settle both imports")
        with store.command_admission():
            with store._engine.connect() as db:
                rows = {
                    str(item[0]): (str(item[1]), json.loads(str(item[2])))
                    for item in db.exec_driver_sql(
                        "SELECT id,state,options FROM archive_import_queue"
                    ).fetchall()
                }
                assert len(rows) == 2
                assert rows[first.id][0] == "COMPLETED"
                assert rows[first.id][1] == {"import_as_archived": False}
                assert rows[archived.id] == ("COMPLETED", {"import_as_archived": True})
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_application_queue_commands_page_remove_retry_and_clear_preserves_provenance(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    try:
        first = await app.enqueue_archive_import(source)
        second = await app.enqueue_archive_import(source)
        third = await app.enqueue_archive_import(source)
        page = await app.list_archive_imports(limit=2)
        assert len(page.items) == 2 and page.next_cursor is not None
        assert len((await app.list_archive_imports(limit=2, cursor=page.next_cursor)).items) == 1
        removed = await app.remove_waiting_archive_import(second.id, expected_revision=second.revision)
        assert removed.state is ImportQueueState.CANCELLED
        with pytest.raises(StateError):
            await app.remove_waiting_archive_import(removed.id, expected_revision=removed.revision)
        retried = await app.retry_archive_import(removed.id)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT retry_of FROM archive_import_queue WHERE id=?", (retried.id,)).scalar_one() == removed.id
        for _ in range(200):
            first_state = next(
                item.state for item in store.list_archive_imports().items if item.id == first.id
            )
            if first_state is ImportQueueState.COMPLETED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("private queue scheduler did not settle the queued import")
        with store.command_admission():
            with store._engine.connect() as connection:
                chat_id = connection.exec_driver_sql(
                    "SELECT local_chat_id FROM archive_import_operations WHERE id="
                    "(SELECT operation_id FROM archive_import_queue WHERE id=?)", (first.id,),
                ).scalar_one()
        await app.clear_archive_import_history((first.id,))
        assert store.get_chat(chat_id) is not None
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT count(*) FROM archive_import_chats WHERE chat_id=?", (chat_id,)).scalar_one() == 1
                assert connection.exec_driver_sql("SELECT count(*) FROM archive_import_queue WHERE id=?", (first.id,)).scalar_one() == 0
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_cancellation_before_durable_cutoff_leaves_no_journal_or_graph(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    entered = threading.Event(); release = threading.Event()
    original = store.cross_archive_import_cutoff

    def gate(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "cross_archive_import_cutoff", gate)
    try:
        queued = await app.enqueue_archive_import(source)
        assert await asyncio.to_thread(entered.wait, 3)
        with store.command_admission():
            with store._engine.connect() as connection:
                preflight_revision = connection.exec_driver_sql(
                    "SELECT queue_revision FROM archive_import_queue WHERE id=?", (queued.id,),
                ).scalar_one()
        cancelled = await app.cancel_archive_import(
            queued.id, expected_revision=int(preflight_revision),
        )
        assert cancelled.state is ImportQueueState.CANCELLED
        release.set()
        for _ in range(100):
            if next(item for item in store.list_archive_imports().items if item.id == queued.id).state is ImportQueueState.CANCELLED:
                break
            await asyncio.sleep(0.01)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,operation_id FROM archive_import_queue WHERE id=?", (queued.id,),
                ).one() == ("CANCELLED", None)
                assert connection.exec_driver_sql("SELECT count(*) FROM archive_import_operations").scalar_one() == 0
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_cancellation_after_cutoff_linearization_drains_the_exact_plan(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    entered = threading.Event(); release = threading.Event()
    original = sqlite.begin_staging

    def barrier(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite, "begin_staging", barrier)
    try:
        queued = await app.enqueue_archive_import(source)
        assert await asyncio.to_thread(entered.wait, 3)
        # The cutoff holds the authority transaction until its CAS returns;
        # release it before issuing the separately admitted cancellation.
        release.set()
        with store.command_admission():
            with store._engine.connect() as connection:
                revision = int(connection.exec_driver_sql(
                    "SELECT queue_revision FROM archive_import_queue WHERE id=?", (queued.id,)
                ).scalar_one())
        with pytest.raises(StateError, match="cancellation cutoff"):
            await app.cancel_archive_import(queued.id, expected_revision=revision)
        for _ in range(200):
            if next(item for item in store.list_archive_imports().items if item.id == queued.id).state is ImportQueueState.COMPLETED:
                break
            await asyncio.sleep(0.01)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state FROM archive_import_queue WHERE id=?", (queued.id,),
                ).scalar_one() == "COMPLETED"
                assert connection.exec_driver_sql(
                    "SELECT state FROM archive_import_operations"
                ).scalar_one() == "COMMITTED"
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_cancellation_waits_for_cutoff_cas_without_blocking_the_event_loop(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    entered = threading.Event(); release = threading.Event()
    original = sqlite.begin_staging

    def gate(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite, "begin_staging", gate)
    try:
        queued = await app.enqueue_archive_import(source)
        assert await asyncio.to_thread(entered.wait, 3)
        cancellation = asyncio.create_task(
            app.cancel_archive_import(queued.id, expected_revision=queued.revision + 1)
        )
        await asyncio.sleep(0)
        assert not cancellation.done()
        # This runs on the event loop while the cancellation CAS is waiting
        # behind the cutoff owner. A synchronous application call would never
        # reach this release point.
        release.set()
        with pytest.raises(StateError, match="cancellation cutoff"):
            await cancellation
        for _ in range(200):
            if next(item for item in store.list_archive_imports().items if item.id == queued.id).state is ImportQueueState.COMPLETED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("post-cutoff settlement did not drain")
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_shutdown_drains_owned_settlement_without_blocking_the_event_loop(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    entered = threading.Event(); release = threading.Event()
    original = store.settle_archive_import

    def gate(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "settle_archive_import", gate)
    try:
        queued = await app.enqueue_archive_import(source)
        assert await asyncio.to_thread(entered.wait, 3)
        closing = asyncio.create_task(app.close())
        await asyncio.sleep(0)
        assert not closing.done()
        # The close driver is awaiting the owned settlement, so this loop turn
        # must remain available to release the real worker barrier.
        release.set()
        await closing
        assert store._closed
    finally:
        release.set()
        if not store._closed:
            await app.close()


@pytest.mark.asyncio
async def test_reopen_rejects_branch_provenance_on_ordinary_native_v3_attempt(tmp_path):
    root = tmp_path / "root"
    app, store, authority = _configured_application(root)
    try:
        chat = await app.create_chat("ordinary native v3")
        attempt = await app.send_message(chat.id, "ordinary request")
        await _finish(app, attempt.id)
        completed = store.get_generation_attempt(attempt.id)
        assert completed is not None
        snapshot = json.loads(completed.request_snapshot)
        assert "branch" not in snapshot["settings_provenance"].values()
        snapshot["settings_provenance"]["temperature"] = "branch"
        store.close(); authority.close()
        # Simulate an out-of-process durable corruption.  Ordinary raw DML
        # through the rooted store is already rejected by its lifecycle
        # trigger; reopening must independently reject persisted corruption.
        database = sqlite3.connect(root / "database" / "state.sqlite3")
        try:
            triggers = database.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='generation_attempts' ORDER BY name"
            ).fetchall()
            assert triggers and all(sql is not None for _, sql in triggers)
            for name, _ in triggers:
                database.execute(f'DROP TRIGGER "{name.replace('"', '""')}"')
            database.execute(
                "UPDATE generation_attempts SET request_snapshot=? WHERE id=?",
                (json.dumps(snapshot, sort_keys=True, separators=(",", ":")), attempt.id),
            )
            for _, definition in triggers:
                database.execute(definition)
            assert database.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='generation_attempts' ORDER BY name"
            ).fetchall() == triggers
            database.commit()
        finally:
            database.close()
        authority = DataRootAuthority(root).acquire()
        with pytest.raises(RuntimeError, match="branch settings provenance lacks an admitted branch"):
            authority.open_store()
    finally:
        if store is not None:
            store.close()


@pytest.mark.asyncio
async def test_public_imported_regeneration_uses_choice_without_chat_global_selection(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 19, tzinfo=UTC)
    settings = {"temperature": 0.25, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="public-regen")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="public-regen-op")
        imported_head = store.get_chat(chat_id).head_message_id
        connection = store.list_provider_connections()[0]; model = store.list_model_catalogue_entries(connection.id)[0]
        with store.command_admission():
            with store._engine.connect() as db:
                source_before = db.exec_driver_sql(
                    "SELECT id,source_attempt,source_evidence_binding FROM archive_imported_attempts WHERE chat_id=?",
                    (chat_id,),
                ).one()
        assert await app.choose_import_continuation(chat_id, imported_head, expected_choice_revision=0, connection_id=connection.id, model_entry_id=model.id, explicit_settings=settings, excluded_refs=()) == 1
        attempt = await app.regenerate_message(chat_id, imported_head)
        await _finish(app, attempt.id)
        completed = store.get_generation_attempt(attempt.id)
        assert completed is not None
        snapshot = json.loads(completed.request_snapshot)
        assert snapshot["snapshot_version"] == 3
        assert snapshot["context_plan"]["version"] == 3
        assert snapshot["effective_settings"]["temperature"] == 0.25
        assert snapshot["settings_provenance"]["temperature"] == "branch"
        # The additive provenance term is accepted only through the v3
        # continuation validator.  Recasting it as an ordinary v2 snapshot
        # must continue to fail closed.
        ordinary = dict(snapshot)
        ordinary["snapshot_version"] = 2
        ordinary.pop("context_plan")
        ordinary.pop("settings_revisions")
        with pytest.raises(ValueError, match="provenance"):
            validate_phase5_snapshot(
                json.dumps(ordinary, sort_keys=True),
                attempt_id=completed.id, chat_id=completed.chat_id,
                user_message_id=completed.user_message_id,
                backend_id=completed.backend_id, model=completed.model,
                provider_id=completed.provider_id,
            )
        assert store.get_chat_model_selection(chat_id) is None
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql("SELECT count(*) FROM archive_continuation_branches WHERE attempt_id=?", (attempt.id,)).scalar_one() == 1
                assert db.exec_driver_sql("SELECT count(*) FROM archive_imported_attempts WHERE chat_id=?", (chat_id,)).scalar_one() == 1
                assert db.exec_driver_sql(
                    "SELECT id,source_attempt,source_evidence_binding FROM archive_imported_attempts WHERE chat_id=?",
                    (chat_id,),
                ).one() == source_before
                assert db.exec_driver_sql(
                    "SELECT explicit_settings FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=1",
                    (chat_id, imported_head),
                ).scalar_one() == json.dumps(settings, sort_keys=True, separators=(",", ":"))
                assert db.exec_driver_sql("SELECT count(*) FROM context_plans WHERE attempt_id=?", (attempt.id,)).scalar_one() == 1
                assert db.exec_driver_sql(
                    "SELECT object_kind,object_id,predecessor_message_id FROM archive_object_derivations "
                    "WHERE chat_id=? AND object_id IN (?,?) ORDER BY object_kind",
                    (chat_id, attempt.id, attempt.assistant_message_id),
                ).all() == [
                    ("attempt", attempt.id, imported_head),
                    ("message", attempt.assistant_message_id, imported_head),
                ]
        store.close(); authority.close()
        authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
        assert store.recover_archive_imports(now=now) == (("public-regen-op", "COMMITTED"),)
        restored = store.get_generation_attempt(attempt.id)
        assert restored is not None and json.loads(restored.request_snapshot) == snapshot
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql(
                    "SELECT id,source_attempt,source_evidence_binding FROM archive_imported_attempts WHERE chat_id=?",
                    (chat_id,),
                ).one() == source_before

        # Archive v2 retains this receiving-installation continuation as native
        # source plus a local derivation; it must not recast its branch setting
        # origin into any v1 category.
        export_source = store.read_chat_export_source(
            chat_id, attachment_policy=AttachmentPolicy.EMBEDDED,
        )
        assert export_source.requires_v2 is True
        exported = archive_bytes(build_archive_v2_projection(
            archive_id="public-regen-branch-roundtrip", created_at=now,
            chat=export_source.chat, messages=export_source.messages,
            attempts=export_source.attempts,
            message_attachments=export_source.message_attachments,
            attempt_attachments=export_source.attempt_attachments, payloads={},
            attachment_policy=AttachmentPolicy.EMBEDDED,
            chat_configuration=export_source.chat_configuration,
            application_version="0.1", migration_revision="0012_phase9_archive_import",
            context_plans=export_source.context_plans,
            object_provenance=export_source.object_provenance,
            continuation_history=export_source.continuation_history,
            history_bindings=export_source.history_bindings,
        ))
        with zipfile.ZipFile(io.BytesIO(exported)) as package:
            exported_attempt = next(
                row for row in parse_jsonl(package.read("domain/attempts.jsonl"))
                if row["source_id"] == attempt.id
            )
            assert exported_attempt["request_time_provenance"]["settings_provenance"]["temperature"] == "branch"
            exported_provenance = {
                (row["object_kind"], row["object_id"]): row
                for row in parse_jsonl(package.read("domain/object-provenance.jsonl"))
            }
            assert exported_provenance[("attempt", attempt.id)]["source"]["kind"] == "native"
            assert exported_provenance[("attempt", attempt.id)]["derivation"] == {
                "kind": "local-continuation",
                "predecessor": {"object_kind": "message", "object_id": imported_head},
            }

        # The same truthful row cannot enter frozen v1.  V2 has the only
        # closed extension; no branch->v1 provenance translation is allowed.
        with pytest.raises(ArchivePackageError, match="settings provenance"):
            archive_bytes(build_archive_projection(
                archive_id="public-regen-branch-v1", created_at=now,
                chat=export_source.chat, messages=export_source.messages,
                attempts=export_source.attempts,
                message_attachments=export_source.message_attachments,
                attempt_attachments=export_source.attempt_attachments, payloads={},
                attachment_policy=AttachmentPolicy.EMBEDDED,
                chat_configuration=export_source.chat_configuration,
                application_version="0.1", migration_revision="0012_phase9_archive_import",
                context_plans=export_source.context_plans,
            ))

        def replace_branch_with_unknown(entries, manifest):
            rows = list(parse_jsonl(entries["domain/attempts.jsonl"]))
            for row in rows:
                if row["source_id"] == attempt.id:
                    provenance = dict(row["request_time_provenance"])
                    settings_provenance = dict(provenance["settings_provenance"])
                    settings_provenance["temperature"] = "other"
                    provenance["settings_provenance"] = settings_provenance
                    row["request_time_provenance"] = provenance
            entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

        with pytest.raises(ArchivePackageError, match="settings provenance"):
            validate_archive(io.BytesIO(_repack_canonical_archive(
                exported, replace_branch_with_unknown,
            )))
        roundtrip = intake / "public-regen-branch-roundtrip.botsarchive"
        roundtrip.write_bytes(exported)
        second_authority = DataRootAuthority(tmp_path / "roundtrip-root").acquire()
        second_store = second_authority.open_store()
        try:
            second_queued = second_store.enqueue_archive_import(
                roundtrip, resolver_roots=(intake,), now=now, queue_id="public-regen-roundtrip",
            )
            second_chat_id = second_store.execute_archive_import(
                second_queued.id, now=now, operation_id="public-regen-roundtrip-op",
            )
            with second_store.command_admission():
                with second_store._engine.connect() as db:
                    imported = [
                        json.loads(str(row[0]))
                        for row in db.exec_driver_sql(
                            "SELECT source_attempt FROM archive_imported_attempts WHERE chat_id=?",
                            (second_chat_id,),
                        ).all()
                    ]
                    assert [
                        item["request_time_provenance"]["settings_provenance"]["temperature"]
                        for item in imported
                        if item["request_time_provenance"].get("status") == "available"
                    ] == ["branch"]
                    assert db.exec_driver_sql(
                        "SELECT count(*) FROM archive_object_derivations d "
                        "JOIN archive_imported_attempts a ON a.id=d.object_id "
                        "WHERE d.chat_id=? AND d.object_kind='attempt'",
                        (second_chat_id,),
                    ).scalar_one() == 1
        finally:
            second_store.close()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_edit_imported_user_creates_and_round_trips_local_derived_branch(
    tmp_path, monkeypatch,
):
    """An imported-user edit is itself the explicit local derivation choice."""
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "first-root")
    second_authority = None
    second_store = None
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="edit-import")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="edit-import-operation")
        assistant_id = store.get_chat(chat_id).head_message_id
        imported_user_id = store.get_message(assistant_id).parent_id
        assert imported_user_id is not None
        with store.command_admission():
            with store._engine.connect() as db:
                original_user = db.exec_driver_sql(
                    "SELECT id,chat_id,parent_id,sequence,role,state,content,created_at,lineage_id,revision,supersedes_id "
                    "FROM messages WHERE id=?", (imported_user_id,)
                ).mappings().one()
                original_mapping = db.exec_driver_sql(
                    "SELECT chat_id,source_message_id,source_lineage_id,source_node_id "
                    "FROM archive_import_messages WHERE message_id=?", (imported_user_id,)
                ).mappings().one()

        async def forbidden_choice(*_args, **_kwargs):
            raise AssertionError("imported-user edit required a preliminary choice")

        monkeypatch.setattr(app, "choose_import_continuation", forbidden_choice)
        attempt = await app.edit_message(chat_id, imported_user_id, "edited imported user")
        await _finish(app, attempt.id)
        edited_user_id = attempt.user_message_id
        edited_user = store.get_message(edited_user_id)
        assert edited_user.supersedes_id == imported_user_id
        assert edited_user.lineage_id == original_user["lineage_id"]

        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql(
                    "SELECT id,chat_id,parent_id,sequence,role,state,content,created_at,lineage_id,revision,supersedes_id "
                    "FROM messages WHERE id=?", (imported_user_id,)
                ).mappings().one() == original_user
                assert db.exec_driver_sql(
                    "SELECT chat_id,source_message_id,source_lineage_id,source_node_id "
                    "FROM archive_import_messages WHERE message_id=?", (imported_user_id,)
                ).mappings().one() == original_mapping

                user_anchor = db.exec_driver_sql(
                    "SELECT base_key,base_message_id,revision,source_configuration,resolution "
                    "FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?",
                    (chat_id, imported_user_id),
                ).mappings().one()
                assert user_anchor["base_message_id"] == imported_user_id
                choice = db.exec_driver_sql(
                    "SELECT local_connection_id,local_model_entry_id,explicit_settings,decision_kind "
                    "FROM archive_continuation_choices WHERE chat_id=? AND base_key=? "
                    "ORDER BY choice_revision DESC LIMIT 1",
                    (chat_id, imported_user_id),
                ).mappings().one()
                assert choice["local_connection_id"] is not None
                assert choice["local_model_entry_id"] is not None
                assert choice["decision_kind"] == "operator_resolution"

                assistant_requirements = db.exec_driver_sql(
                    "SELECT ordinal,source_imported_attempt_id,source_native_attempt_id,expected_digest,expected_size,"
                    "representation_digest,binding_kind,bound_native_attachment_id,bound_imported_ref_id "
                    "FROM archive_continuation_requirements WHERE chat_id=? AND base_key=? ORDER BY ordinal",
                    (chat_id, assistant_id),
                ).fetchall()
                user_requirements = db.exec_driver_sql(
                    "SELECT ordinal,source_imported_attempt_id,source_native_attempt_id,expected_digest,expected_size,"
                    "representation_digest,binding_kind,bound_native_attachment_id,bound_imported_ref_id "
                    "FROM archive_continuation_requirements WHERE chat_id=? AND base_key=? ORDER BY ordinal",
                    (chat_id, imported_user_id),
                ).fetchall()
                assert user_requirements == assistant_requirements
                assistant_candidates = db.exec_driver_sql(
                    "SELECT ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id "
                    "FROM archive_continuation_requirement_candidates WHERE chat_id=? AND base_key=? "
                    "ORDER BY ordinal,candidate_attachment_id",
                    (chat_id, assistant_id),
                ).fetchall()
                user_candidates = db.exec_driver_sql(
                    "SELECT ordinal,candidate_attachment_id,native_attachment_id,imported_ref_id "
                    "FROM archive_continuation_requirement_candidates WHERE chat_id=? AND base_key=? "
                    "ORDER BY ordinal,candidate_attachment_id",
                    (chat_id, imported_user_id),
                ).fetchall()
                assert user_candidates == assistant_candidates

                branch = db.exec_driver_sql(
                    "SELECT base_key,choice_revision,first_message_id,attempt_id "
                    "FROM archive_continuation_branches WHERE attempt_id=?", (attempt.id,)
                ).mappings().one()
                assert branch["base_key"] == imported_user_id
                assert branch["first_message_id"] == edited_user_id
                derivations = {
                    (str(kind), str(object_id), str(predecessor))
                    for kind, object_id, predecessor in db.exec_driver_sql(
                        "SELECT object_kind,object_id,predecessor_message_id FROM archive_object_derivations "
                        "WHERE chat_id=? AND object_id IN (?,?)",
                        (chat_id, edited_user_id, attempt.id),
                    ).fetchall()
                }
                assert derivations == {
                    ("message", edited_user_id, imported_user_id),
                    ("attempt", attempt.id, imported_user_id),
                }

        snapshot = json.loads(store.get_generation_attempt(attempt.id).request_snapshot)
        assert snapshot["snapshot_version"] == 3

        projection = await app.prepare_archive_export(
            chat_id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        exported = archive_bytes(projection)
        assert validate_archive(io.BytesIO(exported)).archive_id == projection.archive_id
        roundtrip = intake / "edit-roundtrip.botsarchive"; roundtrip.write_bytes(exported)
        second_authority = DataRootAuthority(tmp_path / "second-root").acquire()
        second_store = second_authority.open_store()
        second_queued = second_store.enqueue_archive_import(
            roundtrip, resolver_roots=(intake,), now=now, queue_id="edit-roundtrip",
        )
        second_chat_id = second_store.execute_archive_import(
            second_queued.id, now=now, operation_id="edit-roundtrip-operation",
        )
        with second_store.command_admission():
            with second_store._engine.connect() as db:
                new_user_id = db.exec_driver_sql(
                    "SELECT message_id FROM archive_import_messages WHERE chat_id=? AND source_message_id=?",
                    (second_chat_id, imported_user_id),
                ).scalar_one()
                new_edited_user_id = db.exec_driver_sql(
                    "SELECT message_id FROM archive_import_messages WHERE chat_id=? AND source_message_id=?",
                    (second_chat_id, edited_user_id),
                ).scalar_one()
                new_attempt_id = db.exec_driver_sql(
                    "SELECT id FROM archive_imported_attempts WHERE chat_id=? AND source_attempt_id=?",
                    (second_chat_id, attempt.id),
                ).scalar_one()
                assert db.exec_driver_sql(
                    "SELECT base_message_id FROM archive_continuation_anchors "
                    "WHERE chat_id=? AND base_key=?", (second_chat_id, new_user_id),
                ).scalar_one() == new_user_id
                imported_branch = db.exec_driver_sql(
                    "SELECT base_key,first_message_id,attempt_id "
                    "FROM archive_imported_branch_choices WHERE chat_id=? AND attempt_id=?",
                    (second_chat_id, new_attempt_id),
                ).mappings().one()
                assert imported_branch["base_key"] == new_user_id
                assert imported_branch["first_message_id"] == new_edited_user_id
                second_derivations = {
                    (str(kind), str(object_id), str(predecessor))
                    for kind, object_id, predecessor in db.exec_driver_sql(
                        "SELECT object_kind,object_id,predecessor_message_id FROM archive_object_derivations "
                        "WHERE chat_id=? AND object_id IN (?,?)",
                        (second_chat_id, new_edited_user_id, new_attempt_id),
                    ).fetchall()
                }
                assert second_derivations == {
                    ("message", new_edited_user_id, new_user_id),
                    ("attempt", new_attempt_id, new_user_id),
                }
    finally:
        if second_store is not None:
            second_store.close()
        elif second_authority is not None:
            second_authority.close()
        store.close()


@pytest.mark.asyncio
async def test_public_imported_regeneration_without_an_admitted_anchor_choice_fails_before_dispatch(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 19, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="missing-regen-choice")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="missing-regen-choice-op")
        target_id = store.get_chat(chat_id).head_message_id
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        assert await app.choose_import_continuation(
            chat_id, "empty", expected_choice_revision=0,
            connection_id=connection.id, model_entry_id=model.id,
            explicit_settings={"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None},
            excluded_refs=(),
        ) == 1
        attempts_before = tuple(store.list_generation_attempts(chat_id))
        with pytest.raises(StateError, match="ready explicit choice"):
            await app.regenerate_message(chat_id, target_id)
        assert tuple(store.list_generation_attempts(chat_id)) == attempts_before
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql("SELECT count(*) FROM archive_continuation_branches WHERE chat_id=?", (chat_id,)).scalar_one() == 0
                assert db.exec_driver_sql("SELECT count(*) FROM archive_continuation_choices WHERE chat_id=? AND base_key='empty'", (chat_id,)).scalar_one() == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_public_imported_regeneration_materializes_ready_source_attachment_without_user_link(tmp_path):
    resolver = tmp_path / "resolver"; resolver.mkdir()
    payload = b"public imported regeneration attachment"
    digest = hashlib.sha256(payload).hexdigest()
    source = resolver / "ready-source.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=len(payload), selected_slots=2))
    (resolver / "payload.bin").write_bytes(payload)
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 19, tzinfo=UTC)
    settings = {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(resolver,), now=now, queue_id="ready-public-regen")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="ready-public-regen-op")
        target_id = store.get_chat(chat_id).head_message_id
        target = store.get_message(target_id)
        assert target is not None and target.parent_id is not None
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        assert await app.choose_import_continuation(
            chat_id, target_id, expected_choice_revision=0,
            connection_id=connection.id, model_entry_id=model.id,
            explicit_settings=settings, excluded_refs=(),
        ) == 1
        attempt = await app.regenerate_message(chat_id, target_id)
        await _finish(app, attempt.id)
        native_child = store.get_chat(chat_id).head_message_id
        child_readiness = await app.import_continuation_readiness(chat_id, native_child)
        assert child_readiness is not None
        assert [item.source_native_attempt_id for item in child_readiness.requirements] == [attempt.id, attempt.id]
        assert all(item.native_attachment_id is not None and item.blocked_reason is None for item in child_readiness.requirements)
        child_send = await app.send_message(chat_id, "native source requirement reuse")
        await _finish(app, child_send.id)
        with store.command_admission():
            with store._engine.connect() as db:
                imported_refs = db.exec_driver_sql(
                    "SELECT ref.attachment_id FROM archive_continuation_requirements requirement "
                    "JOIN archive_continuation_requirement_candidates candidate "
                    "ON candidate.chat_id=requirement.chat_id AND candidate.base_key=requirement.base_key AND candidate.ordinal=requirement.ordinal "
                    "JOIN archive_import_attachment_refs ref ON ref.id=candidate.imported_ref_id "
                    "WHERE requirement.chat_id=? AND requirement.base_key=? AND ref.availability='READY' "
                    "ORDER BY requirement.ordinal,candidate.imported_ref_id",
                    (chat_id, target_id),
                ).all()
                assert len(imported_refs) == 2 and len({row[0] for row in imported_refs}) == 2
                assert db.exec_driver_sql(
                    "SELECT attachment_id,ordinal FROM attempt_attachments WHERE attempt_id=?",
                    (attempt.id,),
                ).all() == [(row[0], ordinal) for ordinal, row in enumerate(imported_refs)]
                assert db.exec_driver_sql(
                    "SELECT attachment_id,ordinal FROM attempt_attachments WHERE attempt_id=?",
                    (child_send.id,),
                ).all() == [(row[0], ordinal) for ordinal, row in enumerate(imported_refs)]
                assert db.exec_driver_sql(
                    "SELECT count(*) FROM message_attachments WHERE message_id=?", (target.parent_id,)
                ).scalar_one() == 0
                assert db.exec_driver_sql(
                    "SELECT count(*) FROM archive_continuation_branches WHERE attempt_id=?", (attempt.id,)
                ).scalar_one() == 1
                assert db.exec_driver_sql(
                    "SELECT count(*) FROM archive_imported_attempts WHERE assistant_message_id=?", (target_id,)
                ).scalar_one() == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_public_send_inherits_branch_choice_and_revisits_immutable_imported_base(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 19, tzinfo=UTC)
    first_settings = {"temperature": 0.25, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    second_settings = {"temperature": 0.75, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="branch-send")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="branch-send-op")
        imported_base = store.get_chat(chat_id).head_message_id
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        assert await app.choose_import_continuation(
            chat_id, imported_base, expected_choice_revision=0, connection_id=connection.id,
            model_entry_id=model.id, explicit_settings=first_settings, excluded_refs=(),
        ) == 1
        first = await app.regenerate_message(chat_id, imported_base)
        await _finish(app, first.id)
        first_child = store.get_chat(chat_id).head_message_id
        child_readiness = await app.import_continuation_readiness(chat_id, first_child)
        assert child_readiness is not None and child_readiness.explicit_settings == first_settings
        sent = await app.send_message(chat_id, "native child turn")
        await _finish(app, sent.id)
        sent_snapshot = json.loads(store.get_generation_attempt(sent.id).request_snapshot)
        assert sent_snapshot["effective_settings"]["temperature"] == 0.25
        assert sent_snapshot["settings_provenance"]["temperature"] == "branch"
        assert store.get_chat_model_selection(chat_id) is None
        # The old imported assistant remains on the active ancestry and gains
        # a distinct second choice; its original choice is not rewritten.
        assert await app.choose_import_continuation(
            chat_id, imported_base, expected_choice_revision=1, connection_id=connection.id,
            model_entry_id=model.id, explicit_settings=second_settings, excluded_refs=(),
        ) == 2
        second = await app.branch_from_message(
            chat_id, imported_base, "revisit imported base",
            expected_chat_revision=store.get_chat(chat_id).revision,
        )
        await _finish(app, second.id)
        second_snapshot = json.loads(store.get_generation_attempt(second.id).request_snapshot)
        assert second_snapshot["effective_settings"]["temperature"] == 0.75
        original = await app.import_continuation_readiness(chat_id, imported_base)
        assert original is not None and original.choice_revision == 2 and original.explicit_settings == second_settings
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql(
                    "SELECT explicit_settings FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=1",
                    (chat_id, imported_base),
                ).scalar_one() == json.dumps(first_settings, sort_keys=True, separators=(",", ":"))
                assert db.exec_driver_sql(
                    "SELECT first_message_id FROM archive_continuation_branches WHERE attempt_id=?", (second.id,)
                ).scalar_one() == store.get_generation_attempt(second.id).user_message_id
                assert db.exec_driver_sql("SELECT count(*) FROM archive_continuation_branches WHERE chat_id=?", (chat_id,)).scalar_one() == 3
    finally:
        store.close()


def test_v1_import_then_v2_export_and_v2_import_preserves_source_hops(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    now = datetime(2026, 9, 19, tzinfo=UTC)
    first_root = tmp_path / "first-root"
    first_authority = DataRootAuthority(first_root).acquire()
    first_store = None
    second_authority = None
    second_store = None
    try:
        first_store = first_authority.open_store()
        queued = first_store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="first")
        imported_chat_id = first_store.execute_archive_import(queued.id, now=now, operation_id="first-operation")
        export_source = first_store.read_chat_export_source(imported_chat_id, attachment_policy=AttachmentPolicy.EMBEDDED)
        assert export_source.requires_v2 is True
        exported = archive_bytes(build_archive_v2_projection(
            archive_id="round-trip-v2", created_at=now, chat=export_source.chat,
            messages=export_source.messages, attempts=export_source.attempts,
            message_attachments=export_source.message_attachments,
            attempt_attachments=export_source.attempt_attachments, payloads={},
            attachment_policy=AttachmentPolicy.EMBEDDED,
            chat_configuration=export_source.chat_configuration,
            application_version="0.1", migration_revision="0012_phase9_archive_import",
            context_plans=export_source.context_plans,
            object_provenance=export_source.object_provenance,
        ))
        v2_source = intake_root / "round-trip-v2.botsarchive"
        v2_source.write_bytes(exported)
        second_authority = DataRootAuthority(tmp_path / "second-root").acquire()
        second_store = second_authority.open_store()
        second_queued = second_store.enqueue_archive_import(v2_source, resolver_roots=(intake_root,), now=now, queue_id="second")
        second_chat_id = second_store.execute_archive_import(second_queued.id, now=now, operation_id="second-operation")
        with second_store.command_admission():
            with second_store._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT count(*) FROM archive_imported_attempts WHERE chat_id=?", (second_chat_id,)).scalar_one() == 1
                assert connection.exec_driver_sql("SELECT count(*) FROM archive_lineage_nodes WHERE source_format=1").scalar_one() >= 1
                assert connection.exec_driver_sql("SELECT count(*) FROM archive_lineage_nodes WHERE source_format=2").scalar_one() >= 1
    finally:
        if second_store is not None:
            second_store.close()
        elif second_authority is not None:
            second_authority.close()
        if first_store is not None:
            first_store.close()
        else:
            first_authority.close()


def test_v1_safe_context_attachment_bindings_survive_v2_second_hop(tmp_path):
    """D317 retains source evidence while remapping only current graph arms."""
    async def scenario():
        now = datetime(2026, 9, 20, tzinfo=UTC)
        intake = tmp_path / "intake"; intake.mkdir()
        source_app, source_store, source_authority = _configured_application(tmp_path / "source-root")
        first_authority = second_authority = None
        first_store = second_store = None
        try:
            payload_path = intake / "context.txt"; payload_path.write_text("safe context attachment", encoding="utf-8")
            attachment = source_store.ingest_attachment(payload_path)
            chat = await source_app.create_chat("v1 safe context")
            await source_app.stage_attachment(chat.id, attachment.id)
            attempt = await source_app.send_message(chat.id, "source request")
            await _finish(source_app, attempt.id)
            raw_v1 = archive_bytes(await source_app.prepare_archive_export(chat.id))
            with zipfile.ZipFile(io.BytesIO(raw_v1)) as package:
                v1_attempt = json.loads(package.read("domain/attempts.jsonl"))
                v1_sources = v1_attempt["request_time_provenance"]["context"]["sources"]
                selected_attachment = next(item for item in v1_sources if item["kind"] == "attachment" and item["selected"])
            v1_path = intake / "v1-safe-context.botsarchive"; v1_path.write_bytes(raw_v1)
            await source_app.close()

            first_authority = DataRootAuthority(tmp_path / "first-root").acquire(); first_store = first_authority.open_store()
            queued = first_store.enqueue_archive_import(v1_path, resolver_roots=(intake,), now=now, queue_id="v1-safe-context")
            first_chat = first_store.execute_archive_import(queued.id, now=now, operation_id="v1-safe-context-op")
            with first_store.command_admission():
                with first_store._engine.connect() as connection:
                    imported_attempt, raw_bindings = connection.exec_driver_sql(
                        "SELECT id,source_evidence_binding FROM archive_imported_attempts WHERE chat_id=?", (first_chat,)
                    ).one()
                    bindings = json.loads(str(raw_bindings))
                    attachment_binding = next(
                        row for row in bindings if row["snapshot_source"]["kind"] == "attachment"
                    )
                    assert attachment_binding["snapshot_source"]["source_id"] == selected_attachment["source_id"]
                    assert attachment_binding["current_binding"] == {
                        "object_kind": "attachment", "object_id": selected_attachment["source_id"],
                    }
                    assert attachment_binding["binding_state"] == "bound"
                    assert imported_attempt

            export_source = first_store.read_chat_export_source(first_chat, attachment_policy=AttachmentPolicy.EMBEDDED)
            exported_binding = next(
                row for row in export_source.history_bindings if row["snapshot_source"]["kind"] == "attachment"
            )
            assert exported_binding["snapshot_source"]["source_id"] == selected_attachment["source_id"]
            assert exported_binding["current_binding"]["object_id"] != selected_attachment["source_id"]
            payloads = {
                item.attachment.blob_digest: item.payload
                for values in (*export_source.message_attachments.values(), *export_source.attempt_attachments.values())
                for item in values if item.payload is not None
            }
            v2_path = intake / "v1-second-hop.botsarchive"
            v2_path.write_bytes(archive_bytes(build_archive_v2_projection(
                archive_id="v1-safe-context-second-hop", created_at=now,
                chat=export_source.chat, messages=export_source.messages, attempts=export_source.attempts,
                message_attachments=export_source.message_attachments,
                attempt_attachments=export_source.attempt_attachments, payloads=payloads,
                attachment_policy=AttachmentPolicy.EMBEDDED,
                chat_configuration=export_source.chat_configuration,
                application_version="0.1", migration_revision="0012_phase9_archive_import",
                context_plans=export_source.context_plans,
                object_provenance=export_source.object_provenance,
                continuation_history=export_source.continuation_history,
                history_bindings=export_source.history_bindings,
                archived_attempt_provenance=export_source.archived_attempt_provenance,
            )))
            assert validate_archive(io.BytesIO(v2_path.read_bytes())).archive_id == "v1-safe-context-second-hop"
            second_authority = DataRootAuthority(tmp_path / "second-root").acquire(); second_store = second_authority.open_store()
            second_queued = second_store.enqueue_archive_import(v2_path, resolver_roots=(intake,), now=now, queue_id="v1-second-hop")
            second_chat = second_store.execute_archive_import(second_queued.id, now=now, operation_id="v1-second-hop-op")
            readiness = second_store.import_continuation_readiness(
                second_chat, second_store.get_chat(second_chat).head_message_id,
            )
            assert readiness is not None and all(item.blocked_reason is None for item in readiness.requirements)
        finally:
            if second_store is not None:
                second_store.close()
            elif second_authority is not None:
                second_authority.close()
            if first_store is not None:
                first_store.close()
            elif first_authority is not None:
                first_authority.close()
            if not source_app._closed:
                await source_app.close()
            elif not source_store.closed:
                source_authority.close()
    asyncio.run(scenario())


def test_native_v3_safe_context_history_bindings_are_complete_and_typed(tmp_path):
    """D315 emits one sealed binding for every native v3 safe-context slot.

    Equal representation digests are deliberately insufficient to identify an
    available attachment.  The typed current arm must retain the exact frozen
    source attachment identity, even where two selected source slots have the
    same bytes.
    """
    async def scenario():
        intake = tmp_path / "intake"; intake.mkdir()
        app, store, authority = _configured_application(tmp_path / "native-root")
        try:
            first = intake / "first.txt"; first.write_text("same safe bytes", encoding="utf-8")
            second = intake / "second.txt"; second.write_text("same safe bytes", encoding="utf-8")
            first_attachment = store.ingest_attachment(first)
            second_attachment = store.ingest_attachment(second)
            chat = await app.create_chat("native safe source bindings")
            initial = await app.send_message(chat.id, "first native turn supplies safe history")
            await _finish(app, initial.id)
            await app.stage_attachment(chat.id, first_attachment.id)
            await app.stage_attachment(chat.id, second_attachment.id)
            attempt = await app.send_message(chat.id, "retain both selected source slots")
            await _finish(app, attempt.id)

            raw = archive_bytes(await app.prepare_archive_export(chat.id, archive_version=2))
            assert validate_archive(io.BytesIO(raw)).archive_id
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                attempts = list(parse_jsonl(package.read("domain/attempts.jsonl")))
                archived_attempt = next(row for row in attempts if row["source_id"] == attempt.id)
                sources = archived_attempt["request_time_provenance"]["context"]["sources"]
                bindings = [
                    row for row in parse_jsonl(package.read("domain/history-bindings.jsonl"))
                    if row["attempt_id"] == attempt.id
                ]
            assert [row["ordinal"] for row in bindings] == list(range(len(sources)))
            assert [row["snapshot_source"]["kind"] for row in bindings] == [row["kind"] for row in sources]
            assert [row["snapshot_source"]["source_id"] for row in bindings] == [row["source_id"] for row in sources]
            attachment_bindings = [
                row for row in bindings if row["snapshot_source"]["kind"] == "attachment"
            ]
            assert {row["current_binding"]["object_id"] for row in attachment_bindings} == {
                first_attachment.id, second_attachment.id,
            }
            assert all(row["binding_state"] == "bound" for row in attachment_bindings)
            history_bindings = [
                row for row in bindings if row["snapshot_source"]["kind"] == "history"
            ]
            assert len(history_bindings) >= 2
            assert all(
                row["current_binding"]["object_kind"] == "message"
                and row["current_binding"]["object_id"] == row["snapshot_source"]["source_id"]
                and row["binding_state"] == "bound"
                for row in history_bindings
            )
            assert any(
                row["snapshot_source"]["kind"] == "bots_instruction"
                and row["current_binding"] is None and row["binding_state"] == "historical-only"
                for row in bindings
            )

            def bindings_from(entries):
                return list(parse_jsonl(entries["domain/history-bindings.jsonl"]))

            def write_bindings(entries, rows):
                entries["domain/history-bindings.jsonl"] = canonical_jsonl_bytes(rows)

            def missing_ordinal(entries, manifest):
                rows = bindings_from(entries)
                write_bindings(entries, [row for row in rows if not (
                    row["attempt_id"] == attempt.id and row["ordinal"] == 0
                )])

            def extra_ordinal(entries, manifest):
                rows = bindings_from(entries)
                rows.append(json.loads(json.dumps(next(
                    row for row in rows if row["attempt_id"] == attempt.id and row["ordinal"] == 0
                ))))
                write_bindings(entries, rows)

            def changed_snapshot_digest(entries, manifest):
                rows = bindings_from(entries)
                attachment = next(
                    row for row in rows
                    if row["attempt_id"] == attempt.id and row["snapshot_source"]["kind"] == "attachment"
                )
                attachment["snapshot_source"]["evidence_digest"] = "0" * 64
                write_bindings(entries, rows)

            def wrong_typed_target(entries, manifest):
                rows = bindings_from(entries)
                attachment = next(
                    row for row in rows
                    if row["attempt_id"] == attempt.id and row["snapshot_source"]["kind"] == "attachment"
                )
                attachment["current_binding"] = {
                    "object_kind": "message", "object_id": attempt.user_message_id,
                }
                write_bindings(entries, rows)

            def swap_same_digest_available_attachments(entries, manifest):
                rows = bindings_from(entries)
                selected = [
                    row for row in rows
                    if row["attempt_id"] == attempt.id and row["snapshot_source"]["kind"] == "attachment"
                ]
                assert len(selected) == 2
                first_id = selected[0]["current_binding"]["object_id"]
                selected[0]["current_binding"]["object_id"] = selected[1]["current_binding"]["object_id"]
                selected[1]["current_binding"]["object_id"] = first_id
                write_bindings(entries, rows)

            for mutation in (
                missing_ordinal, extra_ordinal, changed_snapshot_digest,
                wrong_typed_target, swap_same_digest_available_attachments,
            ):
                with pytest.raises(ArchivePackageError):
                    validate_archive(io.BytesIO(_repack_canonical_archive(raw, mutation)))
        finally:
            if not app._closed:
                await app.close()
            elif not store.closed:
                authority.close()
    asyncio.run(scenario())


def test_imported_branch_history_is_exact_immutable_reopens_and_round_trips(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "branch-history.botsarchive"
    source.write_bytes(archive_with_continuation_branch())
    now = datetime(2026, 9, 19, tzinfo=UTC)
    first_root = tmp_path / "first-root"
    first_authority = DataRootAuthority(first_root).acquire()
    store = None
    second_authority = None
    second_store = None
    try:
        store = first_authority.open_store()
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="branch-first")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="branch-first-operation")
        with store.command_admission():
            with store._engine.connect() as connection:
                branch = connection.exec_driver_sql(
                    "SELECT chat_id,first_message_id,attempt_id,base_key,choice_snapshot "
                    "FROM archive_imported_branch_choices WHERE chat_id=?", (chat_id,)
                ).mappings().one()
                assert json.loads(str(branch["choice_snapshot"])) == {
                    "anchor_key": "assistant", "choice_revision": 1,
                    "first_message_id": "user", "attempt_id": "attempt",
                    "created_at": "2026-09-19T00:00:00.000000Z",
                }
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_continuation_branches WHERE chat_id=?", (chat_id,)
                ).scalar_one() == 0

        # Startup must independently replay the current-schema branch checks.
        store.close()
        first_authority.close()
        first_authority = DataRootAuthority(first_root).acquire()
        store = first_authority.open_store()
        with store.command_admission():
            with store._engine.begin() as connection:
                branch = connection.exec_driver_sql(
                    "SELECT chat_id,first_message_id,attempt_id,base_key,choice_snapshot "
                    "FROM archive_imported_branch_choices WHERE chat_id=?", (chat_id,)
                ).mappings().one()
                values = tuple(branch[key] for key in ("first_message_id", "chat_id", "attempt_id", "base_key", "choice_snapshot"))
                connection.exec_driver_sql(
                    "DELETE FROM archive_imported_branch_choices WHERE first_message_id=?", (branch["first_message_id"],)
                )
                with pytest.raises(Exception, match="imported branch choice"):
                    connection.exec_driver_sql(
                        "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                        (branch["chat_id"], branch["first_message_id"], branch["attempt_id"], branch["base_key"], branch["choice_snapshot"]),
                    )
                malformed = json.loads(str(branch["choice_snapshot"]))
                del malformed["created_at"]
                malformed_snapshot = json.dumps(malformed, sort_keys=True, separators=(",", ":"))
                arm_phase9_import_graph(
                    connection, "branch-malformed-grant", (),
                    imported_branches=((
                        branch["first_message_id"], branch["chat_id"], branch["attempt_id"],
                        branch["base_key"], malformed_snapshot,
                    ),),
                )
                try:
                    with pytest.raises(Exception, match="imported branch choice"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                            (branch["chat_id"], branch["first_message_id"], branch["attempt_id"], branch["base_key"], malformed_snapshot),
                        )
                finally:
                    clear_phase9_import_graph(connection)
                arm_phase9_import_graph(connection, "branch-grant", (), imported_branches=(values,))
                try:
                    snapshot = json.loads(str(branch["choice_snapshot"]))
                    for changed in (
                        {**snapshot, "first_message_id": "assistant"},
                        {**snapshot, "choice_revision": 2},
                    ):
                        with pytest.raises(Exception, match="imported branch choice"):
                            connection.exec_driver_sql(
                                "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                                (branch["chat_id"], branch["first_message_id"], branch["attempt_id"], branch["base_key"], json.dumps(changed, sort_keys=True, separators=(",", ":"))),
                            )
                    with pytest.raises(Exception, match="imported branch choice"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                            (branch["chat_id"], branch["first_message_id"], branch["attempt_id"], "empty", branch["choice_snapshot"]),
                        )
                    with pytest.raises(Exception, match="imported branch choice"):
                        connection.exec_driver_sql(
                            "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                            ("wrong-chat", branch["first_message_id"], branch["attempt_id"], branch["base_key"], branch["choice_snapshot"]),
                        )
                    connection.exec_driver_sql(
                        "INSERT INTO archive_imported_branch_choices(chat_id,first_message_id,attempt_id,base_key,choice_snapshot) VALUES (?,?,?,?,?)",
                        (branch["chat_id"], branch["first_message_id"], branch["attempt_id"], branch["base_key"], branch["choice_snapshot"]),
                    )
                    with pytest.raises(Exception, match="immutable"):
                        connection.exec_driver_sql(
                            "UPDATE archive_imported_branch_choices SET base_key='empty' WHERE first_message_id=?",
                            (branch["first_message_id"],),
                        )
                finally:
                    clear_phase9_import_graph(connection)

        export_source = store.read_chat_export_source(chat_id, attachment_policy=AttachmentPolicy.EMBEDDED)
        assert export_source.continuation_history is not None
        exported_branch = export_source.continuation_history["branches"]
        assert len(exported_branch) == 1
        assert exported_branch[0]["anchor_key"] != "assistant"
        exported = archive_bytes(build_archive_v2_projection(
            archive_id="branch-round-trip", created_at=now, chat=export_source.chat,
            messages=export_source.messages, attempts=export_source.attempts,
            message_attachments=export_source.message_attachments,
            attempt_attachments=export_source.attempt_attachments, payloads={},
            attachment_policy=AttachmentPolicy.EMBEDDED,
            chat_configuration=export_source.chat_configuration,
            application_version="0.1", migration_revision="0012_phase9_archive_import",
            context_plans=export_source.context_plans,
            object_provenance=export_source.object_provenance,
            continuation_history=export_source.continuation_history,
            history_bindings=export_source.history_bindings,
        ))
        roundtrip = intake_root / "branch-round-trip.botsarchive"
        roundtrip.write_bytes(exported)
        second_authority = DataRootAuthority(tmp_path / "second-root").acquire()
        second_store = second_authority.open_store()
        queued = second_store.enqueue_archive_import(roundtrip, resolver_roots=(intake_root,), now=now, queue_id="branch-second")
        second_chat_id = second_store.execute_archive_import(queued.id, now=now, operation_id="branch-second-operation")
        with second_store.command_admission():
            with second_store._engine.connect() as connection:
                snapshot = connection.exec_driver_sql(
                    "SELECT choice_snapshot FROM archive_imported_branch_choices WHERE chat_id=?", (second_chat_id,)
                ).scalar_one()
                assert json.loads(str(snapshot)) == exported_branch[0]
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_continuation_branches WHERE chat_id=?", (second_chat_id,)
                ).scalar_one() == 0

        # Simulate durable out-of-process corruption while preserving the
        # exact installed guard definitions.  Startup must reject the broken
        # snapshot even though raw-DML admission cannot be used to create it.
        corrupt_root = tmp_path / "corrupt-root"
        corrupt_authority = DataRootAuthority(corrupt_root).acquire()
        corrupt_store = None
        try:
            corrupt_store = corrupt_authority.open_store()
            queued = corrupt_store.enqueue_archive_import(
                source, resolver_roots=(intake_root,), now=now, queue_id="branch-corrupt",
            )
            corrupt_store.execute_archive_import(
                queued.id, now=now, operation_id="branch-corrupt-operation",
            )
            corrupt_store.close(); corrupt_authority.close()
            database = sqlite3.connect(corrupt_root / "database" / "state.sqlite3")
            try:
                triggers = database.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='archive_imported_branch_choices' ORDER BY name"
                ).fetchall()
                assert triggers and all(definition is not None for _, definition in triggers)
                for name, _ in triggers:
                    database.execute(f'DROP TRIGGER "{name.replace('"', '""')}"')
                database.execute("UPDATE archive_imported_branch_choices SET choice_snapshot='{}'")
                for _, definition in triggers:
                    database.execute(definition)
                assert database.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='archive_imported_branch_choices' ORDER BY name"
                ).fetchall() == triggers
                database.commit()
            finally:
                database.close()
            corrupt_authority = DataRootAuthority(corrupt_root).acquire()
            with pytest.raises(RuntimeError, match="imported branch choice"):
                corrupt_authority.open_store()
        finally:
            if corrupt_store is not None:
                corrupt_store.close()
            else:
                corrupt_authority.close()
    finally:
        if second_store is not None:
            second_store.close()
        elif second_authority is not None:
            second_authority.close()
        if store is not None:
            store.close()
        else:
            first_authority.close()


@pytest.mark.asyncio
async def test_v1_fake_selection_without_override_uses_stable_local_equivalent(tmp_path):
    """A v1 archive has no branch history and absent settings inherit locally."""
    intake = tmp_path / "intake"; intake.mkdir()
    source_app, source_store, source_authority = _configured_application(tmp_path / "source-root")
    destination_app, destination_store, destination_authority = _configured_application(tmp_path / "destination-root")
    try:
        source_connection = await source_app.create_provider_connection(
            name="source fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
        )
        await source_app.refresh_models(
            source_connection.id, FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
        )
        source_model = source_store.get_model_catalogue_entry_by_provider_id(
            source_connection.id, "fake-v0.1",
        )
        assert source_model is not None
        source_chat = await source_app.create_chat("v1 fake source")
        source_store.set_chat_model_selection(source_chat.id, source_model.id)
        source_attempt = await source_app.send_message(source_chat.id, "archive this")
        await _finish(source_app, source_attempt.id)
        source = intake / "v1-fake.botsarchive"
        source.write_bytes(archive_bytes(await source_app.prepare_archive_export(source_chat.id)))

        # Earlier candidates are deliberately unfit.  The import must select
        # the first later *runnable* equivalent, not merely the first lexical
        # provider/model tuple.
        seeded = destination_store.list_provider_connections()[0]
        await destination_app.set_provider_connection_enabled(
            seeded.id, False, expected_revision=seeded.revision,
        )
        insufficient = await destination_app.create_provider_connection(
            name="insufficient fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
        )
        await destination_app.add_manual_model(
            connection_id=insufficient.id, provider_model_id="fake-v0.1", display_name="Insufficient Fake",
        )
        retired = await destination_app.create_provider_connection(
            name="retired fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
        )
        await destination_app.refresh_models(
            retired.id, FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
        )
        await destination_app.retire_provider_connection(
            retired.id, expected_revision=retired.revision,
        )
        candidates = []
        for name in ("z fake", "a fake"):
            connection = await destination_app.create_provider_connection(
                name=name, backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
            )
            await destination_app.refresh_models(
                connection.id, FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
            )
            model = destination_store.get_model_catalogue_entry_by_provider_id(connection.id, "fake-v0.1")
            assert model is not None
            candidates.append((connection.id, model.id))
        providers_before = tuple(destination_store.list_provider_connections())
        queued = destination_store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=datetime(2026, 9, 20, tzinfo=UTC), queue_id="v1-fake",
        )
        chat_id = destination_store.execute_archive_import(
            queued.id, now=datetime(2026, 9, 20, tzinfo=UTC), operation_id="v1-fake-operation",
        )
        local_head = destination_store.get_chat(chat_id).head_message_id
        assert local_head is not None
        with destination_store.command_admission():
            with destination_store._engine.connect() as connection:
                choice = connection.exec_driver_sql(
                    "SELECT local_connection_id,local_model_entry_id,explicit_settings,decision_kind "
                    "FROM archive_continuation_choices WHERE chat_id=? AND base_key=?",
                    (chat_id, local_head),
                ).one()
                available = connection.exec_driver_sql(
                    "SELECT p.id,m.id FROM model_catalogue_entries m JOIN provider_connections p ON p.id=m.connection_id "
                    "WHERE p.backend_type='fake' AND p.profile='generic' AND p.enabled=1 AND p.retired=0 "
                    "AND m.availability='available' AND m.provider_model_id='fake-v0.1' ORDER BY p.id,m.id"
                ).fetchall()
                runnable = [candidate for candidate in available if connection.exec_driver_sql(
                    "SELECT 1 FROM capability_facts WHERE model_entry_id=? "
                    "AND capability_key='generation.streaming' AND state='supported' AND value IS NULL",
                    (candidate[1],),
                ).first() is not None]
                assert runnable and available[0] != runnable[0]
                assert choice == (
                    *runnable[0],
                    '{"max_output_tokens":null,"reasoning_effort":null,"temperature":null,"timeout_seconds":null}',
                    "equivalent",
                )
                assert connection.exec_driver_sql(
                    "SELECT resolution FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?",
                    (chat_id, local_head),
                ).scalar_one() == "EQUIVALENT"
        # A subsequent local operator choice is append-only receiver state.
        # It may supersede the active branch target without rewriting the
        # sealed initial automatic-equivalence row.
        assert destination_store.admit_import_continuation_choice(
            chat_id, local_head, expected_choice_revision=1,
            connection_id=runnable[0][0], model_entry_id=runnable[0][1],
            explicit_settings={
                "temperature": None, "max_output_tokens": None,
                "reasoning_effort": None, "timeout_seconds": None,
            },
            excluded_refs=(), now=datetime(2026, 9, 20, tzinfo=UTC),
        ) == 2
        # The archive has no source override.  A real continuation must use
        # the receiver's current default, not manufacture a frozen null.
        destination_store.set_model_generation_settings(
            runnable[0][1], GenerationSettings(temperature=0.7, max_output_tokens=321),
        )
        continued = await destination_app.regenerate_message(chat_id, local_head)
        await _finish(destination_app, continued.id)
        continuation_snapshot = json.loads(destination_store.get_generation_attempt(continued.id).request_snapshot)
        assert continuation_snapshot["effective_settings"]["temperature"] == 0.7
        assert continuation_snapshot["effective_settings"]["max_output_tokens"] == 321
        assert continuation_snapshot["settings_provenance"]["temperature"] == "model"
        assert tuple(destination_store.list_provider_connections()) == providers_before
        # A native continuation is legitimate later local evolution.  Startup
        # must compare the sealed initial equivalent row, without reselecting
        # it against the now-mutated receiver defaults or rejecting the child.
        await destination_app.close()
        destination_app = None
        reopened_authority = DataRootAuthority(tmp_path / "destination-root").acquire()
        reopened_store = reopened_authority.open_store()
        try:
            assert reopened_store.recover_archive_imports(
                now=datetime(2026, 9, 20, tzinfo=UTC),
            ) == (("v1-fake-operation", "COMMITTED"),)
        finally:
            reopened_store.close(); reopened_authority.close()

        # These remain valid SQL shapes after their table guards are restored,
        # but each contradicts a distinct pre-cutoff receiver fact.
        database_path = tmp_path / "destination-root" / "database" / "state.sqlite3"
        with sqlite3.connect(database_path) as database:
            initial_choice = database.execute(
                "SELECT local_connection_id,local_model_entry_id,explicit_settings,safe_target_descriptor "
                "FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=1",
                (chat_id, local_head),
            ).fetchone()
            assert initial_choice is not None
            initial_anchor = database.execute(
                "SELECT source_configuration FROM archive_continuation_anchors "
                "WHERE chat_id=? AND base_key=?",
                (chat_id, local_head),
            ).fetchone()
            assert initial_anchor is not None

        def mutate(table, statement, parameters):
            with sqlite3.connect(database_path) as database:
                triggers = database.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name", (table,)
                ).fetchall()
                for name, _ in triggers:
                    database.execute(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
                database.execute(statement, parameters)
                for _, definition in triggers:
                    assert definition is not None
                    database.execute(definition)
                assert database.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name", (table,)
                ).fetchall() == triggers
                database.commit()

        def assert_refused(expected):
            corrupted_authority = DataRootAuthority(tmp_path / "destination-root").acquire()
            try:
                with pytest.raises(RuntimeError, match="durable Phase 6 attachment storage failed verification") as error:
                    corrupted_authority.open_store()
                pending, messages = [error.value], []
                while pending:
                    current = pending.pop()
                    if str(current) not in messages:
                        messages.append(str(current))
                        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
                assert any(expected in item for item in messages)
            finally:
                corrupted_authority.close()

        for table, statement, parameters, restore, expected in (
            ("archive_continuation_choices",
             "UPDATE archive_continuation_choices SET safe_target_descriptor=? WHERE chat_id=? AND base_key=? AND choice_revision=1",
             ('{"availability":"available","backend_type":"fake","capabilities":[],"provider_model_id":"tampered","provider_profile":"generic"}', chat_id, local_head),
             ("UPDATE archive_continuation_choices SET safe_target_descriptor=? WHERE chat_id=? AND base_key=? AND choice_revision=1", (initial_choice[3], chat_id, local_head)),
             "initial receiver continuation choices contradict sealed graph"),
            ("archive_continuation_choices",
             "UPDATE archive_continuation_choices SET explicit_settings=? WHERE chat_id=? AND base_key=? AND choice_revision=1",
             ('{"max_output_tokens":null,"reasoning_effort":null,"temperature":0.25,"timeout_seconds":null}', chat_id, local_head),
             ("UPDATE archive_continuation_choices SET explicit_settings=? WHERE chat_id=? AND base_key=? AND choice_revision=1", (initial_choice[2], chat_id, local_head)),
             "initial receiver continuation choices contradict sealed graph"),
            ("archive_continuation_choices",
             "UPDATE archive_continuation_choices SET local_model_entry_id=? WHERE chat_id=? AND base_key=? AND choice_revision=1",
             (candidates[-1][1], chat_id, local_head),
             ("UPDATE archive_continuation_choices SET local_model_entry_id=? WHERE chat_id=? AND base_key=? AND choice_revision=1", (initial_choice[1], chat_id, local_head)),
            "initial receiver continuation choices contradict sealed graph"),
            ("archive_continuation_anchors",
             "UPDATE archive_continuation_anchors SET resolution='UNRESOLVED',resolution_evidence=? WHERE chat_id=? AND base_key=?",
             ('{"kind":"tampered"}', chat_id, local_head),
             ("UPDATE archive_continuation_anchors SET resolution='EQUIVALENT',resolution_evidence=? WHERE chat_id=? AND base_key=?", ('{"kind":"deterministic-fake-equivalence"}', chat_id, local_head)),
             "initial receiver continuation anchors contradict sealed graph"),
            ("archive_continuation_anchors",
             "UPDATE archive_continuation_anchors SET source_configuration=? WHERE chat_id=? AND base_key=?",
             ('{"tampered":true}', chat_id, local_head),
             ("UPDATE archive_continuation_anchors SET source_configuration=? WHERE chat_id=? AND base_key=?", (initial_anchor[0], chat_id, local_head)),
             "committed import continuation anchors contradict sealed graph"),
        ):
            mutate(table, statement, parameters)
            assert_refused(expected)
            mutate(table, *restore)

        # Preflight seals one exact target.  A later disable must make graph
        # admission fail; the cutoff cannot silently choose another runnable
        # fake from the same receiver catalogue.
        disabled_app, disabled_store, _disabled_authority = _configured_application(tmp_path / "disabled-root")
        try:
            alternate = await disabled_app.create_provider_connection(
                name="still runnable fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
            )
            await disabled_app.refresh_models(
                alternate.id, FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
            )
            disabled_queue = disabled_store.enqueue_archive_import(
                source, resolver_roots=(intake,), now=datetime(2026, 9, 20, tzinfo=UTC),
                queue_id="disabled-after-preflight",
            )
            disabled_preflight = disabled_store.preflight_archive_import(
                disabled_queue.id, owner_epoch="disabled-after-preflight-operation",
            )
            assert len(disabled_preflight.captured.plan.initial_receiver_continuation) == 1
            frozen = disabled_preflight.captured.plan.initial_receiver_continuation[0]
            frozen_provider = next(
                row for row in disabled_store.list_provider_connections()
                if row.id == frozen["connection_id"]
            )
            await disabled_app.set_provider_connection_enabled(
                frozen_provider.id, False, expected_revision=frozen_provider.revision,
            )
            assert any(
                provider.id != frozen_provider.id and provider.enabled and not provider.retired
                for provider in disabled_store.list_provider_connections()
            )
            disabled_cutoff = disabled_store.cross_archive_import_cutoff(
                disabled_preflight, now=datetime(2026, 9, 20, tzinfo=UTC),
                operation_id="disabled-after-preflight-operation",
            )
            with pytest.raises(StateError, match="sealed receiver continuation target is no longer runnable"):
                disabled_store.settle_archive_import(disabled_cutoff)
            with disabled_store.command_admission():
                with disabled_store._engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM chats",
                    ).scalar_one() == 0
                    assert connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM archive_import_journal WHERE operation_id=?",
                        ("disabled-after-preflight-operation",),
                    ).scalar_one() == 1
        finally:
            await disabled_app.close()
        disabled_reopened_authority = DataRootAuthority(tmp_path / "disabled-root").acquire()
        disabled_reopened = disabled_reopened_authority.open_store()
        try:
            # Startup already settled this known pre-graph journal.  A later
            # manual recovery sees nothing resumable and no chat exists.
            assert disabled_reopened.recover_archive_imports(
                now=datetime(2026, 9, 20, tzinfo=UTC),
            ) == ()
            assert disabled_reopened.list_chats() == ()
            with disabled_reopened.command_admission():
                with disabled_reopened._engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT state,failure_code FROM archive_import_operations WHERE id=?",
                        ("disabled-after-preflight-operation",),
                    ).one() == ("FAILED", "RECOVERED_NO_GRAPH")
        finally:
            disabled_reopened.close(); disabled_reopened_authority.close()

    finally:
        await source_app.close()
        if destination_app is not None:
            await destination_app.close()


@pytest.mark.asyncio
async def test_v1_fake_explicit_unsupported_override_remains_unresolved(tmp_path):
    """Import preserves an explicit source setting when no runnable target exists."""
    intake = tmp_path / "intake"; intake.mkdir()
    source_app, source_store, _source_authority = _configured_application(tmp_path / "source-root")
    destination_app, destination_store, _destination_authority = _configured_application(tmp_path / "destination-root")
    try:
        source_connection = await source_app.create_provider_connection(
            name="source fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
        )
        await source_app.refresh_models(
            source_connection.id, FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
        )
        source_model = source_store.get_model_catalogue_entry_by_provider_id(
            source_connection.id, "fake-v0.1",
        )
        assert source_model is not None
        source_chat = await source_app.create_chat("explicit source setting")
        source_store.set_chat_model_selection(source_chat.id, source_model.id)
        source_store.set_chat_model_generation_settings(
            source_chat.id, source_model.id, GenerationSettings(temperature=0.5),
        )
        source_attempt = await source_app.send_message(source_chat.id, "archive this")
        await _finish(source_app, source_attempt.id)
        source = intake / "explicit-unsupported.botsarchive"
        source.write_bytes(archive_bytes(await source_app.prepare_archive_export(source_chat.id)))

        target = destination_store.get_model_catalogue_entry_by_provider_id(
            destination_store.list_provider_connections()[0].id, "fake-v0.1",
        )
        assert target is not None
        await destination_app.set_capability_override(CapabilityOverride(
            target.id, CapabilityKey.TEMPERATURE, CapabilityState.UNSUPPORTED,
        ))
        providers_before = tuple(destination_store.list_provider_connections())
        queued = destination_store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=datetime(2026, 9, 20, tzinfo=UTC),
            queue_id="explicit-unsupported",
        )
        chat_id = destination_store.execute_archive_import(
            queued.id, now=datetime(2026, 9, 20, tzinfo=UTC),
            operation_id="explicit-unsupported-operation",
        )
        head = destination_store.get_chat(chat_id).head_message_id
        assert head is not None
        with destination_store.command_admission():
            with destination_store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT resolution FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?",
                    (chat_id, head),
                ).scalar_one() == "UNRESOLVED"
                assert connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM archive_continuation_choices WHERE chat_id=? AND base_key=?",
                    (chat_id, head),
                ).scalar_one() == 0
                configuration = json.loads(connection.exec_driver_sql(
                    "SELECT source_configuration FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?",
                    (chat_id, head),
                ).scalar_one())
        assert configuration["overrides"] == [{
            "model": configuration["selection"]["model"], "revision": 1,
            "temperature": 0.5, "max_output_tokens": None,
            "reasoning_effort": None, "timeout_seconds": None,
        }]
        assert tuple(destination_store.list_provider_connections()) == providers_before
    finally:
        await source_app.close()
        await destination_app.close()


@pytest.mark.asyncio
async def test_http_archive_without_endpoint_identity_remains_unresolved(tmp_path, monkeypatch):
    """An HTTP descriptor never authorizes endpoint, catalogue, or secret work."""
    intake = tmp_path / "intake"; intake.mkdir()
    source_app, source_store, _source_authority = _configured_application(tmp_path / "source-root")
    destination_app, destination_store, _destination_authority = _configured_application(tmp_path / "destination-root")
    try:
        source_connection = await source_app.create_provider_connection(
            name="archived HTTP", backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
            profile=ProviderProfile.GENERIC, endpoint="http://127.0.0.1:9187/v1",
        )
        source_model = await source_app.add_manual_model(
            connection_id=source_connection.id, provider_model_id="archived-http-model",
            display_name="Archived HTTP model",
        )
        source_chat = await source_app.create_chat("http source")
        source_store.set_chat_model_selection(source_chat.id, source_model.id)
        source = intake / "http-without-endpoint.botsarchive"
        source.write_bytes(archive_bytes(await source_app.prepare_archive_export(source_chat.id)))

        def forbidden(*_args, **_kwargs):
            raise AssertionError("archive import must not invoke a provider, catalogue, or secret operation")

        monkeypatch.setattr(destination_app, "refresh_models", forbidden)
        monkeypatch.setattr(destination_app, "create_provider_connection", forbidden)
        monkeypatch.setattr(destination_app._configuration, "_secret_status", forbidden)
        providers_before = tuple(destination_store.list_provider_connections())
        queued = destination_store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=datetime(2026, 9, 20, tzinfo=UTC),
            queue_id="http-no-endpoint",
        )
        chat_id = destination_store.execute_archive_import(
            queued.id, now=datetime(2026, 9, 20, tzinfo=UTC),
            operation_id="http-no-endpoint-operation",
        )
        with destination_store.command_admission():
            with destination_store._engine.connect() as connection:
                anchor = connection.exec_driver_sql(
                    "SELECT base_key,resolution,source_configuration FROM archive_continuation_anchors WHERE chat_id=?",
                    (chat_id,),
                ).mappings().one()
                assert anchor["base_key"] == "empty"
                assert anchor["resolution"] == "UNRESOLVED"
                assert connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM archive_continuation_choices WHERE chat_id=?",
                    (chat_id,),
                ).scalar_one() == 0
        configuration = json.loads(str(anchor["source_configuration"]))
        assert configuration["selection"]["model"]["backend_type"] == "openai_compatible_http"
        assert "endpoint" not in json.dumps(configuration, sort_keys=True)
        assert tuple(destination_store.list_provider_connections()) == providers_before
    finally:
        await source_app.close()
        await destination_app.close()


def test_imported_inspector_provenance_is_metadata_only(tmp_path, monkeypatch):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "source.botsarchive"
    source.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="inspect")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="inspect-operation")
        message_id = store.list_messages(chat_id)[0].id
        monkeypatch.setattr(
            type(store._attachment_manager), "read_verified",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Inspector opened payload bytes")),
        )
        with store.command_admission():
            provenance = store.inspection_import_provenance(chat_id, message_id)
        assert provenance["Archive version"] == "2"
        assert provenance["Source chat ID"] == "chat"
        assert provenance["Source message ID"] in {"user", "assistant"}
        assert len(provenance["Archive digest"]) == 64
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_imported_inspector_attachment_metadata_includes_ready_references(
    tmp_path, monkeypatch,
):
    """Metadata-only Inspector projections include READY imported attachments."""
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"inspector ready imported attachment"
    digest = hashlib.sha256(payload).hexdigest()
    source = intake / "ready.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    (intake / "payload.bin").write_bytes(payload)
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        store = authority.open_store()
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="inspect-ready")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="inspect-ready-operation")
        with store.command_admission():
            with store._engine.connect() as db:
                message_id, ref_id = db.exec_driver_sql(
                    "SELECT im.message_id,r.id "
                    "FROM archive_import_message_attachment_refs im "
                    "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
                    "WHERE r.operation_id='inspect-ready-operation'"
                ).one()
                attempt_id, attempt_ref_id = db.exec_driver_sql(
                    "SELECT ia.attempt_id,r.id "
                    "FROM archive_import_attempt_attachment_refs ia "
                    "JOIN archive_import_attachment_refs r ON r.id=ia.attachment_ref_id "
                    "WHERE r.operation_id='inspect-ready-operation'"
                ).one()
                assert attempt_ref_id == ref_id
        monkeypatch.setattr(
            type(store._attachment_manager), "read_verified",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Inspector metadata opened payload bytes")
            ),
        )
        message_metadata = store.list_message_attachment_metadata(str(message_id))
        attempt_metadata = store.list_attempt_attachment_metadata(str(attempt_id))
        assert tuple(item.id for item in message_metadata) == (str(ref_id),)
        assert tuple(item.id for item in attempt_metadata) == (str(ref_id),)
        assert store.get_chat(chat_id) is not None
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_missing_external_reference_publishes_truthful_reserved_identity(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    source = intake_root / "missing.botsarchive"
    source.write_bytes(missing_external_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake_root,), now=now, queue_id="missing-queue"
        )
        chat_id = store.execute_archive_import(
            queued.id, now=now, operation_id="missing-operation"
        )
        with store.command_admission():
            with store._engine.connect() as connection:
                row = connection.exec_driver_sql(
                    "SELECT attachment_id,availability,expected_size FROM archive_import_attachment_refs"
                ).fetchone()
                assert row == (None, "MISSING_EXTERNAL", 7)
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_import_message_attachment_refs"
                ).scalar_one() == 1
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM chats WHERE id=?", (chat_id,)
                ).scalar_one() == 1
                requirement = connection.exec_driver_sql(
                    "SELECT r.ordinal,r.binding_kind,r.bound_imported_ref_id,"
                    "hex(r.expected_digest),hex(r.representation_digest),r.source_imported_attempt_id,"
                    "c.imported_ref_id FROM archive_continuation_requirements AS r "
                    "JOIN archive_continuation_requirement_candidates AS c "
                    "ON c.chat_id=r.chat_id AND c.base_key=r.base_key AND c.ordinal=r.ordinal"
                ).fetchone()
                assert requirement[0] == 1
                assert requirement[1] == "identity"
                assert requirement[2] == requirement[6]
                assert requirement[3].lower() == "a" * 64
                assert requirement[4].lower() == "a" * 64
                assert requirement[5] is not None
        readiness = store.import_continuation_readiness(chat_id, store.get_chat(chat_id).head_message_id)
        assert readiness is not None
        assert readiness.resolution == "UNAVAILABLE"
        assert len(readiness.requirements) == 1
        assert readiness.requirements[0].blocked_reason == "missing-external"
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_verified_intake_heals_every_matching_reserved_attachment_identity(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    payload = intake_root / "healed.txt"
    payload.write_bytes(b"healed!")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    archive = intake_root / "missing.botsarchive"
    archive.write_bytes(missing_external_archive(digest=digest, size=payload.stat().st_size))
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(
            archive, resolver_roots=(intake_root,), now=now, queue_id="healing-queue"
        )
        store.execute_archive_import(queued.id, now=now, operation_id="healing-operation")
        ordinary = store.ingest_attachment(payload)
        message_id = None
        chat_id = None
        with store.command_admission():
            with store._engine.connect() as connection:
                ref_id, attachment_id, availability, blob_digest, source_kind, source_metadata = connection.exec_driver_sql(
                    "SELECT r.id,r.attachment_id,r.availability,a.blob_digest,a.source_kind,r.source_metadata "
                    "FROM archive_import_attachment_refs AS r "
                    "JOIN attachments AS a ON a.id=r.id"
                ).fetchone()
                assert ref_id == attachment_id
                assert availability == "READY"
                assert bytes(blob_digest).hex() == digest
                assert source_kind == "imported"
                assert json.loads(str(source_metadata))["source_kind"] == "filesystem"
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM attachments WHERE blob_digest=?", (bytes.fromhex(digest),)
                ).scalar_one() == 2
                message_id = connection.exec_driver_sql(
                    "SELECT message_id FROM archive_import_message_attachment_refs"
                ).scalar_one()
                chat_id = connection.exec_driver_sql(
                    "SELECT chat_id FROM archive_import_chats"
                ).scalar_one()
        assert store.get_attachment(str(ref_id)) is not None
        assert ordinary.id != str(ref_id)
        assert tuple(item.id for item in store.list_message_attachments(str(message_id))) == (str(ref_id),)
        readiness = store.import_continuation_readiness(str(chat_id), store.get_chat(str(chat_id)).head_message_id)
        assert readiness is not None
        assert readiness.requirements[0].imported_ref_id == str(ref_id)
        assert readiness.requirements[0].blocked_reason is None
        with pytest.raises(StateError, match="durable historical references"):
            store.delete_attachment(str(ref_id))
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_imported_backing_keeps_archived_safe_filename_over_same_digest_local_name(tmp_path):
    """D390 never recasts imported metadata from an unrelated local backing."""
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"same digest, distinct safe filename"
    digest = hashlib.sha256(payload).hexdigest()
    source = intake / "archive.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    local = intake / "unrelated-local-name.txt"; local.write_bytes(payload)
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        local_attachment = store.ingest_attachment(local)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="safe-imported-filename")
        with store.command_admission():
            with store._engine.connect() as connection:
                imported_id, filename, source_name, availability = connection.exec_driver_sql(
                    "SELECT a.id,a.filename,a.source_name,r.availability "
                    "FROM archive_import_attachment_refs r JOIN attachments a ON a.id=r.id "
                    "WHERE r.operation_id='safe-imported-filename'"
                ).one()
                assert imported_id != local_attachment.id
                assert (filename, source_name, availability) == ("attachment.txt", "attachment.txt", "READY")
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM attachments WHERE blob_digest=?", (bytes.fromhex(digest),)
                ).scalar_one() == 2
        assert store.get_chat(chat_id) is not None
    finally:
        store.close()


def test_redacted_ready_healing_preserves_sealed_filename_truth(tmp_path):
    """Healing must not replace a sealed redaction token with a local filename."""
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"redacted healing payload"
    digest = hashlib.sha256(payload).hexdigest()
    raw = missing_external_archive(digest=digest, size=len(payload))

    def redact_filename(entries, _manifest):
        rows = list(parse_jsonl(entries["domain/attachments.jsonl"]))
        rows[0]["filename"] = "[redacted]"
        rows[0]["filename_status"] = "redacted"
        entries["domain/attachments.jsonl"] = canonical_jsonl_bytes(rows)

    source = intake / "redacted-missing.botsarchive"
    source.write_bytes(_repack_canonical_archive(raw, redact_filename))
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        store = authority.open_store()
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="redacted-healing",
        )
        store.execute_archive_import(queued.id, now=now, operation_id="redacted-healing-operation")
        ordinary = intake / "ordinary-local-name.txt"; ordinary.write_bytes(payload)
        local_attachment = store.ingest_attachment(ordinary)
        with store.command_admission():
            with store._engine.connect() as connection:
                ref_id, filename, source_name, availability, metadata = connection.exec_driver_sql(
                    "SELECT a.id,a.filename,a.source_name,r.availability,r.source_metadata "
                    "FROM archive_import_attachment_refs r JOIN attachments a ON a.id=r.id "
                    "WHERE r.operation_id='redacted-healing-operation'"
                ).one()
                assert availability == "READY"
                assert (filename, source_name) == ("[redacted]", "[redacted]")
                assert json.loads(str(metadata))["filename_status"] == "redacted"
                assert ref_id != local_attachment.id
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_empty_import_requires_v2_and_reimports_its_provenance(tmp_path):
    """An imported empty chat still has immutable v2 provenance to preserve."""
    intake = tmp_path / "intake"; intake.mkdir()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    empty = archive_v2_bytes({
        "format": "org.necromilias.bots5.chat-archive", "archive_version": 2,
        "archive_id": "empty-import", "created_at": "2026-09-20T00:00:00.000000Z",
        "source_application_version": "0.1", "source_db_migration_revision": "0012_phase9_archive_import",
        "source_chat": {"source_id": "empty-source", "title": "empty"},
        "attachment_policy": "embedded", "self_contained": True,
        "features": sorted(FEATURES), "external_resources": [], "secret_exclusion": "safe fields only",
    }, {
        "domain/chat.json": canonical_json_bytes({
            "source_id": "empty-source", "title": "empty", "created_at": "2026-09-20T00:00:00.000000Z",
            "updated_at": "2026-09-20T00:00:00.000000Z", "archived_at": None,
            "head_message_id": None, "revision": 0,
        }),
        "domain/messages.jsonl": b"", "domain/attempts.jsonl": b"", "domain/context-plans.jsonl": b"",
        "domain/attachments.jsonl": b"", "domain/message-attachments.jsonl": b"", "domain/attempt-attachments.jsonl": b"",
        "domain/chat-configuration.json": canonical_json_bytes({
            "semantic": "inert-continuation-hints", "selection": None, "overrides": [],
        }),
        "domain/provenance.json": canonical_json_bytes({
            "source_origin": "imported", "import_origin": "recorded", "attachment_policy": "embedded", "resources": [],
        }),
        "domain/object-provenance.jsonl": canonical_jsonl_bytes([{
            "object_kind": "chat", "object_id": "empty-source",
            "source": {"kind": "v1-bootstrap", "immediate": {
                "archive_id": "older", "archive_version": 1, "logical_content_digest": "b" * 64,
                "object_id": "empty-source", "imported_at": "2026-09-20T00:00:00.000000Z",
            }, "prior_chain": []}, "derivation": {"kind": "root", "predecessor": None},
        }]),
        "domain/continuation-history.json": canonical_json_bytes({
            "active_head_message_id": None, "anchors": [], "choices": [], "branches": [],
        }),
        "domain/history-bindings.jsonl": b"",
    })
    source = intake / "empty-import.botsarchive"; source.write_bytes(empty)

    async def scenario():
        app, store, authority = _configured_application(tmp_path / "root")
        second_authority = second_store = None
        try:
            queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
            chat_id = store.execute_archive_import(queued.id, now=now, operation_id="empty-import-op")
            export_source = store.read_chat_export_source(chat_id, attachment_policy=AttachmentPolicy.EMBEDDED)
            assert export_source.requires_v2 is True
            projection = await app.prepare_archive_export(chat_id)
            assert projection.manifest_base["archive_version"] == 2
            with pytest.raises(Exception, match="Archive version 2"):
                await app.prepare_archive_export(chat_id, archive_version=1)
            exported = intake / "empty-exported.botsarchive"; exported.write_bytes(archive_bytes(projection))
            assert validate_archive(io.BytesIO(exported.read_bytes())).archive_id
            second_authority = DataRootAuthority(tmp_path / "second-root").acquire(); second_store = second_authority.open_store()
            queued = second_store.enqueue_archive_import(exported, resolver_roots=(intake,), now=now)
            second_chat = second_store.execute_archive_import(queued.id, now=now, operation_id="empty-reimport-op")
            with second_store.command_admission():
                with second_store._engine.connect() as connection:
                    assert connection.exec_driver_sql(
                        "SELECT source_chat_id FROM archive_import_chats WHERE chat_id=?", (second_chat,)
                    ).scalar_one() == chat_id
                    assert connection.exec_driver_sql(
                        "SELECT count(*) FROM archive_lineage_nodes "
                        "WHERE object_kind='chat' AND source_object_id='empty-source'"
                    ).scalar_one() >= 1
        finally:
            if second_store is not None:
                second_store.close()
            elif second_authority is not None:
                second_authority.close()
            await app.close()
    asyncio.run(scenario())


def test_redacted_selected_slots_keep_same_digest_multiplicity_and_reject_unrelated_candidates(tmp_path):
    intake_root = tmp_path / "intake"; intake_root.mkdir()
    source = intake_root / "redacted.botsarchive"
    source.write_bytes(missing_external_archive(
        selected_slots=2, redacted_slots=True, unrelated_same_digest=True,
    ))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    settings = {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake_root,), now=now, queue_id="redacted-slots")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="redacted-slots-op")
        local_base = store.get_chat(chat_id).head_message_id
        assert local_base is not None
        with store.command_admission():
            with store._engine.connect() as connection:
                requirements = connection.exec_driver_sql(
                    "SELECT ordinal,binding_kind,bound_imported_ref_id FROM archive_continuation_requirements "
                    "WHERE chat_id=? AND base_key=? ORDER BY ordinal", (chat_id, local_base)
                ).fetchall()
                assert requirements == [(1, "digest", None), (2, "digest", None)]
                candidate_counts = connection.exec_driver_sql(
                    "SELECT ordinal,count(*) FROM archive_continuation_requirement_candidates "
                    "WHERE chat_id=? AND base_key=? GROUP BY ordinal ORDER BY ordinal", (chat_id, local_base)
                ).fetchall()
                assert candidate_counts == [(1, 2), (2, 2)]
                unrelated_ref = connection.exec_driver_sql(
                    "SELECT id FROM archive_import_attachment_refs WHERE source_attachment_id='unrelated'"
                ).scalar_one()
        readiness = store.import_continuation_readiness(chat_id, local_base)
        assert readiness is not None and readiness.resolution == "UNAVAILABLE"
        assert [item.ordinal for item in readiness.requirements] == [1, 2]
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        with pytest.raises(StateError, match="not a source requirement candidate"):
            store.admit_import_continuation_choice(
                chat_id, local_base, expected_choice_revision=0,
                connection_id=connection.id, model_entry_id=model.id, explicit_settings=settings,
                excluded_refs=({
                    "requirement_ordinal": 1, "expected_digest": "a" * 64,
                    "attachment_id": str(unrelated_ref), "reason": "operator-excluded",
                },), now=now,
            )
    finally:
        store.close()


def test_actual_redacted_safe_source_keeps_digest_requirement_without_identity(tmp_path):
    """A real redacted v3 source uses the closed redaction token, not an alias."""
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "redacted-source.botsarchive"
    source.write_bytes(missing_external_archive(redacted_slots=True))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        with zipfile.ZipFile(io.BytesIO(source.read_bytes())) as package:
            attempt = parse_jsonl(package.read("domain/attempts.jsonl"))[0]
            attachment_source = next(
                item for item in attempt["request_time_provenance"]["context"]["sources"]
                if item["kind"] == "attachment"
            )
            assert attachment_source["source_id"] == "[redacted]"
            assert attachment_source["source_id_status"] == "redacted"
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="actual-redacted")
        base_key = store.get_chat(chat_id).head_message_id
        assert base_key is not None
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT ordinal,binding_kind,bound_imported_ref_id FROM archive_continuation_requirements "
                    "WHERE chat_id=? AND base_key=?", (chat_id, base_key),
                ).one() == (1, "digest", None)
        readiness = store.import_continuation_readiness(chat_id, base_key)
        assert readiness is not None and readiness.requirements[0].blocked_reason == "missing-external"
    finally:
        store.close()


def test_explicit_degraded_exclusions_preserve_missing_slots_and_materialize_no_fake_input(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "missing.botsarchive"
    source.write_bytes(missing_external_archive(selected_slots=2, redacted_slots=True))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    settings = {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="degraded-choice")
        base_key = store.get_chat(chat_id).head_message_id
        assert base_key is not None
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        exclusions = tuple(
            {"requirement_ordinal": ordinal, "expected_digest": "a" * 64,
             "attachment_id": None, "reason": "missing-external"}
            for ordinal in (1, 2)
        )
        assert store.admit_import_continuation_choice(
            chat_id, base_key, expected_choice_revision=0,
            connection_id=connection.id, model_entry_id=model.id,
            explicit_settings=settings, excluded_refs=exclusions, now=now,
        ) == 1
        readiness = store.import_continuation_readiness(chat_id, base_key)
        assert readiness is not None and readiness.excluded_refs == exclusions
        assert readiness.resolution != "UNAVAILABLE"
        assert store.materialize_import_continuation_attachments(chat_id, base_key) == ()
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql(
                    "SELECT degraded,excluded_refs FROM archive_continuation_choices "
                    "WHERE chat_id=? AND base_key=? AND choice_revision=1", (chat_id, base_key),
                ).fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_degraded_exclusion_keeps_source_ordinal_while_native_send_and_regeneration_use_dense_plan(tmp_path):
    """An excluded first source slot must not authorize the wrong candidate.

    The imported requirement ordinal remains immutable evidence.  The native
    Phase 6 context plan is dense, so retained source ordinal 1 is native
    ordinal 0.  This exercises both public consumers through the real store
    guards, then proves late healing does not revise the original choice.
    """
    resolver = tmp_path / "resolver"; resolver.mkdir()
    ready_payload = b"retained source slot"
    ready_digest = hashlib.sha256(ready_payload).hexdigest()
    missing_payload = b"excluded source slot"
    missing_digest = hashlib.sha256(missing_payload).hexdigest()
    source = resolver / "degraded-two-slots.botsarchive"
    source.write_bytes(missing_external_archive(
        selected_slots=2, redacted_slots=True,
        slot_identities=((missing_digest, len(missing_payload)), (ready_digest, len(ready_payload))),
    ))
    (resolver / "retained.bin").write_bytes(ready_payload)
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 20, tzinfo=UTC)
    settings = {"temperature": None, "max_output_tokens": None, "reasoning_effort": None, "timeout_seconds": None}
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(resolver,), now=now, queue_id="degraded-native")
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="degraded-native-operation")
        imported_base = store.get_chat(chat_id).head_message_id
        assert imported_base is not None
        connection = store.list_provider_connections()[0]
        model = store.list_model_catalogue_entries(connection.id)[0]
        exclusion = ({
            "requirement_ordinal": 1, "expected_digest": missing_digest,
            "attachment_id": None, "reason": "missing-external",
        },)
        assert await app.choose_import_continuation(
            chat_id, imported_base, expected_choice_revision=0,
            connection_id=connection.id, model_entry_id=model.id,
            explicit_settings=settings, excluded_refs=exclusion,
        ) == 1
        readiness = await app.import_continuation_readiness(chat_id, imported_base)
        assert readiness is not None and readiness.excluded_refs == exclusion
        assert [item.blocked_reason for item in readiness.requirements] == ["missing-external", None]

        sent = await app.send_message(chat_id, "continue with retained source slot")
        await _finish(app, sent.id)
        regenerated = await app.regenerate_message(chat_id, imported_base)
        await _finish(app, regenerated.id)
        with store.command_admission():
            with store._engine.connect() as db:
                retained_ref = db.exec_driver_sql(
                    "SELECT id FROM archive_import_attachment_refs WHERE source_attachment_id='attachment-1'"
                ).scalar_one()
                for attempt_id in (sent.id, regenerated.id):
                    assert db.exec_driver_sql(
                        "SELECT attachment_id,ordinal FROM attempt_attachments WHERE attempt_id=?", (attempt_id,)
                    ).all() == [(retained_ref, 0)]
                assert db.exec_driver_sql(
                    "SELECT degraded,excluded_refs FROM archive_continuation_choices "
                    "WHERE chat_id=? AND base_key=? AND choice_revision=1", (chat_id, imported_base)
                ).fetchone() == (1, json.dumps(list(exclusion), separators=(",", ":")))

        (resolver / "excluded.bin").write_bytes(missing_payload)
        store.ingest_attachment(resolver / "excluded.bin")
        healed = await app.import_continuation_readiness(chat_id, imported_base)
        assert healed is not None and healed.excluded_refs == exclusion
        assert [item.blocked_reason for item in healed.requirements] == [None, None]
        projection = await app.prepare_archive_export(
            chat_id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE, archive_version=2,
        )
        exported_path = resolver / "degraded-roundtrip.botsarchive"
        exported_path.write_bytes(archive_bytes(projection))
        second_authority = DataRootAuthority(tmp_path / "roundtrip-root").acquire()
        second_store = second_authority.open_store()
        try:
            second_queued = second_store.enqueue_archive_import(
                exported_path, resolver_roots=(resolver,), now=now, queue_id="degraded-roundtrip",
            )
            second_chat = second_store.execute_archive_import(
                second_queued.id, now=now, operation_id="degraded-roundtrip-operation",
            )
            with second_store.command_admission():
                with second_store._engine.connect() as db:
                    local_base = db.exec_driver_sql(
                        "SELECT message_id FROM archive_import_messages WHERE chat_id=? AND source_message_id=?",
                        (second_chat, imported_base),
                    ).scalar_one()
                    persisted = db.exec_driver_sql(
                        "SELECT degraded,excluded_refs FROM archive_continuation_choices "
                        "WHERE chat_id=? AND base_key=? AND choice_revision=1", (second_chat, local_base)
                    ).fetchone()
                    assert persisted is not None and persisted[0] == 1
                    assert tuple(json.loads(persisted[1])) == exclusion
        finally:
            second_store.close()
    finally:
        store.close()


def test_recovery_marks_a_durable_pregraph_operation_failed_without_replay(tmp_path):
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    archive = intake_root / "source.botsarchive"
    archive.write_bytes(_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire()
    store = None
    try:
        store = authority.open_store()
        now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(
            archive, resolver_roots=(intake_root,), now=now, queue_id="recovery-queue"
        )
        store.stage_archive_import(queued.id, now=now, operation_id="recovery-operation")
        assert store.recover_archive_imports(now=now) == (("recovery-operation", "KNOWN_NO_COMMIT"),)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,failure_code FROM archive_import_queue WHERE id=?", (queued.id,)
                ).fetchone() == ("FAILED", "RECOVERED_NO_GRAPH")
                assert connection.exec_driver_sql(
                    "SELECT state,failure_code FROM archive_import_operations WHERE id='recovery-operation'"
                ).fetchone() == ("FAILED", "RECOVERED_NO_GRAPH")
                assert connection.exec_driver_sql(
                    "SELECT phase FROM archive_import_journal WHERE operation_id='recovery-operation'"
                ).scalar_one() == "FAILED"
                assert connection.exec_driver_sql(
                    "SELECT claimed_queue_id FROM archive_import_queue_control"
                ).scalar_one() is None
    finally:
        if store is not None:
            store.close()
        else:
            authority.close()


def test_embedded_archive_import_publishes_reserved_ready_bytes_across_reopen(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"embedded archive payload"
    source = intake / "embedded.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=payload)))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="embedded-queue")
        store.execute_archive_import(queued.id, now=now, operation_id="embedded-operation")
        with store.command_admission():
            with store._engine.connect() as connection:
                ref = connection.exec_driver_sql("SELECT id,attachment_id,availability FROM archive_import_attachment_refs").fetchone()
                assert ref[0] == ref[1] and ref[2] == "READY"
                ref_id = str(ref[0])
        assert store.read_attachment_bytes(ref_id) == payload
        store.close(); authority.close(); authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
        assert store.read_attachment_bytes(ref_id) == payload
    finally:
        if store is not None: store.close()
        else: authority.close()


def test_embedded_payload_dedup_capacity_and_cutoff_gc_reservation(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"dedup before capture"
    source = intake / "embedded-dedup.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=payload)))
    payload_path = intake / "already-ready.bin"; payload_path.write_bytes(payload)
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 20, tzinfo=UTC)
        ordinary = store.ingest_attachment(payload_path)
        store.delete_attachment(ordinary.id)
        # A preflight capacity failure has no cutoff/journal effect and closes
        # the retained snapshots through the normal failed preflight settlement.
        manager_type = type(store._attachment_manager)
        original_capacity = manager_type.namespace_capacity
        monkeypatch.setattr(
            manager_type, "namespace_capacity",
            lambda manager, area: (
                original_capacity(manager, area)[0], 0, original_capacity(manager, area)[2]
            ),
        )
        limited = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="capacity-limited",
        )
        with pytest.raises(StateError, match="capacity"):
            store.preflight_archive_import(limited.id, owner_epoch="capacity-owner")
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT state,failure_code,operation_id FROM archive_import_queue WHERE id=?", (limited.id,)
                ).one() == ("FAILED", "RESOURCE_LIMIT", None)
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM archive_import_journal").scalar_one() == 0
        monkeypatch.setattr(manager_type, "namespace_capacity", original_capacity)

        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="dedup-cutoff",
        )
        preflight = store.preflight_archive_import(queued.id, owner_epoch="dedup-cutoff-operation")
        settlement = store.cross_archive_import_cutoff(
            preflight, now=now, operation_id="dedup-cutoff-operation",
        )
        # The ready blob is orphaned, so it is a GC candidate except for the
        # durable post-cutoff payload reservation.
        assert store.gc_attachments() == ()
        original_capture = manager_type.capture_verified_snapshot
        monkeypatch.setattr(
            manager_type, "capture_verified_snapshot",
            lambda *_args, **_kwargs: pytest.fail("dedup must verify existing CAS before capture allocation"),
        )
        chat_id = store.settle_archive_import(settlement)
        monkeypatch.setattr(manager_type, "capture_verified_snapshot", original_capture)
        with store.command_admission():
            with store._engine.connect() as connection:
                ref_id = connection.exec_driver_sql(
                    "SELECT id FROM archive_import_attachment_refs WHERE operation_id=?",
                    ("dedup-cutoff-operation",),
                ).scalar_one()
        assert store.read_attachment_bytes(str(ref_id)) == payload
    finally:
        if store is not None: store.close()
        else: authority.close()


def test_reopen_rejects_conflicting_imported_digest_sizes_before_healing(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "two-missing.botsarchive"
    source.write_bytes(missing_external_archive(selected_slots=2))
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 20, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="size-conflict")
        store.execute_archive_import(queued.id, now=now, operation_id="size-conflict-operation")
        store.close(); store = None; authority.close(); authority = None
        database = root / "database" / "state.sqlite3"
        with sqlite3.connect(database) as connection:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='phase9_import_attachment_ref_metadata_immutable'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER phase9_import_attachment_ref_metadata_immutable")
            connection.execute(
                "UPDATE archive_import_attachment_refs SET expected_size=expected_size+1 "
                "WHERE id=(SELECT id FROM archive_import_attachment_refs ORDER BY id LIMIT 1)"
            )
            connection.execute(str(trigger_sql))
            connection.commit()
        authority = DataRootAuthority(root).acquire()
        with pytest.raises(RuntimeError, match="contradict a digest size identity"):
            authority.open_store()
    finally:
        if store is not None: store.close()
        if authority is not None: authority.close()


def test_reopen_rejects_same_name_weakened_phase9_trigger(tmp_path):
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = None
    try:
        store = authority.open_store()
        store.close(); store = None; authority.close(); authority = None
        database = root / "database" / "state.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TRIGGER phase9_import_node_exact_guard")
            connection.execute(
                "CREATE TRIGGER phase9_import_node_exact_guard BEFORE INSERT ON archive_lineage_nodes "
                "BEGIN SELECT 1; END"
            )
            connection.commit()
        authority = DataRootAuthority(root).acquire()
        with pytest.raises(RuntimeError, match="schema definition is contradictory"):
            authority.open_store()
    finally:
        if store is not None: store.close()
        if authority is not None: authority.close()


def test_retained_payload_snapshot_prefers_invalid_utf8_after_earlier_nul(tmp_path):
    payload = b"\0" + b"a" * (1024 * 1024) + b"\xff"
    source = tmp_path / "invalid-after-nul.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=payload)))
    captured = capture_validated_archive(source, source_fingerprint(source))
    try:
        assert len(captured.payloads) == 1
        _digest, snapshot = captured.payloads[0]
        assert snapshot.text_representation_id is None
        assert snapshot.ineligibility_reason == "invalid_utf8"
    finally:
        close_payload_snapshots(captured)


def test_cutoff_rejects_ready_dedup_object_replaced_after_preflight(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"stable CAS bytes"
    source = intake / "source.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=payload)))
    payload_path = intake / "ready.bin"; payload_path.write_bytes(payload)
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 20, tzinfo=UTC)
        ordinary = store.ingest_attachment(payload_path)
        digest = hashlib.sha256(payload).hexdigest()
        store.delete_attachment(ordinary.id)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="identity-swap")
        preflight = store.preflight_archive_import(queued.id, owner_epoch="identity-swap-operation")
        replacement = root / "attachments" / "objects" / ".replacement"
        replacement.write_bytes(payload)
        os.replace(replacement, root / "attachments" / "objects" / digest)
        with pytest.raises(StateError, match="changed after preflight verification"):
            store.cross_archive_import_cutoff(
                preflight, now=now, operation_id="identity-swap-operation",
            )
    finally:
        if store is not None: store.close()
        else: authority.close()


def test_cutoff_capacity_drop_has_no_journal_or_graph(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=b"cutoff capacity")))
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 20, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="cutoff-capacity")
        preflight = store.preflight_archive_import(queued.id, owner_epoch="cutoff-capacity-operation")
        manager_type = type(store._attachment_manager)
        original_capacity = manager_type.namespace_capacity
        monkeypatch.setattr(
            manager_type, "namespace_capacity",
            lambda manager, area: (
                original_capacity(manager, area)[0], 0, original_capacity(manager, area)[2]
            ),
        )
        with pytest.raises(StateError, match="capacity"):
            store.cross_archive_import_cutoff(
                preflight, now=now, operation_id="cutoff-capacity-operation",
            )
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM archive_import_journal").scalar_one() == 0
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM archive_import_operations").scalar_one() == 0
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM chats").scalar_one() == 0
    finally:
        if store is not None: store.close()
        else: authority.close()


def test_postcutoff_enospc_recovers_known_no_graph_without_replay(tmp_path):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=b"post cutoff ENOSPC")))
    root = tmp_path / "root"
    authority = DataRootAuthority(root).acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 20, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="enospc")
        settlement = store.cross_archive_import_cutoff(
            store.preflight_archive_import(queued.id, owner_epoch="enospc-operation"),
            now=now, operation_id="enospc-operation",
        )
        def fail_capture(point):
            if point == "after-capture-file-fsync":
                raise OSError(errno.ENOSPC, "No space left on device")
        attachment_fs._TEST_FAULT_HOOK = fail_capture
        with pytest.raises(OSError, match="No space"):
            store.settle_archive_import(settlement)
        attachment_fs._TEST_FAULT_HOOK = None
        store.close(); store = None; authority.close(); authority = None
        authority = DataRootAuthority(root).acquire(); store = authority.open_store()
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM chats").scalar_one() == 0
                assert connection.exec_driver_sql(
                    "SELECT state,failure_code FROM archive_import_operations WHERE id='enospc-operation'"
                ).one() == ("FAILED", "RECOVERED_NO_GRAPH")
                assert connection.exec_driver_sql(
                    "SELECT phase FROM archive_import_journal WHERE operation_id='enospc-operation'"
                ).scalar_one() == "FAILED"
        assert store._attachment_manager.inventory("captures") == ()
        assert store._attachment_manager.inventory("staging") == ()
    finally:
        attachment_fs._TEST_FAULT_HOOK = None
        if store is not None: store.close()
        if authority is not None: authority.close()


def test_capture_keeps_opened_archive_bytes_when_source_name_is_replaced(tmp_path, monkeypatch):
    original_payload = b"a" * (2 * 1024 * 1024)
    replacement_payload = b"replacement"
    source = tmp_path / "source.botsarchive"
    replacement = tmp_path / "replacement.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=original_payload)))
    replacement.write_bytes(archive_bytes(_attachment_projection(payload=replacement_payload)))
    expected = source_fingerprint(source)
    real_read = archive_import_core.os.read
    replaced = False

    def replace_after_first_read(fd, size):
        nonlocal replaced
        block = real_read(fd, size)
        if block and not replaced:
            replaced = True
            os.replace(replacement, source)
        return block

    monkeypatch.setattr(archive_import_core.os, "read", replace_after_first_read)
    with pytest.raises(archive_import_core.ImportErrorCode, match="SOURCE_CHANGED"):
        capture_validated_archive(source, expected)
    assert replaced is True


def test_external_resolver_heals_reserved_reference_before_graph_commit(tmp_path):
    resolver = tmp_path / "resolver"; resolver.mkdir()
    payload = b"external archive payload"
    digest = hashlib.sha256(payload).hexdigest()
    source = resolver / "external.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    (resolver / "payload.bin").write_bytes(payload)
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = None
    try:
        store = authority.open_store(); now = datetime(2026, 9, 19, tzinfo=UTC)
        queued = store.enqueue_archive_import(source, resolver_roots=(resolver,), now=now, queue_id="external-queue")
        store.execute_archive_import(queued.id, now=now, operation_id="external-operation")
        with store.command_admission():
            with store._engine.connect() as connection:
                ref_id, availability = connection.exec_driver_sql("SELECT id,availability FROM archive_import_attachment_refs").fetchone()
                assert availability == "READY"
                requirement = connection.exec_driver_sql(
                    "SELECT r.binding_kind,r.bound_imported_ref_id,r.bound_native_attachment_id,"
                    "c.imported_ref_id,c.native_attachment_id "
                    "FROM archive_continuation_requirements AS r "
                    "JOIN archive_continuation_requirement_candidates AS c "
                    "ON c.chat_id=r.chat_id AND c.base_key=r.base_key AND c.ordinal=r.ordinal"
                ).fetchone()
                assert requirement == ("identity", ref_id, None, ref_id, None)
        assert store.read_attachment_bytes(str(ref_id)) == payload
    finally:
        if store is not None: store.close()
        else: authority.close()


@pytest.mark.asyncio
async def test_native_chat_reusing_ready_imported_attachment_exports_truthful_v2_hop(tmp_path):
    """Imported provenance belongs to the reused object, not its new chat."""
    intake = tmp_path / "intake"; intake.mkdir()
    payload = b"ready imported object reused by native chat"
    digest = hashlib.sha256(payload).hexdigest()
    source = intake / "source.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    (intake / "payload.bin").write_bytes(payload)
    app, store, authority = _configured_application(tmp_path / "first-root")
    second_authority = second_store = None
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        imported_chat = store.execute_archive_import(queued.id, now=now, operation_id="reuse-import")
        with store.command_admission():
            with store._engine.connect() as db:
                ref_id, source_node = db.exec_driver_sql(
                    "SELECT id,source_node_id FROM archive_import_attachment_refs WHERE operation_id='reuse-import'"
                ).one()
        native_chat = await app.create_chat("native reuse")
        await app.stage_attachment(native_chat.id, ref_id)
        native_attempt = await app.send_message(native_chat.id, "use the retained imported payload")
        await _finish(app, native_attempt.id)
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql(
                    "SELECT count(*) FROM archive_import_chats WHERE chat_id=?", (native_chat.id,)
                ).scalar_one() == 0
        export_source = store.read_chat_export_source(
            native_chat.id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        assert export_source.requires_v2 is True
        with pytest.raises(ArchiveVersionRequired):
            await app.prepare_archive_export(
                native_chat.id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
                archive_version=1,
            )
        projection = await app.prepare_archive_export(
            native_chat.id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        raw = archive_bytes(projection)
        assert validate_archive(io.BytesIO(raw)).archive_id == projection.archive_id
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            provenance = {
                (row["object_kind"], row["object_id"]): row
                for row in parse_jsonl(package.read("domain/object-provenance.jsonl"))
            }
            assert provenance[("attachment", ref_id)]["source"]["kind"] == "imported"
            assert provenance[("attachment", ref_id)]["source"]["immediate"]["object_id"] == "attachment"
            assert provenance[("attachment", ref_id)]["source"]["immediate"]["archive_version"] == 2
            attachment_row = next(
                row for row in parse_jsonl(package.read("domain/attachments.jsonl"))
                if row["source_id"] == ref_id
            )
            assert (attachment_row["filename"], attachment_row["filename_status"],
                    attachment_row["source_kind"], attachment_row["source_kind_status"]) == (
                "attachment.txt", "available", "filesystem", "available",
            )
            assert source_node
        exported = intake / "native-reuse-v2.botsarchive"; exported.write_bytes(raw)
        second_authority = DataRootAuthority(tmp_path / "second-root").acquire()
        second_store = second_authority.open_store()
        requeued = second_store.enqueue_archive_import(exported, resolver_roots=(intake,), now=now)
        reimported = second_store.execute_archive_import(requeued.id, now=now, operation_id="reuse-reimport")
        with second_store.command_admission():
            with second_store._engine.connect() as db:
                reused = db.exec_driver_sql(
                    "SELECT source_node_id FROM archive_import_attachment_refs WHERE operation_id='reuse-reimport'"
                ).scalar_one()
                source_format, prior = db.exec_driver_sql(
                    "SELECT source_format,prior_node_id FROM archive_lineage_nodes WHERE id=?", (reused,)
                ).one()
                # The receiver allocates fresh local lineage-node IDs.  Its
                # v2 node must nevertheless retain the exported prior hop.
                assert source_format == 2 and prior is not None and prior != source_node
                assert db.exec_driver_sql("SELECT count(*) FROM archive_import_chats WHERE chat_id=?", (reimported,)).scalar_one() == 1
        # The reimported attempt is now imported while its selected attachment
        # has an older imported lineage.  Re-export validates that the binding
        # can match the owner's scope at a retained target hop, rather than
        # incorrectly demanding that both objects share an oldest hop.
        second_export = second_store.read_chat_export_source(
            reimported, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        reexported = archive_bytes(build_archive_v2_projection(
            archive_id="native-reuse-third-hop", created_at=now,
            chat=second_export.chat, messages=second_export.messages,
            attempts=second_export.attempts,
            message_attachments=second_export.message_attachments,
            attempt_attachments=second_export.attempt_attachments,
            payloads={}, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
            chat_configuration=second_export.chat_configuration,
            application_version="0.1", migration_revision="0012_phase9_archive_import",
            context_plans=second_export.context_plans,
            object_provenance=second_export.object_provenance,
            continuation_history=second_export.continuation_history,
            history_bindings=second_export.history_bindings,
            archived_attempt_provenance=second_export.archived_attempt_provenance,
        ))
        assert validate_archive(io.BytesIO(reexported)).archive_id == "native-reuse-third-hop"
    finally:
        if second_store is not None:
            second_store.close()
        elif second_authority is not None:
            second_authority.close()
        store.close()


@pytest.mark.asyncio
async def test_missing_external_import_exports_truthful_v2_and_never_fabricates_payload(tmp_path):
    """A reserved missing reference remains missing across an external v2 hop."""
    intake = tmp_path / "intake"; intake.mkdir()
    digest = "b" * 64
    source = intake / "missing-source.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=11))
    app, store, authority = _configured_application(tmp_path / "first-root")
    second_authority = second_store = None
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="missing-export-import")
        with store.command_admission():
            with store._engine.connect() as db:
                ref_id, metadata = db.exec_driver_sql(
                    "SELECT id,source_metadata FROM archive_import_attachment_refs WHERE operation_id='missing-export-import'"
                ).one()
                assert json.loads(metadata)["filename"] == "attachment.txt"
        export_source = store.read_chat_export_source(
            chat_id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        assert export_source.requires_v2 is True
        with pytest.raises(ArchiveVersionRequired):
            await app.prepare_archive_export(
                chat_id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE, archive_version=1,
            )
        with pytest.raises(ExportError, match="authoritative attachment payload is unavailable"):
            await app.prepare_archive_export(chat_id, attachment_policy=AttachmentPolicy.EMBEDDED)
        projection = await app.prepare_archive_export(
            chat_id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        raw = archive_bytes(projection)
        assert validate_archive(io.BytesIO(raw)).archive_id == projection.archive_id
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            attachment = parse_jsonl(package.read("domain/attachments.jsonl"))[0]
            assert attachment["source_id"] == ref_id
            assert attachment["blob_digest"] == digest
            assert attachment["filename"] == "attachment.txt"
            assert attachment["payload_availability"] == "missing-external"
            assert attachment["integrity_status"] == "not-present"
            assert f"payloads/sha256/{digest}" not in package.namelist()
            bindings = list(parse_jsonl(package.read("domain/history-bindings.jsonl")))
            assert any(row["snapshot_source"]["kind"] == "attachment" for row in bindings)
        exported = intake / "missing-external-v2.botsarchive"; exported.write_bytes(raw)
        second_authority = DataRootAuthority(tmp_path / "second-root").acquire()
        second_store = second_authority.open_store()
        requeued = second_store.enqueue_archive_import(exported, resolver_roots=(intake,), now=now)
        reimported = second_store.execute_archive_import(requeued.id, now=now, operation_id="missing-export-reimport")
        with second_store.command_admission():
            with second_store._engine.connect() as db:
                exported_ref, availability, source_metadata, source_node = db.exec_driver_sql(
                    "SELECT id,availability,source_metadata,source_node_id FROM archive_import_attachment_refs "
                    "WHERE operation_id='missing-export-reimport'"
                ).one()
                assert exported_ref != ref_id
                assert availability == "MISSING_EXTERNAL"
                assert json.loads(source_metadata)["filename"] == "attachment.txt"
                source_format, source_object_id = db.exec_driver_sql(
                    "SELECT source_format,source_object_id FROM archive_lineage_nodes WHERE id=?", (source_node,)
                ).one()
                assert (source_format, source_object_id) == (2, ref_id)
                assert db.exec_driver_sql(
                    "SELECT count(*) FROM archive_import_message_attachment_refs im "
                    "JOIN archive_import_messages m ON m.message_id=im.message_id WHERE m.chat_id=?",
                    (reimported,),
                ).scalar_one() == 1
    finally:
        if second_store is not None:
            second_store.close()
        elif second_authority is not None:
            second_authority.close()
        store.close()


@pytest.mark.asyncio
async def test_imported_missing_external_preserves_sealed_redactions_and_relation_ordinals(tmp_path):
    """Neither payload state nor a local placeholder may rewrite source truth."""
    intake = tmp_path / "intake"; intake.mkdir()
    ready_payload = b"first relation is ready"
    ready_digest = hashlib.sha256(ready_payload).hexdigest()
    missing_digest = "c" * 64
    raw = missing_external_archive(
        digest=ready_digest, size=len(ready_payload), selected_slots=2,
        slot_identities=((ready_digest, len(ready_payload)), (missing_digest, 13)),
    )
    def redact_metadata(entries, _manifest):
        rows = list(parse_jsonl(entries["domain/attachments.jsonl"]))
        second = next(row for row in rows if row["source_id"] == "attachment-1")
        second["filename"] = "[redacted]"; second["filename_status"] = "redacted"
        second["source_kind"] = "[redacted]"; second["source_kind_status"] = "redacted"
        entries["domain/attachments.jsonl"] = canonical_jsonl_bytes(rows)
    source = intake / "mixed.botsarchive"
    source.write_bytes(_repack_canonical_archive(raw, redact_metadata))
    (intake / "ready.bin").write_bytes(ready_payload)
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now)
        chat_id = store.execute_archive_import(queued.id, now=now, operation_id="mixed-source")
        with store.command_admission():
            with store._engine.connect() as db:
                refs = db.exec_driver_sql(
                    "SELECT id,source_attachment_id,availability FROM archive_import_attachment_refs "
                    "WHERE operation_id='mixed-source' ORDER BY source_attachment_id"
                ).all()
        ids = {source_id: ref_id for ref_id, source_id, _availability in refs}
        assert [availability for _ref_id, _source_id, availability in refs] == ["READY", "MISSING_EXTERNAL"]
        projection = await app.prepare_archive_export(
            chat_id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        with zipfile.ZipFile(io.BytesIO(archive_bytes(projection))) as package:
            rows = {row["source_id"]: row for row in parse_jsonl(package.read("domain/attachments.jsonl"))}
            assert rows[ids["attachment-1"]]["payload_availability"] == "missing-external"
            assert (rows[ids["attachment-1"]]["filename"], rows[ids["attachment-1"]]["filename_status"],
                    rows[ids["attachment-1"]]["source_kind"], rows[ids["attachment-1"]]["source_kind_status"]) == (
                "[redacted]", "redacted", "[redacted]", "redacted",
            )
            assert rows[ids["attachment-0"]]["payload_availability"] == "verified"
            message_links = list(parse_jsonl(package.read("domain/message-attachments.jsonl")))
            attempt_links = list(parse_jsonl(package.read("domain/attempt-attachments.jsonl")))
            assert [row["attachment_id"] for row in message_links] == [ids["attachment-0"], ids["attachment-1"]]
            assert [row["attachment_id"] for row in attempt_links] == [ids["attachment-0"], ids["attachment-1"]]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_only_preflight_and_drains_cutoff_owner():
    entered_cutoff = asyncio.Event()
    entered_settlement = asyncio.Event()
    release_settlement = asyncio.Event()
    admissions: list[str] = []

    class Store:
        @contextmanager
        def command_admission(self, *, independent=False):
            assert independent is True
            admissions.append("entered")
            try:
                yield
            finally:
                admissions.append("released")

    workers = OwnedImportWorkers(Store())

    async def preflight():
        return "plan"

    async def cutoff(plan):
        assert plan == "plan"
        entered_cutoff.set()
        return "operation"

    async def settle(operation):
        assert operation == "operation"
        entered_settlement.set()
        await release_settlement.wait()
        return "settled"

    task = workers.start(preflight, cutoff, settle)
    await entered_cutoff.wait()
    await entered_settlement.wait()
    closing = asyncio.create_task(workers.shutdown())
    await asyncio.sleep(0)
    assert not closing.done()
    assert admissions == ["entered"]
    release_settlement.set()
    await closing
    assert await task == "settled"
    assert admissions == ["entered", "released"]


@pytest.mark.asyncio
async def test_import_barrier_allows_unrelated_native_generation_and_coherent_exports(
    tmp_path, monkeypatch,
):
    """One authority cut must not leak a partial import into an unrelated chat."""
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "concurrent.botsarchive"; source.write_bytes(_archive())
    app, store, authority = _configured_application(tmp_path / "root")
    entered = threading.Event(); release = threading.Event()
    original = store.cross_archive_import_cutoff

    def gate(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=60)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "cross_archive_import_cutoff", gate)
    try:
        queued = await app.enqueue_archive_import(source)
        assert await asyncio.to_thread(entered.wait, 3)

        # The import is before its first durable effect.  An unrelated native
        # generation must retain its normal authority and complete meanwhile.
        native_chat = await app.create_chat("unrelated native during import")
        native_attempt = await app.send_message(native_chat.id, "unrelated native turn")
        await _finish(app, native_attempt.id)
        with store.command_admission():
            with store._engine.connect() as db:
                assert db.exec_driver_sql("SELECT count(*) FROM chats").scalar_one() == 1
                assert db.exec_driver_sql("SELECT count(*) FROM archive_import_chats").scalar_one() == 0
                assert db.exec_driver_sql("SELECT count(*) FROM archive_import_operations").scalar_one() == 0

        release.set()
        completed_item = None
        for _ in range(1000):
            item = next(item for item in store.list_archive_imports(limit=100).items if item.id == queued.id)
            if item.state is ImportQueueState.COMPLETED:
                completed_item = item
                break
            await asyncio.sleep(0.01)
        else:
            with store.command_admission():
                with store._engine.connect() as db:
                    queue_row = db.exec_driver_sql(
                        "SELECT state,failure_code,operation_id FROM archive_import_queue WHERE id=?", (queued.id,)
                    ).one()
                    operation_row = db.exec_driver_sql(
                        "SELECT state,failure_code FROM archive_import_operations WHERE id=?", (queued.operation_id,)
                    ).one_or_none() if queued.operation_id is not None else None
            raise AssertionError(
                f"barrier-released import did not settle: queue={queue_row}, operation={operation_row}"
            )

        with store.command_admission():
            with store._engine.connect() as db:
                imported_chat = db.exec_driver_sql(
                    "SELECT local_chat_id FROM archive_import_operations WHERE id=?", (completed_item.operation_id,)
                ).scalar_one()
                assert db.exec_driver_sql("SELECT state FROM archive_import_operations WHERE id=?", (completed_item.operation_id,)).scalar_one() == "COMMITTED"
                assert db.exec_driver_sql("SELECT count(*) FROM chats").scalar_one() == 2
                assert db.exec_driver_sql("SELECT count(*) FROM archive_import_chats WHERE chat_id=?", (imported_chat,)).scalar_one() == 1
                assert db.exec_driver_sql("SELECT count(*) FROM archive_import_chats WHERE chat_id=?", (native_chat.id,)).scalar_one() == 0
                assert db.exec_driver_sql("SELECT count(*) FROM generation_attempts WHERE chat_id=?", (imported_chat,)).scalar_one() == 0
                assert db.exec_driver_sql("SELECT chat_id FROM generation_attempts WHERE id=?", (native_attempt.id,)).scalar_one() == native_chat.id

        imported_projection = await app.prepare_archive_export(
            imported_chat, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        )
        native_projection = await app.prepare_archive_export(native_chat.id)
        assert imported_projection.manifest_base["archive_version"] == 2
        assert native_projection.manifest_base["archive_version"] == 1
        assert validate_archive(io.BytesIO(archive_bytes(imported_projection))).archive_id == imported_projection.archive_id
        assert validate_archive(io.BytesIO(archive_bytes(native_projection))).archive_id == native_projection.archive_id
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_healed_imported_attachment_survives_receipt_loss_rebuild_and_navigation_filters(
    tmp_path, monkeypatch,
):
    """Healing, native reuse, receipt loss, rebuild, and navigation are one truth."""
    payload = b"integrated imported consumer needle"
    digest = hashlib.sha256(payload).hexdigest()
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "consumer.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    app, store, authority = _configured_application(tmp_path / "root")
    now = datetime(2026, 9, 20, tzinfo=UTC)

    def imported_result(*, chat_id=None, **filter_kwargs):
        page = store.search(
            "integrated imported consumer needle",
            filters=SearchFilters(
                chat_id=chat_id, document_kinds=(SearchDocumentKind.ATTACHMENT,), **filter_kwargs
            ),
        )
        return next(item for item in page.results if item.document_id == ref_id)

    def location_snapshot(*, chat_id=None, **filter_kwargs):
        result = imported_result(chat_id=chat_id, **filter_kwargs)
        return tuple(
            (item.chat_id, item.message_id, item.branch_state.value, item.archived_at)
            for item in result.locations
        )

    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="consumer")
        imported_chat = store.execute_archive_import(queued.id, now=now, operation_id="consumer-operation")
        with store.command_admission():
            with store._engine.connect() as db:
                ref_id = db.exec_driver_sql(
                    "SELECT id FROM archive_import_attachment_refs WHERE operation_id='consumer-operation'"
                ).scalar_one()

        # Drop the healing receipt, then prove a deterministic rebuild recreates
        # the authoritative imported location without touching business truth.
        receipts = []
        original_accept = store._accept_search_receipt
        monkeypatch.setattr(store, "_accept_search_receipt", receipts.append)
        ordinary = intake / "ordinary.txt"; ordinary.write_bytes(payload)
        store.ingest_attachment(ordinary)
        assert len(receipts) == 1
        monkeypatch.setattr(store, "_accept_search_receipt", original_accept)
        stale = store.search_status()
        assert stale.condition is SearchIndexCondition.STALE
        assert stale.source_revision is not None and stale.checkpoint_revision is not None
        assert stale.source_revision > stale.checkpoint_revision
        store.rebuild_search_index()
        initial = imported_result(chat_id=imported_chat)
        assert initial.locations
        assert all(item.chat_id == imported_chat for item in initial.locations)
        assert all(item.branch_state is SearchBranchState.ACTIVE for item in initial.locations)
        navigation = store.resolve_search_result(initial)
        assert navigation.chat.id == imported_chat

        # Reuse the healed imported identity in an unrelated native chat.  Edit
        # the first user turn so the same attachment has historical and active
        # native locations before that chat is archived.
        native_chat = await app.create_chat("native imported reuse")
        await app.stage_attachment(native_chat.id, ref_id)
        first = await app.send_message(native_chat.id, "first native reuse")
        await _finish(app, first.id)
        await app.stage_attachment(native_chat.id, ref_id)
        second = await app.send_message(native_chat.id, "second native reuse")
        await _finish(app, second.id)
        edited = await app.edit_message(native_chat.id, first.user_message_id, "edited native reuse")
        await _finish(app, edited.id)
        await app.stage_attachment(native_chat.id, ref_id)
        active = await app.send_message(native_chat.id, "active native reuse")
        await _finish(app, active.id)

        global_result = imported_result()
        assert {item.chat_id for item in global_result.locations} == {imported_chat, native_chat.id}
        native_result = imported_result(chat_id=native_chat.id)
        assert {item.branch_state for item in native_result.locations} == {
            SearchBranchState.ACTIVE, SearchBranchState.HISTORICAL,
        }
        for index, location in enumerate(native_result.locations):
            navigation = store.resolve_search_result(native_result, location_index=index)
            assert navigation.chat.id == native_chat.id
            assert navigation.focus_message_id == location.message_id
        active_only = imported_result(chat_id=native_chat.id, active_branch_only=True)
        assert [item.branch_state for item in active_only.locations] == [SearchBranchState.ACTIVE]

        await app.archive_chat(native_chat.id)
        assert store.search(
            "integrated imported consumer needle",
            filters=SearchFilters(
                chat_id=native_chat.id, document_kinds=(SearchDocumentKind.ATTACHMENT,),
            ),
        ).results == ()
        archived = imported_result(chat_id=native_chat.id, include_archived=True)
        assert archived.locations
        assert all(item.archived_at is not None for item in archived.locations)
        assert {item.branch_state for item in archived.locations} == {
            SearchBranchState.ACTIVE, SearchBranchState.HISTORICAL,
        }
        active_archived = imported_result(
            chat_id=native_chat.id, include_archived=True, active_branch_only=True,
        )
        assert [item.branch_state for item in active_archived.locations] == [SearchBranchState.ACTIVE]
        assert all(item.archived_at is not None for item in active_archived.locations)

        before_rebuild = location_snapshot(chat_id=native_chat.id, include_archived=True)
        store.rebuild_search_index()
        assert location_snapshot(chat_id=native_chat.id, include_archived=True) == before_rebuild
        assert imported_result().locations
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_v2_automatic_equivalent_receiver_choice_round_trips_native_generation(tmp_path):
    """T3 receiving-choice serialization, end to end.

    A v2 source override admitted as an automatic equivalent receiver choice
    must authorize exactly one native continuation, keep the emitted v2
    archive's imported source hop and local derivation separate, and re-import
    with the same facts.
    """
    intake = tmp_path / "intake"; intake.mkdir()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    source_app, source_store, _source_authority = _configured_application(tmp_path / "source-root")
    destination_app, destination_store, _destination_authority = _configured_application(tmp_path / "destination-root")
    second_authority = second_store = None
    try:
        source_connection = await source_app.create_provider_connection(
            name="source fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC,
        )
        await source_app.refresh_models(
            source_connection.id, FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
        )
        source_model = source_store.get_model_catalogue_entry_by_provider_id(
            source_connection.id, "fake-v0.1",
        )
        assert source_model is not None
        source_chat = await source_app.create_chat("v2 automatic equivalent")
        source_store.set_chat_model_selection(source_chat.id, source_model.id)
        source_store.set_chat_model_generation_settings(
            source_chat.id, source_model.id, GenerationSettings(temperature=0.5),
        )
        source_attempt = await source_app.send_message(source_chat.id, "archive this")
        await _finish(source_app, source_attempt.id)
        source = intake / "v2-auto-equivalent.botsarchive"
        source.write_bytes(archive_bytes(
            await source_app.prepare_archive_export(source_chat.id, archive_version=2)
        ))
        assert validate_archive(io.BytesIO(source.read_bytes())).archive_id

        queued = destination_store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="v2-auto-equivalent",
        )
        chat_id = destination_store.execute_archive_import(
            queued.id, now=now, operation_id="v2-auto-equivalent-operation",
        )
        imported_head = destination_store.get_chat(chat_id).head_message_id
        assert imported_head is not None
        with destination_store.command_admission():
            with destination_store._engine.connect() as connection:
                anchor_resolution = connection.exec_driver_sql(
                    "SELECT resolution FROM archive_continuation_anchors WHERE chat_id=? AND base_key=?",
                    (chat_id, imported_head),
                ).scalar_one()
                receiver_choices = connection.exec_driver_sql(
                    "SELECT choice_revision,explicit_settings,decision_kind FROM archive_continuation_choices "
                    "WHERE chat_id=? AND base_key=? ORDER BY choice_revision",
                    (chat_id, imported_head),
                ).mappings().all()
        assert anchor_resolution == "EQUIVALENT"
        assert len(receiver_choices) == 1
        assert int(receiver_choices[0]["choice_revision"]) == 1
        assert receiver_choices[0]["decision_kind"] == "equivalent"
        assert json.loads(str(receiver_choices[0]["explicit_settings"]))["temperature"] == 0.5
        providers_before = tuple(destination_store.list_provider_connections())

        continued = await destination_app.regenerate_message(chat_id, imported_head)
        await _finish(destination_app, continued.id)
        snapshot = json.loads(destination_store.get_generation_attempt(continued.id).request_snapshot)
        assert snapshot["effective_settings"]["temperature"] == 0.5
        assert snapshot["settings_provenance"]["temperature"] == "branch"
        assert tuple(destination_store.list_provider_connections()) == providers_before

        exported = archive_bytes(
            await destination_app.prepare_archive_export(chat_id, archive_version=2)
        )
        with zipfile.ZipFile(io.BytesIO(exported)) as package:
            provenance = {
                (row["object_kind"], row["object_id"]): row
                for row in parse_jsonl(package.read("domain/object-provenance.jsonl"))
            }
            history = json.loads(package.read("domain/continuation-history.json"))
            exported_attempt = next(
                row for row in parse_jsonl(package.read("domain/attempts.jsonl"))
                if row["source_id"] == continued.id
            )
        assert provenance[("attempt", continued.id)]["source"]["kind"] == "native"
        assert provenance[("attempt", continued.id)]["derivation"] == {
            "kind": "local-continuation",
            "predecessor": {"object_kind": "message", "object_id": imported_head},
        }
        assert {
            row["object_id"] for row in provenance.values()
            if row["object_kind"] == "message" and row["source"]["kind"] == "imported"
        }
        assert exported_attempt["request_time_provenance"]["settings_provenance"]["temperature"] == "branch"
        assert [branch for branch in history["branches"] if branch["attempt_id"] == continued.id]

        second_path = intake / "v2-auto-equivalent-second-hop.botsarchive"
        second_path.write_bytes(exported)
        second_authority = DataRootAuthority(tmp_path / "second-root").acquire()
        second_store = second_authority.open_store()
        second_queued = second_store.enqueue_archive_import(
            second_path, resolver_roots=(intake,), now=now, queue_id="v2-auto-equivalent-second-hop",
        )
        second_chat = second_store.execute_archive_import(
            second_queued.id, now=now, operation_id="v2-auto-equivalent-second-hop-operation",
        )
        second_head = second_store.get_chat(second_chat).head_message_id
        assert second_head is not None
        assert second_store.import_continuation_readiness(second_chat, second_head) is not None
        with second_store.command_admission():
            with second_store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_imported_attempts WHERE chat_id=?", (second_chat,),
                ).scalar_one() >= 1
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_lineage_nodes WHERE source_format=2"
                ).scalar_one() >= 1
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM archive_lineage_nodes n "
                    "JOIN archive_import_operations o ON o.archive_id=n.archive_id "
                    "JOIN archive_import_chats c ON c.operation_id=o.id "
                    "WHERE c.chat_id=? AND n.prior_node_id IS NOT NULL",
                    (second_chat,),
                ).scalar_one() >= 1
    finally:
        if second_store is not None:
            second_store.close()
        elif second_authority is not None:
            second_authority.close()
        await destination_app.close()
        await source_app.close()
