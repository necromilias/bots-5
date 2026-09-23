"""Durable queue and journal operations for Archive import.

The caller holds the authority transition and supplies one SQLite connection.
Every consequential transition is compare-and-set; these helpers deliberately
have no retry loop or timeout lease stealing policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bots5.core.import_queue import ImportQueueError, ImportQueueState, QueueItem, transition


class ArchiveImportStoreError(RuntimeError):
    pass


_PHASES = ("PREFLIGHT_SEALED", "PAYLOADS_STAGED", "PAYLOADS_PUBLISHED", "GRAPH_COMMIT_INTENT", "GRAPH_COMMITTED", "CLEANUP_PENDING", "SETTLED")


def _journal_json(value: object) -> object:
    """Operational journal JSON never embeds binary objects or locations."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, tuple):
        return [_journal_json(item) for item in value]
    if isinstance(value, list):
        return [_journal_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _journal_json(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class JournalIntent:
    operation_id: str
    archive_id: str
    archive_version: int
    logical_content_digest: str
    source_chat_id: str
    source_chat_revision: int
    source_content_sha256: bytes
    source_byte_size: int
    captured_at: str
    imported_at: str
    graph_plan_sha256: bytes
    graph_inventory: tuple[dict[str, str], ...]
    payload_plan: tuple[dict[str, object], ...]


def _item(row) -> QueueItem:
    return QueueItem(str(row[0]), int(row[1]), int(row[2]), ImportQueueState(str(row[3])), tuple(int(value) for value in row[4:9]), row[9], row[10])


def queue_item(connection, queue_id: str) -> QueueItem:
    row = connection.exec_driver_sql(
        "SELECT id,queue_revision,ordinal,state,source_device,source_inode,source_size,source_mtime_ns,source_ctime_ns,failure_code,operation_id FROM archive_import_queue WHERE id=?", (queue_id,)
    ).first()
    if row is None:
        raise ArchiveImportStoreError("queue item is absent")
    return _item(row)


def queue_control(connection) -> tuple[int, str | None, str | None]:
    row = connection.exec_driver_sql(
        "SELECT revision,claimed_queue_id,owner_epoch FROM archive_import_queue_control WHERE singleton=1"
    ).first()
    if row is None:
        raise ArchiveImportStoreError("queue control singleton is absent")
    return int(row[0]), row[1], row[2]


def advance_queue_control(
    connection, expected_revision: int, *, claimed_queue_id: str | None,
    owner_epoch: str | None,
) -> int:
    """Advance the one durable queue snapshot/version by an exact CAS."""
    if (claimed_queue_id is None) != (owner_epoch is None):
        raise ArchiveImportStoreError("queue control claim is malformed")
    changed = connection.exec_driver_sql(
        "UPDATE archive_import_queue_control SET revision=?,claimed_queue_id=?,owner_epoch=? "
        "WHERE singleton=1 AND revision=?",
        (expected_revision + 1, claimed_queue_id, owner_epoch, expected_revision),
    ).rowcount
    if changed != 1:
        raise ArchiveImportStoreError("queue control compare-and-set failed")
    return expected_revision + 1


def enqueue(connection, item: QueueItem, *, source_path: str, resolver_roots: tuple[str, ...], options: dict[str, object], enqueued_at: str, retry_of: str | None = None) -> None:
    if item.state is not ImportQueueState.QUEUED or item.operation_id is not None:
        raise ArchiveImportStoreError("new queue item must be waiting")
    connection.exec_driver_sql(
        "INSERT INTO archive_import_queue(id,queue_revision,ordinal,source_path,source_device,source_inode,source_size,source_mtime_ns,source_ctime_ns,resolver_roots,options,state,enqueued_at,started_at,finished_at,operation_id,failure_code,retry_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
        (item.id,item.revision,item.ordinal,source_path,*item.source_fingerprint,json.dumps(resolver_roots,separators=(",",":")),json.dumps(options,sort_keys=True,separators=(",",":")),item.state.value,enqueued_at,retry_of),
    )
    revision, claimed_queue_id, owner_epoch = queue_control(connection)
    advance_queue_control(
        connection, revision, claimed_queue_id=claimed_queue_id, owner_epoch=owner_epoch,
    )


def assert_enqueueable(connection) -> None:
    """Reserve the serialization point without suppressing independent work.

    The queue deliberately retains duplicate source fingerprints as separate
    operator requests.  Pagination, rather than an arbitrary small capacity,
    bounds consumers of the durable queue.
    """
    del connection


def _persist_transition(connection, prior: QueueItem, updated: QueueItem, *, now: str) -> None:
    rowcount = connection.exec_driver_sql(
        "UPDATE archive_import_queue SET queue_revision=?,state=?,started_at=CASE WHEN ?='PREFLIGHTING' THEN ? WHEN ?='QUEUED' THEN NULL ELSE started_at END,finished_at=CASE WHEN ? IN ('COMPLETED','FAILED','CANCELLED') THEN ? WHEN ?='QUEUED' THEN NULL ELSE finished_at END,failure_code=?,operation_id=? WHERE id=? AND queue_revision=?",
        (updated.revision,updated.state.value,updated.state.value,now,updated.state.value,updated.state.value,now,updated.state.value,updated.failure_code,updated.operation_id,updated.id,prior.revision),
    ).rowcount
    if rowcount != 1:
        raise ArchiveImportStoreError("queue compare-and-set failed")


def claim(connection, queue_id: str, expected_revision: int, *, now: str, owner_epoch: str) -> QueueItem:
    prior = queue_item(connection, queue_id)
    updated = transition(prior, expected_revision, ImportQueueState.PREFLIGHTING)
    revision, claimed_queue_id, current_owner_epoch = queue_control(connection)
    if claimed_queue_id is not None:
        raise ArchiveImportStoreError("another import is already owned")
    if current_owner_epoch is not None:
        raise ArchiveImportStoreError("queue control claim is malformed")
    advance_queue_control(
        connection, revision, claimed_queue_id=queue_id, owner_epoch=owner_epoch,
    )
    _persist_transition(connection, prior, updated, now=now)
    return updated


def cancel_waiting(connection, queue_id: str, expected_revision: int, *, now: str, queued_only: bool = False) -> QueueItem:
    """Durably win cancellation before any import journal exists."""
    prior = queue_item(connection, queue_id)
    allowed = {ImportQueueState.QUEUED} if queued_only else {ImportQueueState.QUEUED, ImportQueueState.PREFLIGHTING}
    if prior.state not in allowed:
        raise ArchiveImportStoreError("archive import has already crossed its cancellation cutoff")
    updated = transition(prior, expected_revision, ImportQueueState.CANCELLED)
    _persist_transition(connection, prior, updated, now=now)
    if prior.state is ImportQueueState.PREFLIGHTING:
        revision, claimed_queue_id, owner_epoch = queue_control(connection)
        if claimed_queue_id != queue_id or owner_epoch is None:
            raise ArchiveImportStoreError("archive import preflight claim is absent")
        advance_queue_control(connection, revision, claimed_queue_id=None, owner_epoch=None)
    else:
        revision, claimed_queue_id, owner_epoch = queue_control(connection)
        advance_queue_control(
            connection, revision, claimed_queue_id=claimed_queue_id, owner_epoch=owner_epoch,
        )
    return updated


def settle_preflight_without_journal(
    connection, queue_id: str, expected_revision: int, owner_epoch: str, *,
    target: ImportQueueState, failure_code: str | None, now: str,
) -> QueueItem:
    """Release one owned preflight that did not write an import journal."""
    if target not in {ImportQueueState.QUEUED, ImportQueueState.FAILED}:
        raise ArchiveImportStoreError("preflight may only return waiting or fail")
    prior = queue_item(connection, queue_id)
    if prior.state is not ImportQueueState.PREFLIGHTING:
        raise ArchiveImportStoreError("queue item is not an owned preflight")
    updated = transition(
        prior, expected_revision, target, failure_code=failure_code,
    )
    revision, claimed_queue_id, control_owner_epoch = queue_control(connection)
    if claimed_queue_id != queue_id or control_owner_epoch != owner_epoch:
        raise ArchiveImportStoreError("preflight owner is absent")
    _persist_transition(connection, prior, updated, now=now)
    advance_queue_control(connection, revision, claimed_queue_id=None, owner_epoch=None)
    return updated


def begin_staging(connection, queue_id: str, expected_revision: int, intent: JournalIntent) -> QueueItem:
    """First durable consequence: operation, journal, reservations and cutoff."""
    prior = queue_item(connection, queue_id)
    updated = transition(prior, expected_revision, ImportQueueState.STAGING, operation_id=intent.operation_id)
    if prior.state is not ImportQueueState.PREFLIGHTING:
        raise ArchiveImportStoreError("only preflight can cross the cutoff")
    connection.exec_driver_sql(
        "INSERT INTO archive_import_operations(id,archive_id,archive_version,logical_content_digest,source_chat_id,source_chat_revision,source_content_sha256,source_byte_size,captured_at,imported_at,state,local_chat_id,committed_at,failure_code) VALUES (?,?,?,?,?,?,?,?,?,?, 'STAGING',NULL,NULL,NULL)",
        (intent.operation_id,intent.archive_id,intent.archive_version,intent.logical_content_digest,intent.source_chat_id,intent.source_chat_revision,intent.source_content_sha256,intent.source_byte_size,intent.captured_at,intent.imported_at),
    )
    connection.exec_driver_sql(
        "INSERT INTO archive_import_journal(operation_id,phase,sequence,graph_plan_sha256,graph_inventory,payload_plan,updated_at) VALUES (?,'PREFLIGHT_SEALED',1,?,?,?,?)",
        (intent.operation_id,intent.graph_plan_sha256,json.dumps(_journal_json(intent.graph_inventory),separators=(",",":")),json.dumps(_journal_json(intent.payload_plan),separators=(",",":")),intent.imported_at),
    )
    for payload in intent.payload_plan:
        connection.exec_driver_sql("INSERT INTO archive_import_payload_reservations(operation_id,digest,size,publication_state) VALUES (?,?,?,'PLANNED')", (intent.operation_id,payload["digest"],payload["size"]))
    _persist_transition(connection, prior, updated, now=intent.imported_at)
    return updated


def advance_journal(connection, operation_id: str, phase: str, *, updated_at: str) -> None:
    if phase not in _PHASES:
        raise ArchiveImportStoreError("journal phase is invalid")
    row = connection.exec_driver_sql("SELECT phase,sequence FROM archive_import_journal WHERE operation_id=?", (operation_id,)).first()
    if row is None or str(row[0]) not in _PHASES or _PHASES.index(phase) != _PHASES.index(str(row[0])) + 1:
        raise ArchiveImportStoreError("journal phase is not monotonic")
    changed = connection.exec_driver_sql("UPDATE archive_import_journal SET phase=?,sequence=?,updated_at=? WHERE operation_id=? AND phase=? AND sequence=?", (phase,int(row[1])+1,updated_at,operation_id,row[0],row[1])).rowcount
    if changed != 1:
        raise ArchiveImportStoreError("journal compare-and-set failed")


def fail_known_pregraph(connection, queue_id: str, expected_revision: int, operation_id: str, *, code: str, now: str) -> QueueItem:
    prior = queue_item(connection, queue_id)
    updated = transition(prior, expected_revision, ImportQueueState.FAILED, failure_code=code, operation_id=operation_id)
    if prior.state not in {ImportQueueState.STAGING, ImportQueueState.COMMITTING}:
        raise ArchiveImportStoreError("cannot settle this queue state as known pregraph failure")
    connection.exec_driver_sql("UPDATE archive_import_operations SET state='FAILED',failure_code=? WHERE id=? AND state IN ('STAGING','COMMITTING')", (code,operation_id))
    # A known no-graph recovery is terminal evidence, never a resume hint.
    # FAILED is the only terminal journal phase that may be reached from any
    # pregraph phase, so this deliberately does not use monotonic advance.
    connection.exec_driver_sql(
        "UPDATE archive_import_journal SET phase='FAILED',updated_at=? "
        "WHERE operation_id=? AND phase NOT IN ('GRAPH_COMMITTED','CLEANUP_PENDING','SETTLED')",
        (now, operation_id),
    )
    _persist_transition(connection, prior, updated, now=now)
    revision, claimed_queue_id, owner_epoch = queue_control(connection)
    if claimed_queue_id != queue_id or owner_epoch is None:
        raise ArchiveImportStoreError("queue controller recovery owner is absent")
    advance_queue_control(connection, revision, claimed_queue_id=None, owner_epoch=None)
    return updated


def begin_graph_commit(connection, queue_id: str, expected_revision: int, operation_id: str, *, now: str) -> QueueItem:
    """Persist the exact point after which graph settlement is consequential."""
    prior = queue_item(connection, queue_id)
    updated = transition(prior, expected_revision, ImportQueueState.COMMITTING, operation_id=operation_id)
    if prior.state is not ImportQueueState.STAGING:
        raise ArchiveImportStoreError("only staging may enter graph commit")
    # A plan with no new payloads still records the two completed payload
    # phases.  Recovery never has to guess whether their absence means an
    # interrupted operation or an empty verified payload plan.
    row = connection.exec_driver_sql(
        "SELECT phase FROM archive_import_journal WHERE operation_id=?", (operation_id,)
    ).first()
    if row is None:
        raise ArchiveImportStoreError("operation journal is absent")
    if str(row[0]) == "PREFLIGHT_SEALED":
        unresolved = connection.exec_driver_sql(
            "SELECT count(*) FROM archive_import_payload_reservations "
            "WHERE operation_id=? AND publication_state<>'READY'",
            (operation_id,),
        ).scalar_one()
        if int(unresolved):
            raise ArchiveImportStoreError("payload reservations are not published")
        advance_journal(connection, operation_id, "PAYLOADS_STAGED", updated_at=now)
        advance_journal(connection, operation_id, "PAYLOADS_PUBLISHED", updated_at=now)
    elif str(row[0]) != "PAYLOADS_PUBLISHED":
        raise ArchiveImportStoreError("payload journal is not settled for graph commit")
    advance_journal(connection, operation_id, "GRAPH_COMMIT_INTENT", updated_at=now)
    changed = connection.exec_driver_sql(
        "UPDATE archive_import_operations SET state='COMMITTING' WHERE id=? AND state='STAGING'",
        (operation_id,),
    ).rowcount
    if changed != 1:
        raise ArchiveImportStoreError("operation graph-commit transition failed")
    _persist_transition(connection, prior, updated, now=now)
    return updated


def complete_known_graph(
    connection,
    queue_id: str,
    expected_revision: int,
    operation_id: str,
    local_chat_id: str,
    *,
    now: str,
) -> QueueItem:
    """Make the known graph commit and terminal queue marker one transaction."""
    prior = queue_item(connection, queue_id)
    updated = transition(prior, expected_revision, ImportQueueState.COMPLETED, operation_id=operation_id)
    if prior.state is not ImportQueueState.COMMITTING:
        raise ArchiveImportStoreError("only committing work may complete")
    changed = connection.exec_driver_sql(
        "UPDATE archive_import_operations SET state='COMMITTED',local_chat_id=?,committed_at=?,failure_code=NULL "
        "WHERE id=? AND state='COMMITTING'",
        (local_chat_id, now, operation_id),
    ).rowcount
    if changed != 1:
        raise ArchiveImportStoreError("operation completion transition failed")
    advance_journal(connection, operation_id, "GRAPH_COMMITTED", updated_at=now)
    _persist_transition(connection, prior, updated, now=now)
    revision, claimed_queue_id, owner_epoch = queue_control(connection)
    if claimed_queue_id != queue_id or owner_epoch is None:
        raise ArchiveImportStoreError("queue controller lost its committing owner")
    advance_queue_control(connection, revision, claimed_queue_id=None, owner_epoch=None)
    return updated


def recover_known_operation(connection, operation_id: str, *, graph_exists) -> str:
    """Classify recovered durable facts without retrying an import.

    The callback checks the exact local chat/inventory in the authoritative
    graph transaction.  A contradictory marker is deliberately an error,
    leaving the authority owner to fail closed rather than guess.
    """
    row = connection.exec_driver_sql(
        "SELECT o.state,o.local_chat_id,j.phase FROM archive_import_operations AS o JOIN archive_import_journal AS j ON j.operation_id=o.id WHERE o.id=?", (operation_id,)
    ).first()
    if row is None:
        raise ArchiveImportStoreError("operation recovery evidence is absent")
    state, chat_id, phase = str(row[0]), row[1], str(row[2])
    has_graph = bool(chat_id is not None and graph_exists(str(chat_id)))
    if phase in {"GRAPH_COMMITTED", "CLEANUP_PENDING", "SETTLED"}:
        if state != "COMMITTED" or not has_graph:
            raise ArchiveImportStoreError("committed marker contradicts graph")
        return "COMMITTED"
    if phase in {"PREFLIGHT_SEALED", "PAYLOADS_STAGED", "PAYLOADS_PUBLISHED", "GRAPH_COMMIT_INTENT", "FAILED"}:
        if has_graph:
            raise ArchiveImportStoreError("precommit marker contradicts graph")
        return "KNOWN_NO_COMMIT"
    raise ArchiveImportStoreError("operation recovery state is unknown")
