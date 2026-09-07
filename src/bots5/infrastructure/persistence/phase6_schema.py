"""Additive Phase 6 schema and startup authority checks.

The tables below are the exact additive current-schema extension installed by
the private candidate migration to the Phase 6 head.
"""

from __future__ import annotations

import hashlib
import json
import re
from sqlalchemy import text


DDL = (
    """
    CREATE TABLE IF NOT EXISTS attachment_blobs (
      digest BLOB(32) PRIMARY KEY,
      byte_size INTEGER NOT NULL CHECK (typeof(byte_size) = 'integer' AND byte_size >= 0),
      state VARCHAR(16) NOT NULL CHECK (state IN ('staging', 'ready', 'deleting')),
      operation_id VARCHAR(36),
      stage_name VARCHAR(36),
      gc_id VARCHAR(36),
      created_at VARCHAR(40) NOT NULL,
      CHECK (typeof(digest) = 'blob' AND length(digest) = 32),
      CHECK (
        (state = 'staging' AND typeof(operation_id) = 'text' AND length(operation_id) = 36
          AND stage_name = operation_id AND gc_id IS NULL) OR
        (state = 'ready' AND operation_id IS NULL AND stage_name IS NULL AND gc_id IS NULL) OR
        (state = 'deleting' AND operation_id IS NULL AND stage_name IS NULL
          AND typeof(gc_id) = 'text' AND length(gc_id) = 36)
      )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
      id VARCHAR(64) PRIMARY KEY,
      blob_digest BLOB(32) NOT NULL REFERENCES attachment_blobs(digest) ON DELETE RESTRICT,
      filename TEXT NOT NULL,
      source_kind VARCHAR(32) NOT NULL,
      source_name TEXT NOT NULL,
      text_representation_id BLOB(32),
      text_digest BLOB(32),
      ineligibility_reason VARCHAR(64),
      created_at VARCHAR(40) NOT NULL,
      CHECK (typeof(filename) = 'text' AND length(filename) BETWEEN 1 AND 255 AND instr(filename, char(0)) = 0),
      CHECK (source_kind IN ('filesystem', 'imported')),
      CHECK (typeof(source_name) = 'text' AND length(source_name) BETWEEN 1 AND 255 AND instr(source_name, char(0)) = 0 AND instr(source_name, '/') = 0 AND instr(source_name, char(92)) = 0),
      CHECK ((text_representation_id IS NULL AND text_digest IS NULL) OR
             (typeof(text_representation_id) = 'blob' AND length(text_representation_id) = 32 AND
              typeof(text_digest) = 'blob' AND length(text_digest) = 32 AND text_digest = text_representation_id)),
      CHECK (ineligibility_reason IS NULL OR ineligibility_reason IN ('invalid_utf8', 'contains_nul', 'not_text'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message_attachments (
      message_id VARCHAR(64) NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
      attachment_id VARCHAR(64) NOT NULL REFERENCES attachments(id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
      PRIMARY KEY(message_id, attachment_id),
      UNIQUE(message_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempt_attachments (
      attempt_id VARCHAR(64) NOT NULL REFERENCES generation_attempts(id) ON DELETE RESTRICT,
      attachment_id VARCHAR(64) NOT NULL REFERENCES attachments(id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
      PRIMARY KEY(attempt_id, attachment_id),
      UNIQUE(attempt_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS context_plans (
      attempt_id VARCHAR(64) PRIMARY KEY REFERENCES generation_attempts(id) ON DELETE RESTRICT,
      plan_version INTEGER NOT NULL CHECK (plan_version = 3),
      canonical_representation TEXT NOT NULL,
      canonical_digest VARCHAR(64) NOT NULL,
      wire_representation_digest VARCHAR(64) NOT NULL,
      budget_limit INTEGER NOT NULL CHECK (budget_limit > 0),
      budget_provenance TEXT NOT NULL,
      budget_semantics TEXT NOT NULL,
      adapter_id TEXT NOT NULL,
      adapter_version TEXT NOT NULL,
      input_counts TEXT NOT NULL,
      envelope_overhead INTEGER NOT NULL CHECK (envelope_overhead >= 0),
      output_reserve INTEGER NOT NULL CHECK (output_reserve >= 0),
      input_units INTEGER NOT NULL CHECK (input_units >= 0),
      total_units INTEGER NOT NULL CHECK (total_units >= 0),
      headroom INTEGER NOT NULL,
      created_at VARCHAR(40) NOT NULL,
      CHECK (total_units = input_units + envelope_overhead + output_reserve),
      CHECK (headroom = budget_limit - total_units)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_attachments_blob_digest ON attachments(blob_digest)",
    "CREATE INDEX IF NOT EXISTS ix_message_attachments_attachment ON message_attachments(attachment_id)",
    "CREATE INDEX IF NOT EXISTS ix_attempt_attachments_attachment ON attempt_attachments(attachment_id)",
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_blob_immutable
    BEFORE UPDATE OF digest, byte_size, created_at ON attachment_blobs
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attachment blob identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_blob_insert_guard
    BEFORE INSERT ON attachment_blobs
    WHEN typeof(NEW.digest) <> 'blob'
      OR length(NEW.digest) <> 32
      OR typeof(NEW.byte_size) <> 'integer'
      OR NEW.byte_size < 0
      OR NEW.state <> 'staging'
      OR bots5_phase6_blob_transition_allowed(
           NEW.digest, '', NEW.state, NEW.operation_id, NEW.stage_name,
           COALESCE(NEW.gc_id, ''), NEW.byte_size
         ) <> 1
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attachment blob identity is malformed'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_blob_state_guard
    BEFORE UPDATE OF state, operation_id, stage_name, gc_id ON attachment_blobs
    WHEN NOT (
      (OLD.state = 'staging' AND NEW.state = 'ready'
        AND NEW.operation_id IS NULL AND NEW.stage_name IS NULL AND NEW.gc_id IS NULL)
      OR (OLD.state = 'ready' AND NEW.state = 'deleting'
        AND NEW.operation_id IS NULL AND NEW.stage_name IS NULL AND NEW.gc_id IS NOT NULL)
    )
      OR bots5_phase6_blob_transition_allowed(
           OLD.digest, OLD.state, NEW.state, COALESCE(NEW.operation_id, ''),
           COALESCE(NEW.stage_name, ''), COALESCE(NEW.gc_id, ''), NEW.byte_size
         ) <> 1
    BEGIN SELECT RAISE(ABORT, 'Phase 6 blob state transition is unauthorized'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_blob_delete_guard
    BEFORE DELETE ON attachment_blobs
    WHEN OLD.state <> 'deleting'
      OR OLD.gc_id IS NULL
      OR EXISTS (SELECT 1 FROM attachments WHERE blob_digest = OLD.digest)
      OR bots5_phase6_blob_delete_allowed(OLD.digest, OLD.gc_id) <> 1
    BEGIN SELECT RAISE(ABORT, 'Phase 6 referenced attachment blob is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_immutable
    BEFORE UPDATE ON attachments
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attachment identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_delete_guard
    BEFORE DELETE ON attachments
    WHEN EXISTS (SELECT 1 FROM message_attachments WHERE attachment_id = OLD.id)
      OR EXISTS (SELECT 1 FROM attempt_attachments WHERE attachment_id = OLD.id)
      OR bots5_phase6_attachment_delete_allowed(OLD.id) <> 1
    BEGIN SELECT RAISE(ABORT, 'Phase 6 historical attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attachment_insert_guard
    BEFORE INSERT ON attachments
    WHEN NOT EXISTS (SELECT 1 FROM attachment_blobs WHERE digest = NEW.blob_digest AND state = 'ready')
      OR bots5_phase6_attachment_insert_allowed(NEW.id) <> 1
      OR (NEW.text_representation_id IS NOT NULL AND (
        NEW.text_representation_id <> NEW.text_digest
        OR NEW.text_representation_id <> NEW.blob_digest
        OR NEW.ineligibility_reason IS NOT NULL
      ))
      OR (NEW.text_representation_id IS NULL AND NEW.text_digest IS NOT NULL)
      OR (NEW.text_representation_id IS NULL AND NEW.ineligibility_reason IS NULL)
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attachment representation is malformed'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_message_attachment_update_guard
    BEFORE UPDATE ON message_attachments
    BEGIN SELECT RAISE(ABORT, 'Phase 6 message attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_message_attachment_insert_guard
    BEFORE INSERT ON message_attachments
    WHEN NOT EXISTS (
      SELECT 1
      FROM generation_attempts AS a
      JOIN context_plans AS p ON p.attempt_id = a.id
      JOIN attachments AS attachment ON attachment.id = NEW.attachment_id
      JOIN attachment_blobs AS blob
        ON blob.digest = attachment.blob_digest AND blob.state = 'ready'
      JOIN json_each(p.canonical_representation, '$.sources') AS source
      WHERE a.user_message_id = NEW.message_id
        AND json_extract(a.request_snapshot, '$.snapshot_version') = 3
        AND json_extract(source.value, '$.kind') = 'attachment'
        AND json_extract(source.value, '$.source_id') = NEW.attachment_id
        AND json_extract(source.value, '$.selected') = 1
        AND NEW.ordinal = (
          SELECT count(*)
          FROM json_each(p.canonical_representation, '$.sources') AS prior
          WHERE CAST(prior.key AS INTEGER) < CAST(source.key AS INTEGER)
            AND json_extract(prior.value, '$.kind') = 'attachment'
            AND json_extract(prior.value, '$.selected') = 1
        )
    )
    BEGIN SELECT RAISE(ABORT, 'Phase 6 message attachment is absent from the frozen context plan'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_message_attachment_delete_guard
    BEFORE DELETE ON message_attachments
    BEGIN SELECT RAISE(ABORT, 'Phase 6 message attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_message_delete_attachment_guard
    BEFORE DELETE ON messages
    WHEN EXISTS (SELECT 1 FROM message_attachments WHERE message_id = OLD.id)
    BEGIN SELECT RAISE(ABORT, 'Phase 6 parent with historical attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attempt_attachment_update_guard
    BEFORE UPDATE ON attempt_attachments
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attempt attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attempt_attachment_insert_guard
    BEFORE INSERT ON attempt_attachments
    WHEN NOT EXISTS (
      SELECT 1
      FROM generation_attempts AS a
      JOIN context_plans AS p ON p.attempt_id = a.id
      JOIN message_attachments AS m
        ON m.message_id = a.user_message_id
       AND m.attachment_id = NEW.attachment_id
       AND m.ordinal = NEW.ordinal
      JOIN attachments AS attachment ON attachment.id = NEW.attachment_id
      JOIN attachment_blobs AS blob
        ON blob.digest = attachment.blob_digest AND blob.state = 'ready'
      JOIN json_each(p.canonical_representation, '$.sources') AS source
      WHERE a.id = NEW.attempt_id
        AND json_extract(a.request_snapshot, '$.snapshot_version') = 3
        AND json_extract(source.value, '$.kind') = 'attachment'
        AND json_extract(source.value, '$.source_id') = NEW.attachment_id
        AND json_extract(source.value, '$.selected') = 1
        AND NEW.ordinal = (
          SELECT count(*)
          FROM json_each(p.canonical_representation, '$.sources') AS prior
          WHERE CAST(prior.key AS INTEGER) < CAST(source.key AS INTEGER)
            AND json_extract(prior.value, '$.kind') = 'attachment'
            AND json_extract(prior.value, '$.selected') = 1
        )
    )
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attempt attachment is absent from the frozen context plan'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_attempt_attachment_delete_guard
    BEFORE DELETE ON attempt_attachments
    BEGIN SELECT RAISE(ABORT, 'Phase 6 attempt attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_generation_attempt_delete_attachment_guard
    BEFORE DELETE ON generation_attempts
    WHEN EXISTS (SELECT 1 FROM attempt_attachments WHERE attempt_id = OLD.id)
    BEGIN SELECT RAISE(ABORT, 'Phase 6 parent with historical attachment reference is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_context_plan_update_guard
    BEFORE UPDATE ON context_plans
    BEGIN SELECT RAISE(ABORT, 'Phase 6 context plan is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_context_plan_insert_guard
    BEFORE INSERT ON context_plans
    WHEN NOT EXISTS (
      SELECT 1 FROM generation_attempts
      WHERE id = NEW.attempt_id
        AND json_extract(request_snapshot, '$.snapshot_version') = 3
        AND json_extract(request_snapshot, '$.context_plan.canonical_digest') = NEW.canonical_digest
        AND json_extract(request_snapshot, '$.context_plan.wire_representation_sha256') = NEW.wire_representation_digest
        AND json_extract(request_snapshot, '$.context_plan.budget.limit') = NEW.budget_limit
        AND json_extract(request_snapshot, '$.context_plan.budget.provenance') = NEW.budget_provenance
        AND json_extract(request_snapshot, '$.context_plan.budget.semantics') = NEW.budget_semantics
        AND json_extract(request_snapshot, '$.context_plan.budget.adapter_id') = NEW.adapter_id
        AND json_extract(request_snapshot, '$.context_plan.budget.adapter_version') = NEW.adapter_version
        AND NEW.budget_semantics = 'UTF-8 code points in canonical JSON wire envelope'
        AND NEW.adapter_id = 'bots5.deterministic-json'
        AND NEW.adapter_version = '1'
        AND json(json_extract(request_snapshot, '$.context_plan.input_counts')) = json(NEW.input_counts)
        AND json_extract(request_snapshot, '$.context_plan.budget.envelope_overhead') = NEW.envelope_overhead
        AND json_extract(request_snapshot, '$.context_plan.budget.output_reserve') = NEW.output_reserve
        AND json_extract(request_snapshot, '$.context_plan.budget.input_units') = NEW.input_units
        AND json_extract(request_snapshot, '$.context_plan.budget.total_units') = NEW.total_units
        AND json_extract(request_snapshot, '$.context_plan.budget.headroom') = NEW.headroom
        AND NEW.total_units <= NEW.budget_limit
        AND EXISTS (
          SELECT 1
          FROM json_each(request_snapshot, '$.capabilities') AS capability
          WHERE json_extract(capability.value, '$.key') = 'limits.context_tokens'
            AND json_extract(capability.value, '$.state') = 'supported'
            AND typeof(json_extract(capability.value, '$.value')) = 'integer'
            AND json_extract(capability.value, '$.value') > 0
            AND json_extract(capability.value, '$.value') = NEW.budget_limit
            AND NEW.budget_provenance = (
              json_extract(capability.value, '$.source') || ':' ||
              CASE
                WHEN json_extract(capability.value, '$.source_revision') IS NULL THEN 'None'
                ELSE CAST(json_extract(capability.value, '$.source_revision') AS TEXT)
              END || ':' ||
              json_extract(request_snapshot, '$.capability_provenance."limits.context_tokens".field')
            )
        )
      )
    BEGIN SELECT RAISE(ABORT, 'Phase 6 context plan requires a matching v3 attempt'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS phase6_context_plan_delete_guard
    BEFORE DELETE ON context_plans
    BEGIN SELECT RAISE(ABORT, 'Phase 6 context plan is immutable'); END
    """,
)

