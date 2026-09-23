from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import sqlite3

import pytest
from sqlalchemy import create_engine, insert, update

from bots5.core.import_history import ObjectProvenance, SourceHop, SourceKind, imported_provenance
from bots5.core.import_queue import ImportQueueState, QueueItem
from bots5.infrastructure.persistence.archive_import_store import JournalIntent, begin_staging, claim, enqueue, recover_known_operation
from bots5.infrastructure.data_root_authority import DataRootAuthority
import bots5.infrastructure.persistence.sqlite as sqlite
from bots5.infrastructure.persistence.schema import attachment_blobs
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase6_blob_transition,
    clear_phase6,
    require_phase6_consumed,
)
from tests.test_phase9_archive_v2 import archive_with_continuation_branch, missing_external_archive
from tests._authority_test_support import upgrade_to


def test_multi_hop_import_retains_each_actual_archive_version_and_time():
    first = imported_provenance(archive_id="v1", archive_version=1, logical_content_digest="a" * 64, source_object_id="source", imported_at="2026-09-19T00:00:00Z", source=None)
    second = imported_provenance(archive_id="v2", archive_version=2, logical_content_digest="b" * 64, source_object_id="middle", imported_at="2026-09-19T00:00:01Z", source=first)
    assert second.source_kind is SourceKind.IMPORTED
    assert second.immediate is not None and second.immediate.archive_version == 2
    assert [(hop.archive_version, hop.imported_at) for hop in second.prior_chain] == [(1, "2026-09-19T00:00:00Z")]


def test_provenance_refuses_duplicate_hops_instead_of_truncating_history():
    hop = SourceHop("v2", 2, "a" * 64, "object", "2026-09-19T00:00:00Z")
    with pytest.raises(ValueError, match="repeats"):
        ObjectProvenance(SourceKind.IMPORTED, hop, (hop,))


def test_pregraph_recovery_is_known_no_commit_and_never_replays(tmp_path):
    database = tmp_path / "state.sqlite3"
    upgrade_to(database, "0012_phase9_archive_import")
    engine = create_engine(f"sqlite:///{database}", future=True)
    try:
        with engine.begin() as connection:
            item = QueueItem("queue", 1, 0, ImportQueueState.QUEUED, (1, 2, 3, 4, 5))
            enqueue(connection, item, source_path="/temporary/archive", resolver_roots=(), options={"import_as_archived": False}, enqueued_at="2026-09-19T00:00:00Z")
            claimed = claim(connection, "queue", 1, now="2026-09-19T00:00:01Z", owner_epoch="epoch")
            begin_staging(connection, "queue", claimed.revision, JournalIntent("operation", "archive", 2, "a" * 64, "source", 1, b"x" * 32, 1, "2026-09-19T00:00:02Z", "2026-09-19T00:00:02Z", b"y" * 32, (), ()))
            assert recover_known_operation(connection, "operation", graph_exists=lambda _chat: False) == "KNOWN_NO_COMMIT"
    finally:
        engine.dispose()


def test_v2_nonempty_history_bindings_reconstruct_after_committed_reopen(tmp_path):
    """Startup reconstruction compares persisted binding evidence without replay."""
    intake = tmp_path / "intake"
    intake.mkdir()
    source = intake / "missing.botsarchive"
    source.write_bytes(missing_external_archive())
    root = tmp_path / "root"
    now = datetime(2026, 9, 20, tzinfo=UTC)
    authority = DataRootAuthority(root).acquire()
    store = authority.open_store()
    try:
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="nonempty-bindings",
        )
        chat_id = store.execute_archive_import(
            queued.id, now=now, operation_id="nonempty-bindings-operation",
        )
        assert store.get_chat(chat_id) is not None
    finally:
        store.close()
    authority = DataRootAuthority(root).acquire()
    reopened = authority.open_store()
    try:
        assert reopened.recover_archive_imports(now=now) == (("nonempty-bindings-operation", "COMMITTED"),)
    finally:
        reopened.close()


