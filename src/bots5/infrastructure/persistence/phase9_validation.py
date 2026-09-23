"""Current-schema validation for the additive Archive-import tables."""

from __future__ import annotations


import json

import sqlalchemy as sa

from bots5.core.interchange import canonical_json_bytes
from bots5.domain.clock import parse_utc
from bots5.domain.models import AttemptState, MessageRole, MessageState
from bots5.domain.provider import GenerationSettings
from bots5.core.provider_configuration import _validate_settings


_PHASE = {
    "PREFLIGHT_SEALED": 1, "PAYLOADS_STAGED": 2, "PAYLOADS_PUBLISHED": 3,
    "GRAPH_COMMIT_INTENT": 4, "GRAPH_COMMITTED": 5, "CLEANUP_PENDING": 6,
    "SETTLED": 7, "FAILED": None,
}

_MESSAGE_TERMINAL_STATES = {
    MessageState.COMPLETE.value,
    MessageState.INCOMPLETE.value,
    MessageState.TRUNCATED.value,
    MessageState.FAILED.value,
    MessageState.ABORTED.value,
}
_ATTEMPT_TERMINAL_STATES = {
    AttemptState.COMPLETE.value,
    AttemptState.INCOMPLETE.value,
    AttemptState.FAILED.value,
    AttemptState.ABORTED.value,
}


def _fail(message: str) -> None:
    raise RuntimeError(f"cannot establish conversation integrity boundary: {message}")

def _snapshot_is_consistent(snapshot_text: object, expected: dict[str, str]) -> bool:
    try:
        snapshot = json.loads(snapshot_text)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(snapshot, dict):
        return False
    return all(key not in snapshot or snapshot[key] == value for key, value in expected.items())