REQUIRED_TABLES = (
    "attachment_blobs", "attachments", "message_attachments", "attempt_attachments", "context_plans",
)
REQUIRED_TRIGGERS = (
    "phase6_attachment_blob_immutable", "phase6_attachment_blob_delete_guard",
    "phase6_attachment_blob_insert_guard", "phase6_attachment_immutable",
    "phase6_attachment_blob_state_guard",
    "phase6_attachment_insert_guard", "phase6_attachment_delete_guard", "phase6_message_attachment_update_guard",
    "phase6_message_attachment_delete_guard", "phase6_message_attachment_insert_guard",
    "phase6_message_delete_attachment_guard",
    "phase6_attempt_attachment_update_guard", "phase6_attempt_attachment_delete_guard",
    "phase6_attempt_attachment_insert_guard", "phase6_context_plan_update_guard",
    "phase6_generation_attempt_delete_attachment_guard",
    "phase6_context_plan_delete_guard", "phase6_context_plan_insert_guard",
)
REQUIRED_INDEXES = (
    "ix_attachments_blob_digest",
    "ix_message_attachments_attachment",
    "ix_attempt_attachments_attachment",
)


def ensure_phase6_schema(connection) -> None:
    for statement in DDL:
        connection.execute(text(statement))


def validate_phase6_schema(connection, *, destructive: bool = True) -> None:
    for table in REQUIRED_TABLES:
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).first()
        if row is None or not row[0]:
            raise RuntimeError(f"current Phase 6 schema is missing {table}")
    # Marker checks are diagnostic only.  First compare every migration-owned
    # table/trigger against a disposable schema built from this exact DDL.
    canonical = _canonical_phase6_objects()
    names = (*REQUIRED_TABLES, *REQUIRED_TRIGGERS, *REQUIRED_INDEXES)
    actual = {
        str(name): _normalise_sql(str(sql or ""))
        for name, sql in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in names) + ")",
            names,
        ).fetchall()
    }
    inventory = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name LIKE 'phase6_%' OR tbl_name IN ("
        + ",".join("?" for _ in REQUIRED_TABLES)
        + ")",
        REQUIRED_TABLES,
    ).fetchall()
    required_names = {*REQUIRED_TABLES, *REQUIRED_TRIGGERS, *REQUIRED_INDEXES}
    unexpected = sorted(
        str(name)
        for kind, name, table_name, sql in inventory
        if str(name) not in required_names
        and not (
            kind == "index"
            and sql is None
            and str(name).startswith("sqlite_autoindex_")
            and str(table_name) in REQUIRED_TABLES
        )
    )
    if unexpected:
        raise RuntimeError(
            "current Phase 6 schema contains unexpected objects: "
            + ", ".join(unexpected)
        )
    for name, expected_sql in canonical.items():
        if actual.get(name) != expected_sql:
            kind = (
                "trigger" if name in REQUIRED_TRIGGERS
                else "index" if name in REQUIRED_INDEXES
                else "table"
            )
            raise RuntimeError(f"current Phase 6 schema {kind} is missing or inert: {name}")
    expected_trigger_markers = {
        "phase6_attachment_blob_immutable": "before update of digest, byte_size, created_at on attachment_blobs",
        "phase6_attachment_blob_delete_guard": "before delete on attachment_blobs",
        "phase6_attachment_blob_state_guard": "before update of state, operation_id, stage_name, gc_id on attachment_blobs",
        "phase6_attachment_blob_insert_guard": "before insert on attachment_blobs",
        "phase6_attachment_immutable": "before update on attachments",
        "phase6_attachment_delete_guard": "before delete on attachments",
        "phase6_attachment_insert_guard": "before insert on attachments",
        "phase6_message_attachment_update_guard": "before update on message_attachments",
        "phase6_message_attachment_delete_guard": "before delete on message_attachments",
        "phase6_message_attachment_insert_guard": "before insert on message_attachments",
        "phase6_message_delete_attachment_guard": "before delete on messages",
        "phase6_attempt_attachment_update_guard": "before update on attempt_attachments",
        "phase6_attempt_attachment_delete_guard": "before delete on attempt_attachments",
        "phase6_attempt_attachment_insert_guard": "before insert on attempt_attachments",
        "phase6_generation_attempt_delete_attachment_guard": "before delete on generation_attempts",
        "phase6_context_plan_update_guard": "before update on context_plans",
        "phase6_context_plan_delete_guard": "before delete on context_plans",
        "phase6_context_plan_insert_guard": "before insert on context_plans",
    }
    for trigger in REQUIRED_TRIGGERS:
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
        ).first()
        sql = " ".join(str(row[0] or "").casefold().split()) if row else ""
        if (
            not sql
            or "raise(abort" not in sql
            or expected_trigger_markers[trigger] not in sql
            or re.search(r"\bwhen\s+(?:\(?\s*)?(?:0|false)\b", sql)
            or re.search(r"\b(?:and|or)\s+(?:\(?\s*)*(?:0|false)\b", sql)
            or "when 1 = 0" in sql
            or "when 0 = 1" in sql
        ):
            raise RuntimeError(f"current Phase 6 schema trigger is missing or inert: {trigger}")
    _validate_phase6_tables(connection)
    _validate_phase6_rows(connection)
    if destructive:
        _validate_phase6_destructive_guards(connection)