def test_reopen_rejects_valid_shaped_sealed_provenance_chain_corruption(tmp_path):
    """D112 compares a sealed prior-hop edge beyond generic row ownership."""
    intake = tmp_path / "intake"
    intake.mkdir()
    source = intake / "missing.botsarchive"
    source.write_bytes(missing_external_archive())
    root = tmp_path / "root"
    now = datetime(2026, 9, 20, tzinfo=UTC)
    authority = DataRootAuthority(root).acquire()
    store = authority.open_store()
    try:
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="sealed-node-corruption",
        )
        store.execute_archive_import(
            queued.id, now=now, operation_id="sealed-node-corruption-operation",
        )
    finally:
        store.close()
        authority.close()

    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='archive_lineage_nodes' ORDER BY name"
        ).fetchall()
        assert triggers and all(definition is not None for _, definition in triggers)
        for name, _ in triggers:
            quoted_name = name.replace('"', '""')
            connection.execute(f'DROP TRIGGER "{quoted_name}"')
        # The assistant terminal node remains a valid assistant node from the
        # same archive.  Only the sealed prior-hop edge is removed, which
        # ordinary source-node ownership checks do not reconstruct.
        connection.execute(
            "UPDATE archive_lineage_nodes SET prior_node_id=NULL WHERE id=("
            "SELECT m.source_node_id FROM archive_import_messages m "
            "WHERE m.source_message_id='assistant')"
        )
        for _, definition in triggers:
            connection.execute(definition)
        assert connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='archive_lineage_nodes' ORDER BY name"
        ).fetchall() == triggers
        connection.commit()

    authority = DataRootAuthority(root).acquire()
    try:
        with pytest.raises(RuntimeError, match="durable Phase 6 attachment storage failed verification") as error:
            authority.open_store()
        # The authority's invalidation cleanup adds its controlled-restart
        # StateError as the immediate cause.  Preserve and inspect the nested
        # reconstruction error rather than accepting the outer startup refusal
        # as proof of this exact sealed-root predicate.
        pending = [error.value]
        seen: set[int] = set()
        messages: list[str] = []
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            messages.append(str(current))
            for linked in (current.__cause__, current.__context__):
                if linked is not None:
                    pending.append(linked)
        assert any("provenance nodes contradict sealed graph" in message for message in messages)
    finally:
        authority.close()


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("operation", "operation contradicts sealed graph"),
        ("choice", "continuation choices contradict sealed graph"),
        ("candidate", "continuation candidates contradict sealed graph"),
    ),
)
def test_reopen_rejects_valid_shaped_sealed_continuation_projection_corruption(
    tmp_path, kind, expected,
):
    """D112 never treats mutable receiver rows as the imported-plan oracle."""
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "source.botsarchive"
    source.write_bytes(
        archive_with_continuation_branch() if kind == "choice" else missing_external_archive()
    )
    root = tmp_path / "root"; now = datetime(2026, 9, 20, tzinfo=UTC)
    authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id=f"sealed-{kind}")
        store.execute_archive_import(queued.id, now=now, operation_id=f"sealed-{kind}-operation")
    finally:
        store.close(); authority.close()

    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        def replace_triggers(table: str, mutation) -> None:
            triggers = connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (table,),
            ).fetchall()
            for name, _ in triggers:
                connection.execute(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
            mutation()
            for _, definition in triggers:
                assert definition is not None
                connection.execute(definition)
            assert connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (table,),
            ).fetchall() == triggers

        if kind == "operation":
            # The receiver hop remains self-consistent with the changed
            # operation.  Only the journal manifest exposes the coordinated
            # lie, so generic node ownership alone cannot catch it.
            connection.execute(
                "UPDATE archive_import_operations SET archive_id='coordinated-lie' "
                "WHERE id='sealed-operation-operation'"
            )
            replace_triggers("archive_lineage_nodes", lambda: connection.execute(
                "UPDATE archive_lineage_nodes SET archive_id='coordinated-lie' "
                "WHERE archive_id='v2-missing' AND source_format=2"
            ))
        elif kind == "choice":
            replace_triggers("archive_continuation_choices", lambda: connection.execute(
                "UPDATE archive_continuation_choices SET explicit_settings="
                "'{\"max_output_tokens\":null,\"reasoning_effort\":null,\"temperature\":1.0,\"timeout_seconds\":null}' "
                "WHERE local_connection_id IS NULL"
            ))
        else:
            # A missing candidate is a valid foreign-key shape but not the
            # complete same-source candidate set sealed at intake.
            connection.execute(
                "DELETE FROM archive_continuation_requirement_candidates WHERE rowid=("
                "SELECT rowid FROM archive_continuation_requirement_candidates LIMIT 1)"
            )
        connection.commit()

    authority = DataRootAuthority(root).acquire()
    try:
        with pytest.raises(RuntimeError, match="durable Phase 6 attachment storage failed verification") as error:
            authority.open_store()
        pending = [error.value]; seen: set[int] = set(); messages: list[str] = []
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current)); messages.append(str(current))
            pending.extend(linked for linked in (current.__cause__, current.__context__) if linked is not None)
        assert any(expected in message for message in messages)
    finally:
        authority.close()


