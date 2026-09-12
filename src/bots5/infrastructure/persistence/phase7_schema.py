"""Closed additive Phase 7 search schema and bounded validation."""

from __future__ import annotations

import re

from sqlalchemy import text


PHASE7_REVISION = "0010_phase7_search_navigation"
SEARCH_SCHEMA_VERSION = 1
SEARCH_TOKENIZER_VERSION = "unicode61-v1"

TABLE_DDL = (
    """
    CREATE TABLE search_source_state (
      singleton_id INTEGER NOT NULL PRIMARY KEY,
      source_revision INTEGER NOT NULL,
      CONSTRAINT ck_search_source_singleton CHECK (singleton_id = 1),
      CONSTRAINT ck_search_source_revision CHECK (source_revision >= 0)
    )
    """,
    """
    CREATE TABLE search_index_state (
      singleton_id INTEGER NOT NULL PRIMARY KEY,
      condition VARCHAR(16) NOT NULL,
      checkpoint_revision INTEGER NOT NULL,
      generation INTEGER NOT NULL,
      schema_version INTEGER NOT NULL,
      tokenizer_version VARCHAR(64) NOT NULL,
      detail TEXT,
      updated_at VARCHAR(40) NOT NULL,
      CONSTRAINT ck_search_index_singleton CHECK (singleton_id = 1),
      CONSTRAINT ck_search_index_condition
        CHECK (condition IN ('VALID', 'REBUILDING', 'INVALID')),
      CONSTRAINT ck_search_checkpoint_revision CHECK (checkpoint_revision >= 0),
      CONSTRAINT ck_search_generation CHECK (generation >= 0),
      CONSTRAINT ck_search_schema_version CHECK (schema_version = 1),
      CONSTRAINT ck_search_tokenizer_version
        CHECK (tokenizer_version = 'unicode61-v1')
    )
    """,
    """
    CREATE TABLE search_document_keys (
      fts_rowid INTEGER NOT NULL PRIMARY KEY,
      document_kind VARCHAR(16) NOT NULL,
      document_id VARCHAR(64) NOT NULL,
      CONSTRAINT ck_search_document_kind
        CHECK (document_kind IN ('chat', 'message', 'attachment')),
      CONSTRAINT ux_search_document_identity UNIQUE (document_kind, document_id)
    )
    """,
    """
    CREATE VIRTUAL TABLE search_fts USING fts5(
      document_key UNINDEXED,
      title,
      body,
      filename,
      tokenize='unicode61'
    )
    """,
)


def _source_trigger(name: str, event: str, table: str, when: str = "") -> str:
    return f"""
    CREATE TRIGGER {name}
    AFTER {event} ON {table}
    {when}
    BEGIN
      SELECT CASE WHEN bots5_phase7_source_mutation_allowed() <> 1
        THEN RAISE(ABORT, 'Phase 7 search-visible mutation requires core authority') END;
      UPDATE search_source_state
        SET source_revision = source_revision + bots5_phase7_consume_source_revision()
        WHERE singleton_id = 1;
      SELECT CASE WHEN changes() <> 1
        THEN RAISE(ABORT, 'Phase 7 search source singleton is unavailable') END;
    END
    """