def _normalise_sql(value: str) -> tuple[str, ...]:
    """Tokenise SQLite DDL without changing quoted semantic content.

    SQLite keywords and unquoted identifiers are case-insensitive, and layout
    whitespace between tokens is insignificant.  Quoted strings and quoted
    identifiers are opaque: their delimiter, content, case, whitespace, and
    doubled-quote escapes are retained exactly.  Comments are retained so an
    altered stored definition is not accepted merely because the alteration
    happens to be non-executable text.
    """
    tokens: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        character = value[index]
        if character.isspace():
            index += 1
            continue
        if value.startswith("--", index):
            end = value.find("\n", index + 2)
            if end < 0:
                end = length
            tokens.append("comment:" + value[index:end])
            index = end
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            if end < 0:
                raise RuntimeError("stored Phase 6 SQL has an unterminated comment")
            end += 2
            tokens.append("comment:" + value[index:end])
            index = end
            continue
        if character in {"'", '"', "`"}:
            delimiter = character
            end = index + 1
            while end < length:
                if value[end] != delimiter:
                    end += 1
                    continue
                if end + 1 < length and value[end + 1] == delimiter:
                    end += 2
                    continue
                end += 1
                break
            else:
                raise RuntimeError("stored Phase 6 SQL has unterminated quoted content")
            tokens.append("quoted:" + value[index:end])
            index = end
            continue
        if character == "[":
            end = value.find("]", index + 1)
            if end < 0:
                raise RuntimeError("stored Phase 6 SQL has an unterminated quoted identifier")
            end += 1
            tokens.append("quoted:" + value[index:end])
            index = end
            continue
        if (
            character in {"x", "X"}
            and index + 1 < length
            and value[index + 1] == "'"
        ):
            end = index + 2
            while end < length:
                if value[end] != "'":
                    end += 1
                    continue
                if end + 1 < length and value[end + 1] == "'":
                    end += 2
                    continue
                end += 1
                break
            else:
                raise RuntimeError("stored Phase 6 SQL has an unterminated blob literal")
            tokens.append("blob:" + value[index:end])
            index = end
            continue
        if character.isalnum() or character in {"_", "$"}:
            end = index + 1
            while end < length and (
                value[end].isalnum() or value[end] in {"_", "$"}
            ):
                end += 1
            token = value[index:end].translate(
                str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
            )
            tokens.append("bare:" + token)
            index = end
            continue
        operator = next(
            (
                candidate
                for candidate in ("->>", "!=", "<=", ">=", "==", "<>", "||", "->")
                if value.startswith(candidate, index)
            ),
            character,
        )
        tokens.append("symbol:" + operator)
        index += len(operator)

    # sqlite_schema omits IF NOT EXISTS from stored CREATE statements.  Treat
    # only that exact syntactic clause as insignificant; never erase a token
    # sequence by concatenating raw text.
    kind_index = 1
    if len(tokens) > 1 and tokens[1] == "bare:unique":
        kind_index = 2
    if (
        len(tokens) >= kind_index + 4
        and tokens[0] == "bare:create"
        and tokens[kind_index] in {"bare:table", "bare:trigger", "bare:index"}
        and tokens[kind_index + 1 : kind_index + 4]
        == ["bare:if", "bare:not", "bare:exists"]
    ):
        del tokens[kind_index + 1 : kind_index + 4]
    return tuple(tokens)


