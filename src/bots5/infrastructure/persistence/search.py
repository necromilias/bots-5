"""SQLite FTS5 mechanics for Phase 7.

This module owns only derived mechanics.  Authoritative mutation, authority
grants, attachment verification, and exact navigation remain store-owned.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from bots5.core.errors import SearchCursorStale, SearchInvalidQuery
from bots5.domain.models import MessageRole, MessageState
from bots5.domain.search import SearchDocumentKind, SearchFilters


MAX_QUERY_CODEPOINTS = 512
MAX_LIMIT = 100
MAX_CURSOR_BYTES = 4096
MAX_FILTER_ITEMS = 64
MAX_SQLITE_OFFSET = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class SearchReceipt:
    source_revision: int
    document_keys: frozenset[str]

    def __post_init__(self) -> None:
        if self.source_revision < 1:
            raise ValueError("search receipt revision must be positive")
        for key in self.document_keys:
            if not key or ":" not in key:
                raise ValueError("search receipt document key is malformed")


@dataclass(frozen=True, slots=True)
class SearchProjection:
    document_kind: str
    document_id: str
    title: str
    body: str
    filename: str

    @property
    def document_key(self) -> str:
        return f"{self.document_kind}:{self.document_id}"


class ReceiptCoordinator:
    """In-memory optional receipts with contiguous-only acknowledgement."""

    def __init__(self) -> None:
        self._receipts: dict[int, frozenset[str]] = {}

    def accept(self, receipt: SearchReceipt) -> None:
        previous = self._receipts.get(receipt.source_revision, frozenset())
        self._receipts[receipt.source_revision] = previous | receipt.document_keys

    def contiguous(self, checkpoint_revision: int) -> tuple[int, frozenset[str]] | None:
        revision = checkpoint_revision + 1
        if revision not in self._receipts:
            return None
        keys: set[str] = set()
        while revision in self._receipts:
            keys.update(self._receipts[revision])
            revision += 1
        return revision - 1, frozenset(keys)

    def acknowledge(self, revision: int) -> None:
        for candidate in tuple(self._receipts):
            if candidate <= revision:
                self._receipts.pop(candidate, None)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _require_utf8(value: str, name: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SearchInvalidQuery(f"search {name} is not valid Unicode") from exc
    return value


def _canonical_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise SearchInvalidQuery("search time bounds must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _filter_values(
    values: object,
    name: str,
    expected_type: type[object],
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise SearchInvalidQuery(f"search {name} filter is malformed")
    if expected_type is str:
        valid_types = all(type(value) is str for value in values)
    else:
        valid_types = all(isinstance(value, expected_type) for value in values)
    if not valid_types:
        raise SearchInvalidQuery(f"search {name} filter is malformed")
    result = tuple(_enum_value(value) for value in values)
    if len(result) > MAX_FILTER_ITEMS:
        raise SearchInvalidQuery(f"search {name} filter has too many values")
    if any(not value or len(value) > 256 or "\x00" in value for value in result):
        raise SearchInvalidQuery(f"search {name} filter is malformed")
    for value in result:
        _require_utf8(value, f"{name} filter")
    return result


def canonical_filters(filters: SearchFilters) -> dict[str, object]:
    if not isinstance(filters, SearchFilters):
        raise SearchInvalidQuery("search filters are invalid")
    chat_id = filters.chat_id
    if chat_id is not None:
        if type(chat_id) is not str or not chat_id or len(chat_id) > 64 or "\x00" in chat_id:
            raise SearchInvalidQuery("search chat filter is malformed")
        _require_utf8(chat_id, "chat filter")
    if type(filters.active_branch_only) is not bool or type(filters.include_archived) is not bool:
        raise SearchInvalidQuery("search visibility filters are malformed")
    if filters.after is not None and not isinstance(filters.after, datetime):
        raise SearchInvalidQuery("search lower time bound is malformed")
    if filters.before is not None and not isinstance(filters.before, datetime):
        raise SearchInvalidQuery("search upper time bound is malformed")
    return {
        "chat_id": chat_id,
        "document_kinds": _filter_values(
            filters.document_kinds, "kind", SearchDocumentKind
        ),
        "roles": _filter_values(filters.roles, "role", MessageRole),
        "message_states": _filter_values(
            filters.message_states, "message state", MessageState
        ),
        "backend_ids": _filter_values(filters.backend_ids, "backend", str),
        "provider_ids": _filter_values(filters.provider_ids, "provider", str),
        "models": _filter_values(filters.models, "model", str),
        "connection_ids": _filter_values(filters.connection_ids, "connection", str),
        "model_entry_ids": _filter_values(
            filters.model_entry_ids, "model entry", str
        ),
        "active_branch_only": bool(filters.active_branch_only),
        "include_archived": bool(filters.include_archived),
        "after": _canonical_time(filters.after),
        "before": _canonical_time(filters.before),
    }


def compile_literal_query(query: str, filters: SearchFilters) -> tuple[str, str]:
    if type(query) is not str:
        raise SearchInvalidQuery("search text must be a string")
    if not query.strip():
        raise SearchInvalidQuery("search text must not be empty")
    if len(query) > MAX_QUERY_CODEPOINTS:
        raise SearchInvalidQuery("search text is too long")
    if "\x00" in query or any(ord(character) < 32 and character not in "\t\n\r" for character in query):
        raise SearchInvalidQuery("search text contains unsupported control characters")
    _require_utf8(query, "text")
    # Quote every whitespace-delimited literal independently.  This preserves
    # ordinary FTS all-term search instead of silently turning a multi-word
    # query into an exact phrase, while operators/wildcards/quotes remain data.
    expression = " ".join(
        '"' + token.replace('"', '""') + '"' for token in query.split()
    )
    canonical = json.dumps(
        {"version": 1, "query": query, "filters": canonical_filters(filters)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return expression, hashlib.sha256(canonical).hexdigest()


def bind_cursor_fingerprint(fingerprint: str, process_epoch: str) -> str:
    """Bind an opaque pagination cursor to one live store process."""
    if type(fingerprint) is not str or type(process_epoch) is not str:
        raise SearchInvalidQuery("search cursor fingerprint inputs are malformed")
    return hashlib.sha256(
        f"{fingerprint}\0{process_epoch}".encode("ascii")
    ).hexdigest()


def validate_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise SearchInvalidQuery(f"search limit must be between 1 and {MAX_LIMIT}")
    return limit


def encode_cursor(
    fingerprint: str,
    checkpoint_revision: int,
    generation: int,
    offset: int,
) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "fingerprint": fingerprint,
            "checkpoint": checkpoint_revision,
            "generation": generation,
            "offset": offset,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(
    cursor: str | None,
    *,
    fingerprint: str,
    checkpoint_revision: int,
    generation: int,
) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_BYTES:
        raise SearchCursorStale("search cursor is malformed")
    try:
        encoded = cursor.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        value = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise SearchCursorStale("search cursor is malformed") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "fingerprint", "checkpoint", "generation", "offset"}
        or type(value["v"]) is not int
        or value["v"] != 1
        or not isinstance(value["fingerprint"], str)
        or value["fingerprint"] != fingerprint
        or type(value["checkpoint"]) is not int
        or value["checkpoint"] != checkpoint_revision
        or type(value["generation"]) is not int
        or value["generation"] != generation
        or type(value["offset"]) is not int
        or not 0 <= value["offset"] <= MAX_SQLITE_OFFSET
    ):
        raise SearchCursorStale("search cursor does not match this index snapshot")
    canonical_raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    canonical_cursor = base64.urlsafe_b64encode(canonical_raw).decode("ascii").rstrip("=")
    if raw != canonical_raw or cursor != canonical_cursor:
        raise SearchCursorStale("search cursor is malformed")
    return int(value["offset"])


def _placeholders(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def build_search_statement(
    expression: str,
    filters: SearchFilters,
    *,
    limit: int,
    offset: int,
) -> tuple[str, tuple[object, ...]]:
    """Build a bounded authoritative-filtered FTS query."""
    values = canonical_filters(filters)
    parameters: list[object] = [expression]
    predicates: list[str] = []
    attachment_location_predicates = ["loc_ma.attachment_id=a.id"]
    attachment_location_parameters: list[object] = []

    kinds = values["document_kinds"]
    if kinds:
        assert isinstance(kinds, tuple)
        predicates.append(f"k.document_kind IN ({_placeholders(kinds)})")
        parameters.extend(kinds)

    if values["chat_id"] is not None:
        predicates.append(
            "((k.document_kind='chat' AND c.id=?) "
            "OR (k.document_kind='message' AND mc.id=?) "
            "OR k.document_kind='attachment')"
        )
        parameters.extend([values["chat_id"]] * 2)
        attachment_location_predicates.append("loc_m.chat_id=?")
        attachment_location_parameters.append(values["chat_id"])

    if not values["include_archived"]:
        predicates.append(
            "((k.document_kind='chat' AND c.archived_at IS NULL) "
            "OR (k.document_kind='message' AND mc.archived_at IS NULL) "
            "OR k.document_kind='attachment')"
        )
        attachment_location_predicates.append("loc_c.archived_at IS NULL")

    roles = values["roles"]
    states = values["message_states"]
    attribution_names = (
        ("backend_ids", "backend_id"),
        ("provider_ids", "provider_id"),
        ("models", "model"),
        ("connection_ids", "connection_id"),
        ("model_entry_ids", "model_entry_id"),
    )
    has_message_filter = bool(roles or states or any(values[name] for name, _ in attribution_names))
    if has_message_filter:
        predicates.append("k.document_kind='message'")
    if roles:
        assert isinstance(roles, tuple)
        predicates.append(f"m.role IN ({_placeholders(roles)})")
        parameters.extend(roles)
    if states:
        assert isinstance(states, tuple)
        predicates.append(f"m.state IN ({_placeholders(states)})")
        parameters.extend(states)
    attribution_predicates: list[str] = []
    attribution_parameters: list[object] = []
    for name, column in attribution_names:
        selected = values[name]
        if selected:
            assert isinstance(selected, tuple)
            attribution_predicates.append(f"ga.{column} IN ({_placeholders(selected)})")
            attribution_parameters.extend(selected)
    if attribution_predicates:
        predicates.append(
            "EXISTS (SELECT 1 FROM generation_attempts ga WHERE "
            "(ga.user_message_id=m.id OR ga.assistant_message_id=m.id) AND "
            + " AND ".join(attribution_predicates)
            + ")"
        )
        parameters.extend(attribution_parameters)

    if values["active_branch_only"]:
        predicates.append(
            "((k.document_kind='chat') "
            "OR (k.document_kind='message' AND EXISTS "
            "(SELECT 1 FROM active_messages act WHERE act.message_id=m.id)) "
            "OR k.document_kind='attachment')"
        )
        attachment_location_predicates.append(
            "EXISTS (SELECT 1 FROM active_messages loc_act "
            "WHERE loc_act.message_id=loc_m.id)"
        )

    authoritative_time = (
        "CASE k.document_kind WHEN 'chat' THEN c.updated_at "
        "WHEN 'message' THEN m.created_at ELSE a.created_at END"
    )
    if values["after"] is not None:
        predicates.append(authoritative_time + ">=?")
        parameters.append(values["after"])
    if values["before"] is not None:
        predicates.append(authoritative_time + "<=?")
        parameters.append(values["before"])

    predicates.append(
        "(k.document_kind<>'attachment' OR EXISTS ("
        "SELECT 1 FROM (SELECT attachment_id,message_id FROM message_attachments "
        "UNION ALL SELECT r.attachment_id,im.message_id "
        "FROM archive_import_message_attachment_refs im "
        "JOIN archive_import_attachment_refs r ON r.id=im.attachment_ref_id "
        "WHERE r.availability='READY') loc_ma "
        "JOIN messages loc_m ON loc_m.id=loc_ma.message_id "
        "JOIN chats loc_c ON loc_c.id=loc_m.chat_id WHERE "
        + " AND ".join(attachment_location_predicates)
        + "))"
    )
    parameters.extend(attachment_location_parameters)

    where = " AND ".join(predicates) if predicates else "1"
    statement = f"""
        WITH RECURSIVE active_messages(message_id) AS (
          SELECT head_message_id FROM chats WHERE head_message_id IS NOT NULL
          UNION
          SELECT m.parent_id FROM messages m JOIN active_messages act
            ON m.id=act.message_id WHERE m.parent_id IS NOT NULL
        )
        SELECT k.document_kind, k.document_id, f.document_key,
               bm25(search_fts) AS rank,
               snippet(search_fts, -1, '[', ']', ' … ', 18) AS snippet,
               {authoritative_time} AS authoritative_at,
               f.title, f.body, f.filename
        FROM search_fts f
        JOIN search_document_keys k ON k.fts_rowid=f.rowid
        LEFT JOIN chats c ON k.document_kind='chat' AND c.id=k.document_id
        LEFT JOIN messages m ON k.document_kind='message' AND m.id=k.document_id
        LEFT JOIN chats mc ON m.chat_id=mc.id
        LEFT JOIN attachments a ON k.document_kind='attachment' AND a.id=k.document_id
        WHERE search_fts MATCH ? AND {where}
        ORDER BY rank ASC, authoritative_at DESC,
                 k.document_kind ASC, k.document_id ASC
        LIMIT ? OFFSET ?
    """
    parameters.extend((limit + 1, offset))
    return statement, tuple(parameters)


def deterministic_rowid(document_key: str, occupied: set[int]) -> int:
    value = _initial_rowid(document_key)
    while value in occupied:
        value = _next_rowid(value)
    return value


def _initial_rowid(document_key: str) -> int:
    value = int.from_bytes(hashlib.sha256(document_key.encode("utf-8")).digest()[:8], "big")
    value &= (1 << 63) - 1
    if value == 0:
        value = 1
    return value


def _next_rowid(value: int) -> int:
    return 1 if value == (1 << 63) - 1 else value + 1


def available_rowid(connection, document_key: str) -> int:
    """Resolve a derived rowid without scanning the complete mapping table."""
    value = _initial_rowid(document_key)
    while connection.exec_driver_sql(
        "SELECT 1 FROM search_document_keys WHERE fts_rowid=? LIMIT 1", (value,)
    ).first() is not None:
        value = _next_rowid(value)
    return value


def replace_projection(connection, projection: SearchProjection | None, document_key: str) -> None:
    existing = connection.exec_driver_sql(
        "SELECT fts_rowid FROM search_document_keys "
        "WHERE document_kind=? AND document_id=?",
        tuple(document_key.split(":", 1)),
    ).scalar_one_or_none()
    if existing is not None:
        connection.exec_driver_sql("DELETE FROM search_fts WHERE rowid=?", (int(existing),))
        connection.exec_driver_sql(
            "DELETE FROM search_document_keys WHERE fts_rowid=?", (int(existing),)
        )
    if projection is None:
        return
    rowid = available_rowid(connection, projection.document_key)
    connection.exec_driver_sql(
        "INSERT INTO search_document_keys(fts_rowid, document_kind, document_id) "
        "VALUES (?, ?, ?)",
        (rowid, projection.document_kind, projection.document_id),
    )
    connection.exec_driver_sql(
        "INSERT INTO search_fts(rowid, document_key, title, body, filename) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            rowid,
            projection.document_key,
            projection.title,
            projection.body,
            projection.filename,
        ),
    )