TRIGGER_DDL = (
    """
    CREATE TRIGGER phase7_search_source_insert_guard
    BEFORE INSERT ON search_source_state
    BEGIN
      SELECT RAISE(ABORT, 'Phase 7 search source singleton cannot be inserted directly');
    END
    """,
    """
    CREATE TRIGGER phase7_search_source_delete_guard
    BEFORE DELETE ON search_source_state
    BEGIN
      SELECT RAISE(ABORT, 'Phase 7 search source singleton cannot be deleted directly');
    END
    """,
    """
    CREATE TRIGGER phase7_search_source_update_guard
    BEFORE UPDATE ON search_source_state
    BEGIN
      SELECT CASE WHEN bots5_phase7_source_revision_update_allowed(
        OLD.singleton_id, NEW.singleton_id, OLD.source_revision, NEW.source_revision
      ) <> 1
        THEN RAISE(ABORT, 'Phase 7 search source revision transition is unauthorized') END;
    END
    """,
    _source_trigger("phase7_chat_insert_source", "INSERT", "chats"),
    _source_trigger("phase7_chat_delete_source", "DELETE", "chats"),
    _source_trigger(
        "phase7_chat_update_source",
        "UPDATE OF id, title, head_message_id, archived_at, updated_at",
        "chats",
        "WHEN NEW.id IS NOT OLD.id OR NEW.title IS NOT OLD.title "
        "OR NEW.head_message_id IS NOT OLD.head_message_id "
        "OR NEW.archived_at IS NOT OLD.archived_at "
        "OR NEW.updated_at IS NOT OLD.updated_at",
    ),
    _source_trigger("phase7_message_insert_source", "INSERT", "messages"),
    _source_trigger("phase7_message_delete_source", "DELETE", "messages"),
    _source_trigger(
        "phase7_message_update_source",
        "UPDATE OF state, content",
        "messages",
        "WHEN (NEW.state IS NOT OLD.state OR NEW.content IS NOT OLD.content) "
        "AND NOT (OLD.state = 'streaming' AND NEW.state = 'streaming')",
    ),
    _source_trigger(
        "phase7_generation_attempt_insert_source", "INSERT", "generation_attempts"
    ),
    _source_trigger(
        "phase7_generation_attempt_delete_source", "DELETE", "generation_attempts"
    ),
    _source_trigger(
        "phase7_generation_attempt_attribution_source",
        "UPDATE OF backend_id, provider_id, model, connection_id, model_entry_id",
        "generation_attempts",
        "WHEN NEW.backend_id IS NOT OLD.backend_id OR NEW.provider_id IS NOT OLD.provider_id "
        "OR NEW.model IS NOT OLD.model OR NEW.connection_id IS NOT OLD.connection_id "
        "OR NEW.model_entry_id IS NOT OLD.model_entry_id",
    ),
    _source_trigger("phase7_attachment_insert_source", "INSERT", "attachments"),
    _source_trigger("phase7_attachment_delete_source", "DELETE", "attachments"),
    _source_trigger(
        "phase7_attachment_update_source",
        "UPDATE OF filename, source_name, text_representation_id, text_digest, ineligibility_reason",
        "attachments",
        "WHEN NEW.filename IS NOT OLD.filename OR NEW.source_name IS NOT OLD.source_name "
        "OR NEW.text_representation_id IS NOT OLD.text_representation_id "
        "OR NEW.text_digest IS NOT OLD.text_digest "
        "OR NEW.ineligibility_reason IS NOT OLD.ineligibility_reason",
    ),
    _source_trigger(
        "phase7_message_attachment_insert_source", "INSERT", "message_attachments"
    ),
    _source_trigger(
        "phase7_message_attachment_delete_source", "DELETE", "message_attachments"
    ),
    _source_trigger(
        "phase7_message_attachment_update_source",
        "UPDATE OF message_id, attachment_id, ordinal",
        "message_attachments",
        "WHEN NEW.message_id IS NOT OLD.message_id OR NEW.attachment_id IS NOT OLD.attachment_id "
        "OR NEW.ordinal IS NOT OLD.ordinal",
    ),
)

PHASE7_TRIGGER_NAMES = tuple(
    re.search(r"CREATE TRIGGER\s+([A-Za-z0-9_]+)", ddl, re.IGNORECASE).group(1)
    for ddl in TRIGGER_DDL
)


def preflight_fts5_connection(connection) -> None:
    """Exercise FTS5/unicode61 using a disposable connection-local table."""
    connection.exec_driver_sql(
        "CREATE VIRTUAL TABLE temp.bots5_phase7_fts_probe "
        "USING fts5(value, tokenize='unicode61')"
    )
    try:
        connection.exec_driver_sql(
            "INSERT INTO temp.bots5_phase7_fts_probe(value) VALUES (?)", ("Grüße 東京",)
        )
        matched = connection.exec_driver_sql(
            "SELECT count(*) FROM temp.bots5_phase7_fts_probe "
            "WHERE bots5_phase7_fts_probe MATCH ?",
            ('"grüße"',),
        ).scalar_one()
        if int(matched) != 1:
            raise RuntimeError("SQLite FTS5 unicode61 preflight did not match Unicode text")
    finally:
        connection.exec_driver_sql("DROP TABLE temp.bots5_phase7_fts_probe")