def test_import_publication_uses_presealed_provenance_node_ids(tmp_path, monkeypatch):
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "missing.botsarchive"; source.write_bytes(missing_external_archive())
    authority = DataRootAuthority(tmp_path / "root").acquire(); store = authority.open_store()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="sealed-nodes")
        preflight = store.preflight_archive_import(queued.id, owner_epoch="sealed-nodes-operation")
        monkeypatch.setattr(sqlite, "uuid7", lambda: (_ for _ in ()).throw(AssertionError("publication allocated UUID")))
        cutoff = store.cross_archive_import_cutoff(preflight, now=now, operation_id="sealed-nodes-operation")
        assert store.settle_archive_import(cutoff) is not None
    finally:
        store.close(); authority.close()


def test_startup_heals_indexed_ready_blob_without_resolver_crawl(tmp_path, monkeypatch):
    payload = b"startup-healing-bytes"
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "missing.botsarchive"
    source.write_bytes(missing_external_archive(
        digest=hashlib.sha256(payload).hexdigest(), size=len(payload),
    ))
    ordinary = intake / "ordinary.txt"
    root = tmp_path / "root"; now = datetime(2026, 9, 20, tzinfo=UTC)
    authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    try:
        queued = store.enqueue_archive_import(source, resolver_roots=(intake,), now=now, queue_id="startup-healing")
        store.execute_archive_import(queued.id, now=now, operation_id="startup-healing-operation")
        ordinary.write_bytes(payload)
        monkeypatch.setattr(
            store, "_heal_imported_attachment_references", lambda *args: frozenset()
        )
        store.ingest_attachment(ordinary)
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT availability,attachment_id FROM archive_import_attachment_refs"
                ).one() == ("MISSING_EXTERNAL", None)
    finally:
        store.close()
    reopened = DataRootAuthority(root).acquire().open_store()
    try:
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                ref_id, attachment_id, source_kind = connection.exec_driver_sql(
                    "SELECT r.id,r.attachment_id,a.source_kind FROM archive_import_attachment_refs r "
                    "JOIN attachments a ON a.id=r.id"
                ).one()
                assert ref_id == attachment_id and source_kind == "imported"
    finally:
        reopened.close()


def test_startup_heals_orphan_ready_import_payload_with_verified_classification(tmp_path):
    """A cutoff after CAS publication has no backing attachment to borrow."""
    payload = b"orphan-ready-import-payload"
    digest = hashlib.sha256(payload).digest()
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "missing.botsarchive"
    source.write_bytes(missing_external_archive(digest=digest.hex(), size=len(payload)))
    payload_source = tmp_path / "sealed-payload.bin"; payload_source.write_bytes(payload)
    root = tmp_path / "root"; now = datetime(2026, 9, 20, tzinfo=UTC)
    authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    try:
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="orphan-ready-healing",
        )
        store.execute_archive_import(
            queued.id, now=now, operation_id="orphan-ready-healing-operation",
        )
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT availability,attachment_id FROM archive_import_attachment_refs"
                ).one() == ("MISSING_EXTERNAL", None)
        # This is the import-payload boundary between durable ready CAS and graph
        # publication: it intentionally creates no normal attachment backing.
        with authority.transition():
            captured = store._attachment_manager.capture(payload_source)
            assert captured.digest == digest
            connection = store._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                arm_phase6_blob_transition(
                    connection, digest, "", "staging",
                    operation_id=captured.operation_id,
                    stage_name=captured.operation_id, byte_size=len(payload),
                )
                try:
                    connection.execute(insert(attachment_blobs).values(
                        digest=digest, byte_size=len(payload), state="staging",
                        operation_id=captured.operation_id, stage_name=captured.operation_id,
                        gc_id=None, created_at="2026-09-20T00:00:00.000Z",
                    ))
                    require_phase6_consumed(connection)
                finally:
                    clear_phase6(connection)
                connection.commit()
            finally:
                connection.close()
            store._attachment_manager.capture_to_stage(
                captured.operation_id, digest, len(payload)
            )
            store._attachment_manager.stage_to_object(
                captured.operation_id, digest, len(payload)
            )
            store._attachment_manager.prove_staging_publication(
                captured.operation_id, digest, len(payload)
            )
            connection = store._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                arm_phase6_blob_transition(
                    connection, digest, "staging", "ready", byte_size=len(payload)
                )
                try:
                    result = connection.execute(update(attachment_blobs).where(
                        attachment_blobs.c.digest == digest
                    ).values(state="ready", operation_id=None, stage_name=None, gc_id=None))
                    require_phase6_consumed(connection)
                    assert result.rowcount == 1
                finally:
                    clear_phase6(connection)
                connection.commit()
            finally:
                connection.close()
        with store.command_admission():
            with store._engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM attachments WHERE blob_digest=?", (digest,)
                ).scalar_one() == 0
        store.close()
    finally:
        if authority.state.value != "CLOSED":
            authority.close()
    reopened_authority = DataRootAuthority(root).acquire(); reopened = reopened_authority.open_store()
    try:
        with reopened.command_admission():
            with reopened._engine.connect() as connection:
                ref_id, attachment_id, source_kind, representation_id, reason = connection.exec_driver_sql(
                    "SELECT r.id,r.attachment_id,a.source_kind,a.text_representation_id,a.ineligibility_reason "
                    "FROM archive_import_attachment_refs r JOIN attachments a ON a.id=r.id"
                ).one()
                assert ref_id == attachment_id
                assert source_kind == "imported"
                assert bytes(representation_id) == digest
                assert reason is None
    finally:
        reopened.close()
        reopened_authority.close()