def _validate_composed_native_graph(connection) -> None:
    chats = {
        str(row["id"]): dict(row)
        for row in connection.execute(
            sa.text("SELECT id, revision, head_message_id, created_at, updated_at FROM chats")
        ).mappings()
    }
    for chat_id, row in chats.items():
        try:
            if type(row["revision"]) is not int:
                raise ValueError("revision is not an integer")
            revision = int(row["revision"])
            parse_utc(row["created_at"])
            parse_utc(row["updated_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"chat {chat_id} has invalid revision or timestamp")
        if revision < 0:
            _fail(f"chat {chat_id} has negative revision")

    message_rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, parent_id, sequence, role, state, created_at, "
            "lineage_id, revision, supersedes_id FROM messages"
        )
    ).mappings().all()
    messages = {str(row["id"]): dict(row) for row in message_rows}
    for message_id, row in messages.items():
        try:
            if type(row["sequence"]) is not int or type(row["revision"]) is not int:
                raise ValueError("sequence or revision is not an integer")
            sequence = int(row["sequence"])
            revision = int(row["revision"])
            parse_utc(row["created_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"message {message_id} has invalid sequence, revision, or timestamp")
        if sequence < 1 or revision < 1:
            _fail(f"message {message_id} has a nonpositive sequence or revision")
        if row["role"] not in {role.value for role in MessageRole}:
            _fail(f"message {message_id} has invalid role {row['role']}")
        if row["state"] not in {state.value for state in MessageState}:
            _fail(f"message {message_id} has invalid state {row['state']}")
        if not row["lineage_id"]:
            _fail(f"message {message_id} has an empty lineage")
        chat = chats.get(str(row["chat_id"]))
        if chat is None:
            _fail(f"message {message_id} references a missing chat")
        if row["parent_id"] is not None:
            if str(row["parent_id"]) == message_id:
                _fail(f"message {message_id} cannot parent itself")
            parent = messages.get(str(row["parent_id"]))
            if parent is None or parent["chat_id"] != row["chat_id"]:
                _fail(f"message {message_id} has a cross-chat parent")
        if row["supersedes_id"] is None:
            if revision != 1:
                _fail(f"message {message_id} has a root revision other than 1")
        else:
            old = messages.get(str(row["supersedes_id"]))
            if (
                old is None
                or old["chat_id"] != row["chat_id"]
                or old["lineage_id"] != row["lineage_id"]
                or int(old["revision"]) + 1 != revision
                or old["role"] != row["role"]
            ):
                _fail(f"message {message_id} has an inconsistent superseded revision")
        if row["role"] == MessageRole.USER.value and row["state"] not in {
            MessageState.SENDING.value,
            MessageState.SENT.value,
            MessageState.FAILED.value,
            MessageState.ABORTED.value,
        }:
            _fail(f"user message {message_id} has invalid state {row['state']}")
        if row["role"] == MessageRole.ASSISTANT.value and row["state"] not in {
            MessageState.STREAMING.value,
            MessageState.COMPLETE.value,
            MessageState.INCOMPLETE.value,
            MessageState.TRUNCATED.value,
            MessageState.FAILED.value,
            MessageState.ABORTED.value,
        }:
            _fail(f"assistant message {message_id} has invalid state {row['state']}")

    for message_id in messages:
        seen: set[str] = set()
        current_id: str | None = message_id
        while current_id is not None:
            if current_id in seen:
                _fail(f"message lineage contains a parent cycle at {current_id}")
            seen.add(current_id)
            parent_id = messages[current_id]["parent_id"]
            current_id = None if parent_id is None else str(parent_id)

    for chat_id, row in chats.items():
        head_id = row["head_message_id"]
        chat_messages = [message for message in messages.values() if message["chat_id"] == chat_id]
        if head_id is None:
            if row["revision"] != 0:
                _fail(f"chat {chat_id} has a nonzero revision without an active head")
            if chat_messages:
                _fail(f"chat {chat_id} has messages but no active head")
        elif row["revision"] < 1:
            _fail(f"chat {chat_id} has an active head with zero revision")
        elif not chat_messages:
            _fail(f"chat {chat_id} has an active head but no messages")
        if head_id is not None and (
            str(head_id) not in messages or messages[str(head_id)]["chat_id"] != chat_id
        ):
            _fail(f"chat {chat_id} has a cross-chat or missing head")
        if head_id is not None:
            head = messages[str(head_id)]
            if head["role"] != MessageRole.ASSISTANT.value:
                _fail(f"chat {chat_id} has a non-assistant active head")
            if any(message["parent_id"] == head_id for message in messages.values()):
                _fail(f"chat {chat_id} active head has child messages")

    attempt_rows = connection.execute(
        sa.text(
            "SELECT id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
            "state, request_snapshot, started_at, ended_at FROM generation_attempts"
        )
    ).mappings().all()
    attempt_by_assistant: dict[str, dict[str, object]] = {}
    for row in attempt_rows:
        attempt_id = str(row["id"])
        if row["state"] not in {state.value for state in AttemptState}:
            _fail(f"generation attempt {attempt_id} has invalid state {row['state']}")
        try:
            parse_utc(row["started_at"])
            if row["ended_at"] is not None:
                parse_utc(row["ended_at"])
        except (AttributeError, TypeError, ValueError) as exc:
            _fail(f"generation attempt {attempt_id} has an invalid timestamp")
        if row["state"] == AttemptState.RUNNING.value and row["ended_at"] is not None:
            _fail(f"running generation attempt {attempt_id} has an end time")
        if row["state"] in _ATTEMPT_TERMINAL_STATES and row["ended_at"] is None:
            _fail(f"terminal generation attempt {attempt_id} has no end time")
        expected = {
            "attempt_id": attempt_id,
            "chat_id": str(row["chat_id"]),
            "user_message_id": str(row["user_message_id"]),
            "backend_id": str(row["backend_id"]),
            "model": str(row["model"]),
        }
        if not _snapshot_is_consistent(row["request_snapshot"], expected):
            _fail(f"generation attempt {attempt_id} has an invalid request snapshot")
        user = messages.get(str(row["user_message_id"]))
        assistant = messages.get(str(row["assistant_message_id"]))
        if (
            user is None
            or assistant is None
            or user["chat_id"] != row["chat_id"]
            or assistant["chat_id"] != row["chat_id"]
            or user["role"] != MessageRole.USER.value
            or assistant["role"] != MessageRole.ASSISTANT.value
            or assistant["parent_id"] != user["id"]
        ):
            _fail(f"generation attempt {attempt_id} has an invalid user/assistant turn")
        assistant_id = str(row["assistant_message_id"])
        if assistant_id in attempt_by_assistant:
            _fail(f"assistant message {assistant_id} has multiple attempts")
        attempt_by_assistant[assistant_id] = dict(row)
        if row["state"] == AttemptState.RUNNING.value and assistant["state"] != MessageState.STREAMING.value:
            _fail(f"running attempt {attempt_id} does not have a streaming assistant")
        if row["state"] == AttemptState.COMPLETE.value and assistant["state"] != MessageState.COMPLETE.value:
            _fail(f"complete attempt {attempt_id} does not have a complete assistant")
        if row["state"] == AttemptState.INCOMPLETE.value and assistant["state"] not in {
            MessageState.INCOMPLETE.value,
            MessageState.TRUNCATED.value,
        }:
            _fail(f"incomplete attempt {attempt_id} has an invalid assistant state")
        if row["state"] == AttemptState.FAILED.value and assistant["state"] != MessageState.FAILED.value:
            _fail(f"failed attempt {attempt_id} does not have a failed assistant")
        if row["state"] == AttemptState.ABORTED.value and assistant["state"] != MessageState.ABORTED.value:
            _fail(f"aborted attempt {attempt_id} does not have an aborted assistant")

    imported_assistant_ids = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT assistant_message_id FROM archive_imported_attempts"
        ).fetchall()
    }
    for message_id, row in messages.items():
        if (
            row["role"] == MessageRole.ASSISTANT.value
            and message_id not in attempt_by_assistant
            and message_id not in imported_assistant_ids
        ):
            _fail(f"assistant message {message_id} has no generation attempt")