def ensure_phase7_schema(connection) -> None:
    """Create Phase 7 objects after ``chats.archived_at`` has been added."""
    for statement in TABLE_DDL:
        connection.execute(text(statement))
    existing = connection.exec_driver_sql(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM chats LIMIT 1) "
        "OR EXISTS (SELECT 1 FROM messages "
        " WHERE role='user' OR (role='assistant' AND state IN "
        " ('complete','incomplete','truncated','failed','aborted')) LIMIT 1) "
        "OR EXISTS (SELECT 1 FROM attachments LIMIT 1) THEN 1 ELSE 0 END"
    ).scalar_one()
    source_revision = 1 if int(existing) else 0
    connection.exec_driver_sql(
        "INSERT INTO search_source_state(singleton_id, source_revision) VALUES (1, ?)",
        (source_revision,),
    )
    connection.exec_driver_sql(
        "INSERT INTO search_index_state "
        "(singleton_id, condition, checkpoint_revision, generation, schema_version, "
        "tokenizer_version, detail, updated_at) "
        "VALUES (1, 'VALID', 0, 0, 1, 'unicode61-v1', NULL, "
        "'1970-01-01T00:00:00.000Z')"
    )
    for statement in TRIGGER_DDL:
        connection.execute(text(statement))


def _columns(connection, table: str) -> dict[str, tuple[object, ...]]:
    return {
        str(row[1]): tuple(row)
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    }


