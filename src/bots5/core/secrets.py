from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from bots5.domain.provider import CredentialSource, CredentialStatus


class SecretStoreError(RuntimeError):
    """A sanitized credential-store failure that never carries a secret value."""


_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "apikey",
        "apikeyvalue",
        "apitoken",
        "accesstoken",
        "authorization",
        "clientsecret",
        "password",
        "refreshtoken",
        "secret",
        "secretvalue",
        "token",
    }
)

_ASCII_NON_ALNUM_CODES = tuple(
    code
    for code in range(128)
    if not chr(code).isascii() or not chr(code).isalnum()
)

# SQLite's built-in lower() is ASCII-only.  These are the non-ASCII Unicode
# code points whose Python casefold() contributes ASCII alphanumeric text to
# normalize_secret_key(); the remaining non-ASCII code points are treated as
# separators by the residual check below.  Keeping this small table explicit
# makes the migration-owned SQL predicate deterministic without requiring a
# Python UDF on raw sqlite3 connections.
_UNICODE_CASEFOLD_ASCII_MAP = (
    (0x00DF, "ss"),
    (0x0130, "i"),
    (0x0149, "n"),
    (0x017F, "s"),
    (0x01F0, "j"),
    (0x1E96, "h"),
    (0x1E97, "t"),
    (0x1E98, "w"),
    (0x1E99, "y"),
    (0x1E9A, "a"),
    (0x1E9E, "ss"),
    (0x212A, "k"),
    (0xFB00, "ff"),
    (0xFB01, "fi"),
    (0xFB02, "fl"),
    (0xFB03, "ffi"),
    (0xFB04, "ffl"),
    (0xFB05, "st"),
    (0xFB06, "st"),
)


def normalize_secret_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def is_forbidden_secret_key(value: object) -> bool:
    return isinstance(value, str) and normalize_secret_key(value) in _FORBIDDEN_SECRET_KEYS


def secret_key_forbidden_sql_expression(column: str) -> str:
    """Build the SQLite equivalent of the normalized forbidden-key predicate.

    SQLite connections used by raw-DML probes do not have Python UDFs installed,
    so the migration-owned triggers inline the same normalization policy. The
    residual non-ASCII check treats Unicode separators as removed for the
    forbidden-key vocabulary while preserving benign keys such as ``apricotKey``.
    """
    expression = f"lower(CAST({column} AS TEXT))"
    for code, folded in _UNICODE_CASEFOLD_ASCII_MAP:
        expression = f"replace({expression}, char({code}), '{folded}')"
    for code in _ASCII_NON_ALNUM_CODES:
        expression = f"replace({expression}, char({code}), '')"
    forbidden = ", ".join(f"'{key}'" for key in sorted(_FORBIDDEN_SECRET_KEYS))
    unicode_variants = []
    for key in sorted(_FORBIDDEN_SECRET_KEYS):
        residual = expression
        for character in sorted(set(key)):
            residual = f"replace({residual}, '{character}', '')"
        counts = " AND ".join(
            f"length({expression}) - length(replace({expression}, '{character}', '')) = {key.count(character)}"
            for character in sorted(set(key))
        )
        unicode_variants.append(
            f"({counts} AND {residual} NOT GLOB '*[A-Za-z0-9]*')"
        )
    return (
        f"(instr(CAST({column} AS TEXT), char(0)) > 0 OR "
        f"{expression} IN ({forbidden}) OR {' OR '.join(unicode_variants)})"
    )


def sanitize_secret_error(error: object, credential: str | None) -> str:
    """Return bounded diagnostics without exposing credential material."""
    detail = str(error)
    if credential:
        detail = detail.replace(credential, "[REDACTED]")
    detail = _BEARER_RE.sub("Bearer [REDACTED]", detail)
    return detail[:500]


def reject_secret_material(value: object, credential: str | None) -> None:
    """Reject untrusted returned data containing the resolved credential."""
    if not credential:
        return

    def contains_secret(item: object) -> bool:
        if isinstance(item, str):
            return credential in item
        if isinstance(item, Mapping):
            return any(
                contains_secret(key) or contains_secret(nested)
                for key, nested in item.items()
            )
        if isinstance(item, (list, tuple)):
            return any(contains_secret(nested) for nested in item)
        return False

    if contains_secret(value):
        raise SecretStoreError("untrusted provider data contained credential material")


class SecretStore(Protocol):
    """Core-owned port for explicit credential sources."""

    source: CredentialSource

    def status(self, reference: str | None) -> CredentialStatus:
        ...

    def get(self, reference: str) -> str:
        ...

    def put(self, reference: str, value: str) -> None:
        ...

    def delete(self, reference: str) -> None:
        ...
