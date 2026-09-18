"""Closed Phase 9 interchange primitives.

The archive container is deliberately not the canonical representation: each
logical JSON or JSONL entry is.  These functions are small enough to audit and
are shared by the core projection and the untrusted-container validator.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable, Mapping


MAX_INTEGER = (1 << 63) - 1


class InterchangeError(ValueError):
    """A value cannot participate in the closed interchange contract."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InterchangeError("duplicate JSON object key")
        result[key] = value
    return result


def strict_json_loads(raw: bytes | str) -> object:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, InterchangeError) as exc:
        raise InterchangeError("invalid canonical JSON") from exc
    _validate_value(value)
    return value


def _reject_constant(value: str) -> object:
    raise InterchangeError(f"non-finite JSON number: {value}")


def _validate_value(value: object) -> None:
    if value is None or type(value) in {bool, str}:
        return
    if type(value) is int:
        if not -MAX_INTEGER <= value <= MAX_INTEGER:
            raise InterchangeError("JSON integer exceeds SQLite-safe range")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise InterchangeError("JSON number is not finite")
        return
    if isinstance(value, Decimal):
        raise InterchangeError("decimal values must be represented as strings")
    if isinstance(value, list):
        for item in value:
            _validate_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise InterchangeError("JSON object keys must be strings")
            _validate_value(item)
        return
    raise InterchangeError("JSON value has an unsupported type")


def canonical_json_bytes(value: object) -> bytes:
    _validate_value(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InterchangeError("value is not canonical JSON") from exc
    if encoded.startswith(b"\xef\xbb\xbf") or b"\r" in encoded:
        raise InterchangeError("canonical JSON encoding invariant failed")
    return encoded + b"\n"


def canonical_jsonl_bytes(rows: Iterable[object]) -> bytes:
    return b"".join(canonical_json_bytes(row) for row in rows)


def parse_jsonl(raw: bytes) -> tuple[object, ...]:
    if b"\r" in raw or raw.startswith(b"\xef\xbb\xbf"):
        raise InterchangeError("JSONL must be UTF-8 with LF line endings")
    if raw and not raw.endswith(b"\n"):
        raise InterchangeError("JSONL must end with LF")
    if raw and any(not line for line in raw[:-1].split(b"\n")):
        raise InterchangeError("JSONL must not contain blank lines")
    rows = tuple(strict_json_loads(line) for line in raw.splitlines())
    if b"".join(canonical_json_bytes(row) for row in rows) != raw:
        raise InterchangeError("JSONL rows are not canonical")
    return rows


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InterchangeError("timestamp must be timezone aware")
    result = value.astimezone(UTC).isoformat(timespec="microseconds")
    return result.replace("+00:00", "Z")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def logical_content_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    rows = sorted(entries, key=lambda item: item[0])
    if len({item[0] for item in rows}) != len(rows):
        raise InterchangeError("logical inventory has duplicate paths")
    payload = "".join(f"{path}\t{size}\t{digest}\n" for path, size, digest in rows)
    return sha256_hex(payload.encode("utf-8"))