def _normalise_sql(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _canonical_owned_sql(value: object) -> str:
    """Normalize layout only; retain all semantic case and quoted content."""
    return " ".join(str(value or "").split())


def validate_phase7_schema(
    connection,
    *,
    require_fts: bool = True,
    expensive: bool = False,
    allow_missing_index_state: bool = False,
    allow_repairable_index_version: bool = False,
) -> None:
    """Validate bounded current-state facts; scan the index only when requested."""
    chat_columns = _columns(connection, "chats")
    archived = chat_columns.get("archived_at")
    if archived is None or str(archived[2]).upper().replace(" ", "") != "TEXT" or int(archived[3]) != 0:
        raise RuntimeError("current Phase 7 chats.archived_at schema is malformed")

    for statement in TABLE_DDL:
        match = re.search(
            r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+([A-Za-z0-9_]+)",
            statement,
            re.IGNORECASE,
        )
        if match is None:
            raise RuntimeError("internal Phase 7 table contract is malformed")
        name = match.group(1)
        actual_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).scalar_one_or_none()
        if _canonical_owned_sql(actual_sql) != _canonical_owned_sql(statement):
            raise RuntimeError(
                f"current Phase 7 table definition is missing or inert: {name}"
            )

    expected_columns = {
        "search_source_state": {
            "singleton_id": ("INTEGER", 1),
            "source_revision": ("INTEGER", 1),
        },
        "search_index_state": {
            "singleton_id": ("INTEGER", 1),
            "condition": ("VARCHAR(16)", 1),
            "checkpoint_revision": ("INTEGER", 1),
            "generation": ("INTEGER", 1),
            "schema_version": ("INTEGER", 1),
            "tokenizer_version": ("VARCHAR(64)", 1),
            "detail": ("TEXT", 0),
            "updated_at": ("VARCHAR(40)", 1),
        },
        "search_document_keys": {
            "fts_rowid": ("INTEGER", 1),
            "document_kind": ("VARCHAR(16)", 1),
            "document_id": ("VARCHAR(64)", 1),
        },
    }
    for table, expected in expected_columns.items():
        actual = _columns(connection, table)
        if set(actual) != set(expected):
            raise RuntimeError(f"current Phase 7 {table} columns are not authoritative")
        for name, (declared, not_null) in expected.items():
            row = actual[name]
            # SQLite reports an INTEGER PRIMARY KEY as nullable even though it is
            # intrinsically non-null; accept that one representation only.
            actual_not_null = int(row[3])
            if name in {"singleton_id", "fts_rowid"} and int(row[5]) == 1:
                actual_not_null = 1
            if str(row[2]).upper().replace(" ", "") != declared or actual_not_null != not_null:
                raise RuntimeError(f"current Phase 7 {table}.{name} schema is malformed")

    fts_sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='search_fts'"
    ).scalar_one_or_none()
    normalised_fts = _normalise_sql(fts_sql)
    if (
        "createvirtualtablesearch_ftsusingfts5(" not in normalised_fts
        or "document_keyunindexed" not in normalised_fts
        or "tokenize='unicode61'" not in normalised_fts
    ):
        raise RuntimeError("current Phase 7 FTS5 schema is missing or malformed")

    actual_triggers = {
        str(row[0]): _normalise_sql(row[1])
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'phase7_%'"
        ).fetchall()
    }
    if set(actual_triggers) != set(PHASE7_TRIGGER_NAMES):
        raise RuntimeError("current Phase 7 trigger inventory is not authoritative")
    for name, statement in zip(PHASE7_TRIGGER_NAMES, TRIGGER_DDL, strict=True):
        if actual_triggers[name] != _normalise_sql(statement):
            raise RuntimeError(f"current Phase 7 trigger is missing or inert: {name}")

    source = connection.exec_driver_sql(
        "SELECT source_revision FROM search_source_state WHERE singleton_id=1"
    ).fetchall()
    state = connection.exec_driver_sql(
        "SELECT singleton_id, condition, checkpoint_revision, generation, schema_version, "
        "tokenizer_version FROM search_index_state"
    ).fetchall()
    if len(source) != 1 or type(source[0][0]) is not int or int(source[0][0]) < 0:
        raise RuntimeError("current Phase 7 source singleton is malformed")
    if not state and allow_missing_index_state:
        state = []
    elif len(state) != 1 or state[0][0] != 1:
        raise RuntimeError("current Phase 7 index singleton is malformed")
    if state:
        _, condition, checkpoint, generation, schema_version, tokenizer = state[0]
        if (
            condition not in {"VALID", "REBUILDING", "INVALID"}
            or type(checkpoint) is not int
            or int(checkpoint) < 0
            or int(checkpoint) > int(source[0][0])
            or type(generation) is not int
            or int(generation) < 0
            or type(schema_version) is not int
            or type(tokenizer) is not str
            or (
                not allow_repairable_index_version
                and (
                    schema_version != SEARCH_SCHEMA_VERSION
                    or tokenizer != SEARCH_TOKENIZER_VERSION
                )
            )
        ):
            raise RuntimeError("current Phase 7 index singleton values are malformed")

    if require_fts:
        # Preparing this bounded statement proves that the current runtime can
        # load the already-created FTS5 virtual table.
        connection.exec_driver_sql(
            "SELECT rowid FROM search_fts WHERE search_fts MATCH ? LIMIT 0", ('"probe"',)
        ).fetchall()
    if expensive:
        connection.exec_driver_sql(
            "INSERT INTO search_fts(search_fts) VALUES('integrity-check')"
        )
        dangling = connection.exec_driver_sql(
            "SELECT count(*) FROM search_document_keys AS k "
            "LEFT JOIN search_fts AS f ON f.rowid=k.fts_rowid WHERE f.rowid IS NULL"
        ).scalar_one()
        unmapped = connection.exec_driver_sql(
            "SELECT count(*) FROM search_fts AS f LEFT JOIN search_document_keys AS k "
            "ON k.fts_rowid=f.rowid WHERE k.fts_rowid IS NULL"
        ).scalar_one()
        if int(dangling) or int(unmapped):
            raise RuntimeError("current Phase 7 FTS/key mapping is inconsistent")