def _canonical_phase6_objects() -> dict[str, tuple[str, ...]]:
    """Derive the closed inventory directly from migration DDL, with no DB open."""
    expected: dict[str, str] = {}
    for statement in DDL:
        match = re.search(
            r"\bCREATE\s+(?:UNIQUE\s+)?(TABLE|TRIGGER|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)",
            statement,
            re.IGNORECASE,
        )
        if match and match.group(2) in {
            *REQUIRED_TABLES,
            *REQUIRED_TRIGGERS,
            *REQUIRED_INDEXES,
        }:
            expected[match.group(2)] = _normalise_sql(statement.strip())
    required = {*REQUIRED_TABLES, *REQUIRED_TRIGGERS, *REQUIRED_INDEXES}
    if set(expected) != required:
        raise RuntimeError("internal Phase 6 DDL inventory is incomplete")
    return expected


def _validate_phase6_tables(connection) -> None:
    expected = {
        "attachment_blobs": {
            "digest": ("BLOB(32)", 0), "byte_size": ("INTEGER", 1),
            "state": ("VARCHAR(16)", 1), "operation_id": ("VARCHAR(36)", 0),
            "stage_name": ("VARCHAR(36)", 0), "gc_id": ("VARCHAR(36)", 0),
            "created_at": ("VARCHAR(40)", 1),
        },
        "attachments": {
            "id": ("VARCHAR(64)", 0), "blob_digest": ("BLOB(32)", 1),
            "filename": ("TEXT", 1), "source_kind": ("VARCHAR(32)", 1),
            "source_name": ("TEXT", 1), "text_representation_id": ("BLOB(32)", 0),
            "text_digest": ("BLOB(32)", 0), "ineligibility_reason": ("VARCHAR(64)", 0),
            "created_at": ("VARCHAR(40)", 1),
        },
        "message_attachments": {
            "message_id": ("VARCHAR(64)", 1), "attachment_id": ("VARCHAR(64)", 1),
            "ordinal": ("INTEGER", 1),
        },
        "attempt_attachments": {
            "attempt_id": ("VARCHAR(64)", 1), "attachment_id": ("VARCHAR(64)", 1),
            "ordinal": ("INTEGER", 1),
        },
        "context_plans": {
            "attempt_id": ("VARCHAR(64)", 0), "plan_version": ("INTEGER", 1),
            "canonical_representation": ("TEXT", 1), "canonical_digest": ("VARCHAR(64)", 1),
            "wire_representation_digest": ("VARCHAR(64)", 1), "budget_limit": ("INTEGER", 1),
            "budget_provenance": ("TEXT", 1), "budget_semantics": ("TEXT", 1),
            "adapter_id": ("TEXT", 1), "adapter_version": ("TEXT", 1),
            "input_counts": ("TEXT", 1), "envelope_overhead": ("INTEGER", 1),
            "output_reserve": ("INTEGER", 1), "input_units": ("INTEGER", 1),
            "total_units": ("INTEGER", 1), "headroom": ("INTEGER", 1),
            "created_at": ("VARCHAR(40)", 1),
        },
    }
    for table, columns in expected.items():
        info = {row[1]: row for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
        if set(info) != set(columns):
            raise RuntimeError(f"current Phase 6 {table} columns are not migration-authoritative")
        for name, (declared, not_null) in columns.items():
            actual = str(info[name][2]).upper().replace(" ", "")
            if actual != declared or int(info[name][3]) != not_null:
                raise RuntimeError(f"current Phase 6 {table} column is malformed: {name}")
    pk = tuple(
        row[1]
        for row in sorted(connection.exec_driver_sql("PRAGMA table_info(attachment_blobs)").fetchall(), key=lambda item: int(item[5] or 0))
        if int(row[5] or 0)
    )
    if pk != ("digest",):
        raise RuntimeError("current Phase 6 attachment blob primary key is malformed")
    for table, expected_fk in {
        "attachments": {("blob_digest", "attachment_blobs", "digest", "RESTRICT")},
        "message_attachments": {("message_id", "messages", "id", "RESTRICT"), ("attachment_id", "attachments", "id", "RESTRICT")},
        "attempt_attachments": {("attempt_id", "generation_attempts", "id", "RESTRICT"), ("attachment_id", "attachments", "id", "RESTRICT")},
        "context_plans": {("attempt_id", "generation_attempts", "id", "RESTRICT")},
    }.items():
        actual = {(row[3], row[2], row[4], str(row[6]).upper()) for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list({table})").fetchall()}
        if actual != expected_fk:
                raise RuntimeError(f"current Phase 6 {table} foreign keys are malformed")


def _validate_phase6_destructive_guards(connection) -> None:
    """Exercise the lifecycle guards inside a rolled-back outer savepoint."""
    from .transition_guard import (
        arm_phase6_attachment_delete,
        arm_phase6_attachment_insert,
        arm_phase6_blob_delete,
        arm_phase6_blob_transition,
        clear_phase6,
        require_phase6_consumed,
    )

    savepoint = "bots5_phase6_schema_probe"
    connection.exec_driver_sql(f"SAVEPOINT {savepoint}")
    try:
        digest = next(
            bytes([value]) * 32
            for value in range(256)
            if connection.exec_driver_sql(
                "SELECT 1 FROM attachment_blobs WHERE digest = ?", (bytes([value]) * 32,)
            ).first()
            is None
        )
        operation_id = "00000000-0000-7000-8000-000000000000"
        gc_id = "00000000-0000-7000-8000-000000000001"
        attachment_id = "bots5-phase6-schema-probe"

        def reject(sql: str, parameters: tuple[object, ...]) -> None:
            try:
                connection.exec_driver_sql(sql, parameters)
            except Exception:
                return
            raise RuntimeError("current Phase 6 destructive guard is inert")

        blob_insert = (
            "INSERT INTO attachment_blobs"
            "(digest, byte_size, state, operation_id, stage_name, gc_id, created_at) "
            "VALUES (?, 0, 'staging', ?, ?, NULL, ?)"
        )
        parameters = (digest, operation_id, operation_id, "1970-01-01T00:00:00.000Z")
        reject(blob_insert, parameters)
        arm_phase6_blob_transition(
            connection,
            digest,
            "",
            "staging",
            operation_id=operation_id,
            stage_name=operation_id,
            byte_size=0,
        )
        try:
            connection.exec_driver_sql(blob_insert, parameters)
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)

        reject(
            "UPDATE attachment_blobs SET state='ready', operation_id=NULL, "
            "stage_name=NULL WHERE digest=?",
            (digest,),
        )
        arm_phase6_blob_transition(
            connection, digest, "staging", "ready", byte_size=0
        )
        try:
            connection.exec_driver_sql(
                "UPDATE attachment_blobs SET state='ready', operation_id=NULL, "
                "stage_name=NULL WHERE digest=?",
                (digest,),
            )
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)

        attachment_insert = (
            "INSERT INTO attachments"
            "(id, blob_digest, filename, source_kind, source_name, "
            "text_representation_id, text_digest, ineligibility_reason, created_at) "
            "VALUES (?, ?, 'probe.bin', 'filesystem', 'probe.bin', NULL, NULL, "
            "'not_text', '1970-01-01T00:00:00.000Z')"
        )
        reject(attachment_insert, (attachment_id, digest))
        arm_phase6_attachment_insert(connection, attachment_id)
        try:
            connection.exec_driver_sql(attachment_insert, (attachment_id, digest))
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)

        reject("DELETE FROM attachments WHERE id=?", (attachment_id,))
        arm_phase6_attachment_delete(connection, attachment_id)
        try:
            connection.exec_driver_sql(
                "DELETE FROM attachments WHERE id=?", (attachment_id,)
            )
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)

        reject(
            "UPDATE attachment_blobs SET state='deleting', gc_id=? WHERE digest=?",
            (gc_id, digest),
        )
        arm_phase6_blob_transition(
            connection, digest, "ready", "deleting", gc_id=gc_id, byte_size=0
        )
        try:
            connection.exec_driver_sql(
                "UPDATE attachment_blobs SET state='deleting', gc_id=? WHERE digest=?",
                (gc_id, digest),
            )
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)

        reject("DELETE FROM attachment_blobs WHERE digest=?", (digest,))
        arm_phase6_blob_delete(connection, digest, gc_id)
        try:
            connection.exec_driver_sql(
                "DELETE FROM attachment_blobs WHERE digest=?", (digest,)
            )
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)
    finally:
        connection.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")