def _validate_continuation_branch_snapshots(connection) -> None:
    """Bind the additive branch settings vocabulary to immutable choice rows.

    Phase 6 validates the closed v3 request shape.  Reopen validation must
    additionally prove that its one additive provenance value was earned by a
    persisted continuation branch and its exact choice, without consulting
    mutable receiving-side settings that may legitimately change later.
    """
    rows = connection.exec_driver_sql(
        "SELECT n.id AS attempt_id,n.chat_id,n.user_message_id,n.assistant_message_id,n.request_snapshot,"
        "b.base_key,b.choice_revision,b.first_message_id,"
        "c.local_connection_id,c.local_model_entry_id,c.explicit_settings "
        "FROM generation_attempts AS n "
        "LEFT JOIN archive_continuation_branches AS b ON b.attempt_id=n.id "
        "LEFT JOIN archive_continuation_choices AS c ON c.chat_id=b.chat_id "
        "AND c.base_key=b.base_key AND c.choice_revision=b.choice_revision"
    ).mappings().all()
    setting_keys = {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}
    for row in rows:
        try:
            snapshot = json.loads(str(row["request_snapshot"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("current Phase 9 native request snapshot is malformed") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError("current Phase 9 native request snapshot is malformed")
        provenance = snapshot.get("settings_provenance")
        has_branch_provenance = isinstance(provenance, dict) and "branch" in provenance.values()
        branch_present = row["base_key"] is not None
        if has_branch_provenance and not branch_present:
            raise RuntimeError("Phase 9 branch settings provenance lacks an admitted branch")
        if not branch_present:
            continue
        if snapshot.get("snapshot_version") != 3 or row["local_connection_id"] is None:
            raise RuntimeError("Phase 9 continuation branch lacks an exact v3 choice")
        if snapshot.get("connection_id") != row["local_connection_id"] or snapshot.get("model_entry_id") != row["local_model_entry_id"]:
            raise RuntimeError("Phase 9 continuation branch target contradicts its choice")
        if row["first_message_id"] not in {row["user_message_id"], row["assistant_message_id"]}:
            raise RuntimeError("Phase 9 continuation branch first message contradicts its attempt")
        try:
            explicit = json.loads(str(row["explicit_settings"]))
            if not isinstance(explicit, dict) or set(explicit) != setting_keys:
                raise ValueError("closed settings fields")
            _validate_settings(GenerationSettings(**explicit))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Phase 9 continuation branch settings are malformed") from exc
        effective = snapshot.get("effective_settings")
        if not isinstance(effective, dict) or not isinstance(provenance, dict):
            raise RuntimeError("Phase 9 continuation branch snapshot settings are malformed")
        for key, value in explicit.items():
            if value is None:
                if provenance.get(key) == "branch":
                    raise RuntimeError("Phase 9 continuation branch claims null override")
            elif effective.get(key) != value or provenance.get(key) != "branch":
                raise RuntimeError("Phase 9 continuation branch settings contradict its choice")


def _validate_object_derivations(connection) -> None:
    """Validate the two closed local derived-from targets after every reopen."""
    rows = connection.exec_driver_sql(
        "SELECT chat_id,object_kind,object_id,predecessor_message_id "
        "FROM archive_object_derivations"
    ).mappings().all()
    message_edges: dict[str, str] = {}
    for row in rows:
        chat_id = str(row["chat_id"])
        kind = str(row["object_kind"])
        object_id = str(row["object_id"])
        predecessor_id = str(row["predecessor_message_id"])
        predecessor = connection.exec_driver_sql(
            "SELECT chat_id,sequence FROM messages WHERE id=?", (predecessor_id,)
        ).mappings().one_or_none()
        if predecessor is None or str(predecessor["chat_id"]) != chat_id:
            raise RuntimeError("Phase 9 derivation predecessor crosses chat ownership")
        if kind == "message":
            target = connection.exec_driver_sql(
                "SELECT chat_id,sequence FROM messages WHERE id=?", (object_id,)
            ).mappings().one_or_none()
            opposite = connection.exec_driver_sql(
                "SELECT 1 FROM generation_attempts WHERE id=? UNION ALL "
                "SELECT 1 FROM archive_imported_attempts WHERE id=?", (object_id, object_id),
            ).first()
            if target is not None:
                message_edges[object_id] = predecessor_id
        elif kind == "attempt":
            target = connection.exec_driver_sql(
                "SELECT m.chat_id,m.sequence FROM ("
                "SELECT assistant_message_id FROM generation_attempts WHERE id=? "
                "UNION ALL SELECT assistant_message_id FROM archive_imported_attempts WHERE id=?"
                ") a JOIN messages m ON m.id=a.assistant_message_id",
                (object_id, object_id),
            ).mappings().all()
            opposite = connection.exec_driver_sql(
                "SELECT 1 FROM messages WHERE id=?", (object_id,),
            ).first()
            if len(target) != 1:
                target = None
            else:
                target = target[0]
        else:
            target = None
            opposite = None
        if (target is None or opposite is not None or str(target["chat_id"]) != chat_id
                or int(predecessor["sequence"]) >= int(target["sequence"])):
            raise RuntimeError("Phase 9 object derivation is contradictory")
    for start in message_edges:
        seen: set[str] = set()
        current = start
        while current in message_edges:
            if current in seen:
                raise RuntimeError("Phase 9 object derivation contains a cycle")
            seen.add(current)
            current = message_edges[current]


def validate_phase9_rows(connection) -> None:
    """Reject cross-table contradictions before a store accepts work.

    This is deliberately current-schema validation: it does not alter the
    frozen migration-0003 native graph validator or pretend imported attempts
    are native request snapshots.
    """
    controls = connection.exec_driver_sql(
        "SELECT singleton, revision, claimed_queue_id, owner_epoch FROM archive_import_queue_control"
    ).fetchall()
    if controls != [(1, 0, None, None)] and not (
        len(controls) == 1 and controls[0][0] == 1 and controls[0][1] >= 0
    ):
        raise RuntimeError("Phase 9 queue control is contradictory")
    # 0012 composes Archive-import evidence with every native graph invariant.
    # Imported terminal assistants are represented by archive_imported_attempts,
    # never fabricated generation_attempt rows; the composed validator preserves
    # that one explicit ownership difference while applying the complete native
    # graph, revision, timestamp, and native-attempt checks.
    _validate_composed_native_graph(connection)
    _validate_continuation_branch_snapshots(connection)
    _validate_object_derivations(connection)
    for phase, sequence in connection.exec_driver_sql(
        "SELECT phase, sequence FROM archive_import_journal"
    ).fetchall():
        expected = _PHASE.get(str(phase))
        if expected is not None and int(sequence) != expected:
            raise RuntimeError("Phase 9 journal phase/sequence is contradictory")
        if expected is None and int(sequence) < 1:
            raise RuntimeError("Phase 9 failed journal has an invalid sequence")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_imported_attempts AS i "
        "JOIN messages AS u ON u.id=i.user_message_id "
        "JOIN messages AS a ON a.id=i.assistant_message_id "
        "WHERE u.chat_id<>i.chat_id OR a.chat_id<>i.chat_id"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported attempt crosses chat ownership")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_chats c "
        "LEFT JOIN archive_import_operations o ON o.id=c.operation_id "
        "LEFT JOIN archive_lineage_nodes n ON n.id=c.source_node_id "
        "WHERE o.id IS NULL OR n.id IS NULL OR n.object_kind<>'chat' "
        "OR n.source_object_id<>c.source_chat_id OR n.archive_id<>o.archive_id "
        "OR n.logical_content_digest<>o.logical_content_digest"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported chat provenance is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_messages i "
        "JOIN archive_import_chats c ON c.chat_id=i.chat_id "
        "LEFT JOIN archive_lineage_nodes n ON n.id=i.source_node_id "
        "LEFT JOIN archive_import_operations o ON o.id=c.operation_id "
        "WHERE n.id IS NULL OR n.object_kind<>'message' OR n.source_object_id<>i.source_message_id "
        "OR n.archive_id<>o.archive_id OR n.logical_content_digest<>o.logical_content_digest"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported message provenance is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_lineages i "
        "JOIN archive_import_chats c ON c.chat_id=i.chat_id "
        "LEFT JOIN archive_lineage_nodes n ON n.id=i.source_node_id "
        "LEFT JOIN archive_import_operations o ON o.id=c.operation_id "
        "WHERE n.id IS NULL OR n.object_kind<>'lineage' OR n.source_object_id<>i.source_lineage_id "
        "OR n.archive_id<>o.archive_id OR n.logical_content_digest<>o.logical_content_digest"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported lineage provenance is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_imported_attempts i "
        "JOIN archive_import_chats c ON c.chat_id=i.chat_id "
        "LEFT JOIN archive_lineage_nodes n ON n.id=i.source_node_id "
        "LEFT JOIN archive_import_operations o ON o.id=c.operation_id "
        "WHERE n.id IS NULL OR n.object_kind<>'attempt' OR n.source_object_id<>i.source_attempt_id "
        "OR n.archive_id<>o.archive_id OR n.logical_content_digest<>o.logical_content_digest"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported attempt provenance is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_continuation_requirements r "
        "LEFT JOIN archive_imported_attempts i ON i.id=r.source_imported_attempt_id "
        "LEFT JOIN generation_attempts n ON n.id=r.source_native_attempt_id "
        "LEFT JOIN archive_import_attachment_refs ir ON ir.id=r.bound_imported_ref_id "
        "LEFT JOIN attachments na ON na.id=r.bound_native_attachment_id "
        "WHERE (r.source_imported_attempt_id IS NULL)=(r.source_native_attempt_id IS NULL) "
        "OR (r.source_imported_attempt_id IS NOT NULL AND (i.id IS NULL OR i.chat_id<>r.chat_id)) "
        "OR (r.source_native_attempt_id IS NOT NULL AND (n.id IS NULL OR n.chat_id<>r.chat_id)) "
        "OR (r.bound_imported_ref_id IS NOT NULL AND ir.id IS NULL) "
        "OR (r.bound_native_attachment_id IS NOT NULL AND na.id IS NULL)"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 continuation requirement ownership is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_continuation_requirement_candidates c "
        "LEFT JOIN archive_continuation_requirements r ON r.chat_id=c.chat_id AND r.base_key=c.base_key AND r.ordinal=c.ordinal "
        "WHERE r.chat_id IS NULL OR (c.native_attachment_id IS NULL)=(c.imported_ref_id IS NULL) "
        "OR c.candidate_attachment_id<>COALESCE(c.native_attachment_id,c.imported_ref_id)"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 continuation candidate ownership is contradictory")
    # Each terminal assistant belongs to exactly one representation.  Native
    # attempts keep their established strict lifecycle; imported attempts are
    # inert evidence and therefore cannot be laundered into native requests.
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM messages AS a "
        "LEFT JOIN generation_attempts AS n ON n.assistant_message_id=a.id "
        "LEFT JOIN archive_imported_attempts AS i ON i.assistant_message_id=a.id "
        "WHERE a.role='assistant' AND a.state<>'streaming' "
        "AND ((n.id IS NULL) = (i.id IS NULL))"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 assistant owner is not exactly native or imported")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_attachment_refs "
        "WHERE (availability='READY' AND (attachment_id IS NULL OR attachment_id<>id OR healed_at IS NULL)) "
        "OR (availability='MISSING_EXTERNAL' AND (attachment_id IS NOT NULL OR healed_at IS NOT NULL))"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 ready imported reference is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM (SELECT expected_digest FROM archive_import_attachment_refs "
        "GROUP BY expected_digest HAVING MIN(expected_size)<>MAX(expected_size))"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported references contradict a digest size identity")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_attachment_refs AS r "
        "LEFT JOIN attachments AS a ON a.id=r.attachment_id "
        "LEFT JOIN attachment_blobs AS b ON b.digest=a.blob_digest "
        "WHERE r.availability='READY' AND (a.id IS NULL OR b.state<>'ready' "
        "OR b.byte_size<>r.expected_size OR a.blob_digest<>r.expected_digest)"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 ready imported reference lacks its verified backing")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_attachment_refs AS r "
        "LEFT JOIN archive_lineage_nodes AS n ON n.id=r.source_node_id "
        "LEFT JOIN archive_import_operations AS o ON o.id=r.operation_id "
        "WHERE n.id IS NULL OR n.object_kind<>'attachment' OR n.source_object_id<>r.source_attachment_id "
        "OR o.id IS NULL OR n.archive_id<>o.archive_id OR n.logical_content_digest<>o.logical_content_digest"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported attachment provenance is contradictory")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_message_attachment_refs AS r "
        "JOIN messages AS m ON m.id=r.message_id "
        "JOIN archive_import_attachment_refs AS a ON a.id=r.attachment_ref_id "
        "JOIN archive_import_operations AS o ON o.id=a.operation_id "
        "LEFT JOIN archive_import_chats AS c ON c.chat_id=m.chat_id AND c.operation_id=o.id "
        "WHERE c.chat_id IS NULL"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported message attachment crosses chat ownership")
    contradictions = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_import_attempt_attachment_refs AS r "
        "JOIN archive_imported_attempts AS i ON i.id=r.attempt_id "
        "JOIN archive_import_attachment_refs AS a ON a.id=r.attachment_ref_id "
        "JOIN archive_import_operations AS o ON o.id=a.operation_id "
        "JOIN archive_import_chats AS c ON c.chat_id=i.chat_id "
        "WHERE c.operation_id<>o.id"
    ).scalar_one()
    if int(contradictions):
        raise RuntimeError("Phase 9 imported attempt attachment crosses chat ownership")
    imported_branch_rows = connection.exec_driver_sql(
        "SELECT b.chat_id,b.first_message_id,b.attempt_id,b.base_key,b.choice_snapshot,"
        "c.source_continuation_history,m.chat_id AS message_chat_id,"
        "a.chat_id AS attempt_chat_id,a.user_message_id,a.assistant_message_id,"
        "a.source_attempt_id,first_source.source_message_id AS first_source_id,"
        "base_source.source_message_id AS base_source_id "
        "FROM archive_imported_branch_choices b "
        "LEFT JOIN archive_import_chats c ON c.chat_id=b.chat_id "
        "LEFT JOIN messages m ON m.id=b.first_message_id "
        "LEFT JOIN archive_imported_attempts a ON a.id=b.attempt_id "
        "LEFT JOIN archive_import_messages first_source ON first_source.message_id=b.first_message_id AND first_source.chat_id=b.chat_id "
        "LEFT JOIN archive_import_messages base_source ON base_source.message_id=b.base_key AND base_source.chat_id=b.chat_id"
    ).mappings().all()
    native_branch_overlap = connection.exec_driver_sql(
        "SELECT count(*) FROM archive_imported_branch_choices i "
        "JOIN archive_continuation_branches n ON n.first_message_id=i.first_message_id OR n.attempt_id=i.attempt_id"
    ).scalar_one()
    if int(native_branch_overlap):
        raise RuntimeError("Phase 9 imported branch choice overlaps native branch history")
    expected_keys = {"anchor_key", "choice_revision", "first_message_id", "attempt_id", "created_at"}
    for row in imported_branch_rows:
        try:
            snapshot_text = str(row["choice_snapshot"])
            snapshot = json.loads(snapshot_text)
            history = json.loads(str(row["source_continuation_history"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Phase 9 imported branch choice is contradictory") from exc
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != expected_keys
            or canonical_json_bytes(snapshot).decode("utf-8").removesuffix("\n") != snapshot_text
            or not isinstance(history, dict)
            or not isinstance(history.get("branches"), list)
            or str(row["message_chat_id"]) != str(row["chat_id"])
            or str(row["attempt_chat_id"]) != str(row["chat_id"])
            or row["first_source_id"] is None
            or (str(row["base_key"]) != "empty" and row["base_source_id"] is None)
            or str(row["first_message_id"]) not in {
                str(row["user_message_id"]), str(row["assistant_message_id"])
            }
            or not isinstance(snapshot["anchor_key"], str)
            or type(snapshot["choice_revision"]) is not int
            or snapshot["choice_revision"] < 1
            or not isinstance(snapshot["first_message_id"], str)
            or not isinstance(snapshot["attempt_id"], str)
            or not isinstance(snapshot["created_at"], str)
        ):
            raise RuntimeError("Phase 9 imported branch choice is contradictory")
        try:
            parse_utc(snapshot["created_at"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Phase 9 imported branch choice has an invalid timestamp") from exc
        expected_anchor = "empty" if str(row["base_key"]) == "empty" else str(row["base_source_id"])
        if (
            snapshot["anchor_key"] != expected_anchor
            or snapshot["first_message_id"] != str(row["first_source_id"])
            or snapshot["attempt_id"] != str(row["source_attempt_id"])
            or snapshot not in history["branches"]
        ):
            raise RuntimeError("Phase 9 imported branch choice is contradictory")
        choice_exists = connection.exec_driver_sql(
            "SELECT 1 FROM archive_continuation_choices WHERE chat_id=? AND base_key=? AND choice_revision=?",
            (row["chat_id"], row["base_key"], snapshot["choice_revision"]),
        ).first()
        if choice_exists is None:
            raise RuntimeError("Phase 9 imported branch choice has no matching local choice")