def test_verified_intake_receipt_covers_every_healed_ref_and_matches_rebuild(
    tmp_path, monkeypatch
):
    """One ordinary capture heals the full exact digest set in one receipt."""
    payload = b"receipt-healing-payload"
    digest = hashlib.sha256(payload).hexdigest()
    intake = tmp_path / "intake"; intake.mkdir()
    source = intake / "missing.botsarchive"
    source.write_bytes(missing_external_archive(
        digest=digest, size=len(payload), selected_slots=2,
    ))
    ordinary = tmp_path / "ordinary.txt"; ordinary.write_bytes(payload)
    root = tmp_path / "root"; now = datetime(2026, 9, 20, tzinfo=UTC)
    authority = DataRootAuthority(root).acquire(); store = authority.open_store()
    try:
        queued = store.enqueue_archive_import(
            source, resolver_roots=(intake,), now=now, queue_id="receipt-healing",
        )
        store.execute_archive_import(
            queued.id, now=now, operation_id="receipt-healing-operation",
        )
        with store.command_admission():
            with store._engine.connect() as connection:
                ref_ids = tuple(str(row[0]) for row in connection.exec_driver_sql(
                    "SELECT id FROM archive_import_attachment_refs ORDER BY id"
                ).fetchall())
        receipts = []
        original_accept = store._accept_search_receipt
        monkeypatch.setattr(store, "_accept_search_receipt", receipts.append)
        ordinary_attachment = store.ingest_attachment(ordinary)
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.document_keys == frozenset({
            f"attachment:{ordinary_attachment.id}",
            *(f"attachment:{ref_id}" for ref_id in ref_ids),
        })
        monkeypatch.setattr(store, "_accept_search_receipt", original_accept)
        original_accept(receipt)
        def visible_location_snapshot():
            page = store.search("attachment-0")
            result = next(
                item for item in page.results
                if item.document_id == ref_ids[0]
            )
            navigation = store.resolve_search_result(result)
            return (
                result.document_key,
                tuple((item.chat_id, item.message_id, item.branch_state.value)
                      for item in result.locations),
                navigation.chat.id,
                tuple(message.id for message in navigation.messages),
            )
        locations_before = visible_location_snapshot()
        with store.command_admission():
            with store._engine.connect() as connection:
                before = connection.exec_driver_sql(
                    "SELECT k.document_kind || ':' || k.document_id,f.title,f.body,f.filename FROM search_document_keys k "
                    "JOIN search_fts f ON f.rowid=k.fts_rowid ORDER BY k.document_kind,k.document_id"
                ).fetchall()
        store.rebuild_search_index()
        locations_after = visible_location_snapshot()
        with store.command_admission():
            with store._engine.connect() as connection:
                after = connection.exec_driver_sql(
                    "SELECT k.document_kind || ':' || k.document_id,f.title,f.body,f.filename FROM search_document_keys k "
                    "JOIN search_fts f ON f.rowid=k.fts_rowid ORDER BY k.document_kind,k.document_id"
                ).fetchall()
        assert locations_after == locations_before
        assert after == before
    finally:
        store.close()
        authority.close()