def _validate_phase6_rows(connection) -> None:
    digest_re = re.compile(r"^[0-9a-f]{64}$")
    blobs: dict[bytes, int] = {}
    for row in connection.exec_driver_sql(
        "SELECT digest, byte_size, state, operation_id, stage_name, gc_id FROM attachment_blobs"
    ).fetchall():
        digest, size, state, operation_id, stage_name, gc_id = row
        if (
            type(digest) is not bytes
            or len(digest) != 32
            or type(size) is not int
            or size < 0
            or state not in {"staging", "ready", "deleting"}
            or (
                state == "staging"
                and not (
                    isinstance(operation_id, str)
                    and operation_id == stage_name
                    and gc_id is None
                )
            )
            or (
                state == "ready"
                and any(value is not None for value in (operation_id, stage_name, gc_id))
            )
            or (
                state == "deleting"
                and not (
                    operation_id is None
                    and stage_name is None
                    and isinstance(gc_id, str)
                )
            )
        ):
            raise RuntimeError("current Phase 6 attachment blob row is malformed")
        for value in (operation_id, gc_id):
            if value is not None:
                try:
                    parsed = __import__("uuid").UUID(value)
                except ValueError as exc:
                    raise RuntimeError("current Phase 6 operation identity is malformed") from exc
                if parsed.version != 7 or str(parsed) != value:
                    raise RuntimeError("current Phase 6 operation identity is malformed")
        blobs[digest] = int(size)
    attachment_ids: set[str] = set()
    for row in connection.exec_driver_sql(
        "SELECT id, blob_digest, filename, source_kind, source_name, "
        "text_representation_id, text_digest, ineligibility_reason FROM attachments"
    ).fetchall():
        ident, blob_digest, filename, source_kind, source_name, rep_id, rep_digest, reason = row
        if ident in attachment_ids or blob_digest not in blobs or source_kind not in {"filesystem", "imported"}:
            raise RuntimeError("current Phase 6 attachment row is malformed")
        if not isinstance(filename, str) or not 1 <= len(filename) <= 255 or "\x00" in filename:
            raise RuntimeError("current Phase 6 attachment filename is malformed")
        if (
            not isinstance(source_name, str)
            or not 1 <= len(source_name) <= 255
            or "/" in source_name
            or "\\" in source_name
            or "\x00" in source_name
        ):
            raise RuntimeError("current Phase 6 attachment source name is malformed")
        if rep_id is None:
            if rep_digest is not None or reason not in {"invalid_utf8", "contains_nul", "not_text"}:
                raise RuntimeError("current Phase 6 attachment ineligibility is malformed")
        elif (
            type(rep_id) is not bytes
            or len(rep_id) != 32
            or rep_id != blob_digest
            or rep_digest != blob_digest
            or reason is not None
        ):
            raise RuntimeError("current Phase 6 attachment representation is malformed")
        attachment_ids.add(str(ident))

    from .phase6_validation import validate_phase6_snapshot

    attempts = connection.exec_driver_sql(
        "SELECT id, chat_id, user_message_id, backend_id, model, provider_id, request_snapshot "
        "FROM generation_attempts"
    ).fetchall()
    plans = {
        str(row[0]): row
        for row in connection.exec_driver_sql(
            "SELECT attempt_id, plan_version, canonical_representation, canonical_digest, "
            "wire_representation_digest, budget_limit, budget_provenance, budget_semantics, "
            "adapter_id, adapter_version, input_counts, envelope_overhead, output_reserve, "
            "input_units, total_units, headroom "
            "FROM context_plans"
        ).fetchall()
    }
    for attempt_id, chat_id, user_message_id, backend_id, model, provider_id, snapshot_text in attempts:
        try:
            snapshot = json.loads(snapshot_text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("current Phase 6 attempt snapshot is malformed") from exc
        version = snapshot.get("snapshot_version") if isinstance(snapshot, dict) else None
        if version == 3:
            user_content = connection.exec_driver_sql(
                "SELECT content FROM messages WHERE id = ?", (user_message_id,)
            ).scalar_one_or_none()
            try:
                validate_phase6_snapshot(
                    snapshot_text,
                    attempt_id=attempt_id,
                    chat_id=chat_id,
                    user_message_id=user_message_id,
                    backend_id=backend_id,
                    model=model,
                    provider_id=provider_id,
                    user_message_content=user_content,
                )
            except ValueError as exc:
                raise RuntimeError("current Phase 6 request snapshot is malformed") from exc
            if str(attempt_id) not in plans:
                raise RuntimeError("current Phase 6 v3 attempt is missing its context plan")
            plan_row = plans[str(attempt_id)]
            plan_snapshot = snapshot["context_plan"]
            if (
                plan_row[1] != plan_snapshot["version"]
                or plan_row[2] != plan_snapshot["canonical_representation"]
                or plan_row[3] != plan_snapshot["canonical_digest"]
                or plan_row[4] != plan_snapshot["wire_representation_sha256"]
                or plan_row[5] != plan_snapshot["budget"]["limit"]
                or plan_row[6] != plan_snapshot["budget"]["provenance"]
                or plan_row[7] != plan_snapshot["budget"]["semantics"]
                or plan_row[8] != plan_snapshot["budget"]["adapter_id"]
                or plan_row[9] != plan_snapshot["budget"]["adapter_version"]
                or json.loads(plan_row[10]) != plan_snapshot["input_counts"]
                or plan_row[11] != plan_snapshot["budget"]["envelope_overhead"]
                or plan_row[12] != plan_snapshot["budget"]["output_reserve"]
                or plan_row[13] != plan_snapshot["budget"]["input_units"]
                or plan_row[14] != plan_snapshot["budget"]["total_units"]
                or plan_row[15] != plan_snapshot["budget"]["headroom"]
            ):
                raise RuntimeError("current Phase 6 context plan contradicts its v3 attempt")
            # The v3 snapshot and plan are immutable historical evidence.  A
            # later mutable capability override must not invalidate reopening
            # a completed request; pre-start authority is checked separately
            # by _validate_phase6_attempt_authority.
        elif version == 2 and str(attempt_id) in plans:
            raise RuntimeError("current Phase 6 context plan is attached to a v2 attempt")
    for attempt_id, row in plans.items():
        if (
            int(row[1]) != 3
            or not digest_re.fullmatch(str(row[3]))
            or not digest_re.fullmatch(str(row[4]))
            or hashlib.sha256(str(row[2]).encode("utf-8")).hexdigest() != row[3]
        ):
            raise RuntimeError("current Phase 6 context plan row is malformed")
    for table, identity in (("message_attachments", "message_id"), ("attempt_attachments", "attempt_id")):
        duplicate = connection.exec_driver_sql(
            f"SELECT {identity}, ordinal, COUNT(*) FROM {table} "
            f"GROUP BY {identity}, ordinal HAVING COUNT(*) > 1 LIMIT 1"
        ).first()
        if duplicate is not None:
            raise RuntimeError(f"current Phase 6 {table} ordinals are duplicated")
    refs = connection.exec_driver_sql(
        "SELECT m.message_id, m.attachment_id, m.ordinal, a.id "
        "FROM message_attachments m LEFT JOIN generation_attempts a ON a.user_message_id = m.message_id"
    ).fetchall()
    for message_id, attachment_id, ordinal, attempt_id in refs:
        if attempt_id is None:
            raise RuntimeError("current Phase 6 message reference has no generation attempt")
        plan_row = plans.get(str(attempt_id))
        if plan_row is None:
            raise RuntimeError("current Phase 6 message reference has no v3 plan")
        canonical = json.loads(plan_row[2])
        selected = [
            item["source_id"]
            for item in canonical["sources"]
            if item["kind"] == "attachment" and item["selected"]
        ]
        if ordinal < 0 or ordinal >= len(selected) or selected[ordinal] != attachment_id:
            raise RuntimeError("current Phase 6 message attachment reference is not plan-coherent")
    refs = connection.exec_driver_sql(
        "SELECT attempt_id, attachment_id, ordinal FROM attempt_attachments"
    ).fetchall()
    for attempt_id, attachment_id, ordinal in refs:
        plan_row = plans.get(str(attempt_id))
        if plan_row is None:
            raise RuntimeError("current Phase 6 attempt reference has no v3 plan")
        canonical = json.loads(plan_row[2])
        selected = [
            item["source_id"]
            for item in canonical["sources"]
            if item["kind"] == "attachment" and item["selected"]
        ]
        if ordinal < 0 or ordinal >= len(selected) or selected[ordinal] != attachment_id:
            raise RuntimeError("current Phase 6 attempt attachment reference is not plan-coherent")
        if connection.exec_driver_sql(
            "SELECT 1 FROM message_attachments m "
            "JOIN generation_attempts a ON a.user_message_id = m.message_id "
            "WHERE a.id = ? AND m.attachment_id = ? AND m.ordinal = ?",
            (attempt_id, attachment_id, ordinal),
        ).first() is None:
            raise RuntimeError("current Phase 6 attempt reference has no message reference")


def _validate_current_context_capability(connection, *, model_entry_id: object, snapshot: dict[str, object]) -> None:
    """Ensure a persisted v3 plan still names the current resolved limit."""
    if type(model_entry_id) is not str:
        raise RuntimeError("current Phase 6 attempt has no model capability authority")
    catalogue_revision = connection.exec_driver_sql(
        "SELECT p.catalogue_revision "
        "FROM model_catalogue_entries m JOIN provider_connections p ON p.id = m.connection_id "
        "WHERE m.id = ?",
        (model_entry_id,),
    ).scalar_one_or_none()
    if catalogue_revision is None:
        raise RuntimeError("current Phase 6 attempt has no model capability authority")
    override = connection.exec_driver_sql(
        "SELECT state, value, revision, reason FROM capability_overrides "
        "WHERE model_entry_id = ? AND capability_key = 'limits.context_tokens'",
        (model_entry_id,),
    ).first()
    if override is not None:
        actual = {
            "key": "limits.context_tokens",
            "state": str(override[0]),
            "source": "manual",
            "source_revision": int(override[2]),
            "value": override[1],
        }
        actual_provenance = {"source": "manual", "reason": override[3] or "manual override"}
    else:
        facts = []
        for row in connection.exec_driver_sql(
            "SELECT state, source, source_revision, value, provenance_json "
            "FROM capability_facts WHERE model_entry_id = ? AND capability_key = 'limits.context_tokens'",
            (model_entry_id,),
        ).fetchall():
            if (
                row[1] in {"confirmed_endpoint", "provider_metadata"}
                and row[2] is not None
                and int(row[2]) != int(catalogue_revision)
            ):
                continue
            provenance = json.loads(row[4] or "{}")
            precedence = {
                "manual": 0,
                "confirmed_endpoint": 1,
                "provider_metadata": 2,
                "trusted_registry": 3,
                "heuristic": 4,
                "unknown": 5,
            }[str(row[1])]
            facts.append(
                (
                    (precedence, 0 if row[2] is not None else 1, -(int(row[2]) if row[2] is not None else 0),
                     json.dumps(provenance, sort_keys=True, separators=(",", ":")), str(row[0]),
                     row[3] is None, -1 if row[3] is None else int(row[3])),
                    row,
                    provenance,
                )
            )
        if not facts:
            raise RuntimeError("current Phase 6 context capability is missing")
        _sort_key, fact, provenance = min(facts, key=lambda item: item[0])
        actual = {
            "key": "limits.context_tokens",
            "state": str(fact[0]),
            "source": str(fact[1]),
            "source_revision": None if fact[2] is None else int(fact[2]),
            "value": fact[3],
        }
        actual_provenance = {"source": actual["source"], **provenance}
    expected = next(
        (item for item in snapshot.get("capabilities", []) if isinstance(item, dict) and item.get("key") == "limits.context_tokens"),
        None,
    )
    expected_provenance = snapshot.get("capability_provenance", {}).get("limits.context_tokens")
    if expected != actual or expected_provenance != actual_provenance:
        raise RuntimeError("current Phase 6 context capability changed since the request was frozen")


def install_phase6_attempt_guards(connection) -> None:
    """Reinstall the Phase 5 attribution/outcome guards with v3 support.

    The legacy Phase 3 startup validator historically refreshes its own
    triggers on every open.  Reapplying this closed v3-aware pair afterwards
    keeps restart behavior identical to fresh migration behavior.
    """
    connection.execute(text("DROP TRIGGER IF EXISTS phase5_attempt_attribution_insert"))
    connection.execute(text("DROP TRIGGER IF EXISTS phase5_attempt_attribution_update"))
    connection.execute(text("DROP TRIGGER IF EXISTS generation_attempt_phase5_completion_insert"))
    connection.execute(text("DROP TRIGGER IF EXISTS generation_attempt_phase5_completion_update"))
    connection.execute(text("""
        CREATE TRIGGER phase5_attempt_attribution_insert
        BEFORE INSERT ON generation_attempts
        WHEN (json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3) AND (
          NEW.connection_id IS NULL OR NEW.model_entry_id IS NULL
          OR json_extract(NEW.request_snapshot, '$.connection_id') IS NOT NEW.connection_id
          OR json_extract(NEW.request_snapshot, '$.model_entry_id') IS NOT NEW.model_entry_id
          OR NOT EXISTS (SELECT 1 FROM model_catalogue_entries AS m
            JOIN provider_connections AS p ON p.id = m.connection_id
            WHERE m.id = NEW.model_entry_id AND m.connection_id = NEW.connection_id
              AND m.provider_model_id = json_extract(NEW.request_snapshot, '$.model')
              AND p.name = json_extract(NEW.request_snapshot, '$.connection_name')
              AND p.revision = json_extract(NEW.request_snapshot, '$.connection_revision')
              AND p.catalogue_revision = json_extract(NEW.request_snapshot, '$.catalogue_revision')
              AND p.endpoint IS json_extract(NEW.request_snapshot, '$.endpoint')
              AND p.credential_source = json_extract(NEW.request_snapshot, '$.credential_source')
              AND p.credential_reference IS json_extract(NEW.request_snapshot, '$.credential_reference')
              AND p.id = json_extract(NEW.request_snapshot, '$.connection_id'))
        )) OR (COALESCE(json_extract(NEW.request_snapshot, '$.snapshot_version'), 0) NOT IN (2, 3)
          AND (NEW.connection_id IS NOT NULL OR NEW.model_entry_id IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is inconsistent'); END
    """))
    connection.execute(text("""
        CREATE TRIGGER phase5_attempt_attribution_update
        BEFORE UPDATE OF connection_id, model_entry_id ON generation_attempts
        WHEN (json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3)
          AND (NEW.connection_id IS NOT OLD.connection_id OR NEW.model_entry_id IS NOT OLD.model_entry_id))
          OR (COALESCE(json_extract(NEW.request_snapshot, '$.snapshot_version'), 0) NOT IN (2, 3)
          AND (NEW.connection_id IS NOT NULL OR NEW.model_entry_id IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is immutable'); END
    """))
    for event, verb in (("insert", "INSERT"), ("update", "UPDATE OF state, finish_reason, request_snapshot")):
        connection.execute(text(f"""
            CREATE TRIGGER generation_attempt_phase5_completion_{event}
            BEFORE {verb} ON generation_attempts
            WHEN json_extract(NEW.request_snapshot, '$.snapshot_version') IN (2, 3) AND (
              (NEW.state = 'complete' AND (NEW.finish_reason IS NOT 'stop' OR NEW.remote_outcome_unknown IS NOT 0)) OR
              (NEW.state = 'incomplete' AND NEW.finish_reason IS 'stop') OR
              (NEW.state IN ('running', 'failed', 'aborted') AND NEW.finish_reason IS NOT NULL))
            BEGIN SELECT RAISE(ABORT, 'Phase 5 generation finish_reason is inconsistent with state'); END
        """))
    # Refresh the v3 plan guard after the startup schema validator has checked
    # the existing trigger.  This upgrades databases already stamped at the
    # Phase 6 head without making startup silently repair a missing/inert guard.
    connection.execute(text("DROP TRIGGER IF EXISTS phase6_context_plan_insert_guard"))
    connection.execute(text("""
        CREATE TRIGGER phase6_context_plan_insert_guard
        BEFORE INSERT ON context_plans
        WHEN NOT EXISTS (
          SELECT 1 FROM generation_attempts
          WHERE id = NEW.attempt_id
            AND json_extract(request_snapshot, '$.snapshot_version') = 3
            AND json_extract(request_snapshot, '$.context_plan.canonical_digest') = NEW.canonical_digest
            AND json_extract(request_snapshot, '$.context_plan.wire_representation_sha256') = NEW.wire_representation_digest
            AND json_extract(request_snapshot, '$.context_plan.budget.limit') = NEW.budget_limit
            AND json_extract(request_snapshot, '$.context_plan.budget.provenance') = NEW.budget_provenance
            AND json_extract(request_snapshot, '$.context_plan.budget.semantics') = NEW.budget_semantics
            AND json_extract(request_snapshot, '$.context_plan.budget.adapter_id') = NEW.adapter_id
            AND json_extract(request_snapshot, '$.context_plan.budget.adapter_version') = NEW.adapter_version
            AND NEW.budget_semantics = 'UTF-8 code points in canonical JSON wire envelope'
            AND NEW.adapter_id = 'bots5.deterministic-json'
            AND NEW.adapter_version = '1'
            AND json(json_extract(request_snapshot, '$.context_plan.input_counts')) = json(NEW.input_counts)
            AND json_extract(request_snapshot, '$.context_plan.budget.envelope_overhead') = NEW.envelope_overhead
            AND json_extract(request_snapshot, '$.context_plan.budget.output_reserve') = NEW.output_reserve
            AND json_extract(request_snapshot, '$.context_plan.budget.input_units') = NEW.input_units
            AND json_extract(request_snapshot, '$.context_plan.budget.total_units') = NEW.total_units
            AND json_extract(request_snapshot, '$.context_plan.budget.headroom') = NEW.headroom
            AND NEW.total_units <= NEW.budget_limit
            AND EXISTS (
              SELECT 1
              FROM json_each(request_snapshot, '$.capabilities') AS capability
              WHERE json_extract(capability.value, '$.key') = 'limits.context_tokens'
                AND json_extract(capability.value, '$.state') = 'supported'
                AND typeof(json_extract(capability.value, '$.value')) = 'integer'
                AND json_extract(capability.value, '$.value') > 0
                AND json_extract(capability.value, '$.value') = NEW.budget_limit
                AND NEW.budget_provenance = (
                  json_extract(capability.value, '$.source') || ':' ||
                  CASE
                    WHEN json_extract(capability.value, '$.source_revision') IS NULL THEN 'None'
                    ELSE CAST(json_extract(capability.value, '$.source_revision') AS TEXT)
                  END || ':' ||
                  json_extract(request_snapshot, '$.capability_provenance."limits.context_tokens".field')
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'Phase 6 context plan requires a matching v3 attempt'); END
    """))
