"""Conformance and compatibility tests for the Phase 5 forbidden-key SQL guard.

The migration-owned triggers enforce the secret-key vocabulary without Python UDFs,
so raw sqlite3 connections are bound by the same policy as the application. These
tests pin three properties of that predicate:

* it never misses a forbidden key, and only over-rejects in the two documented ways;
* it stays shallow enough for the fixed one-hundred-entry LEMON parser stack that
  SQLite used before 3.46.0 (stock Ubuntu 24.04 ships 3.45.1);
* the migrated database really enforces it for raw writes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bots5.core.secrets import (
    _FORBIDDEN_SECRET_KEYS,
    _UNICODE_CASEFOLD_ASCII_MAP,
    is_forbidden_secret_key,
    normalize_secret_key,
    secret_key_forbidden_sql_expression,
)
from tests._authority_test_support import upgrade_database

# SQLite before 3.46.0 parses statements with a fixed 100-entry LEMON stack. The
# generated predicate must stay far below it: the pre-repair expression nested 98
# parentheses and failed with "parser stack overflow" on SQLite 3.45.1.
_MAX_GENERATED_NESTING = 24

_SEEDED_MODEL = "01900000-0000-7000-8000-000000000006"


def _nesting_depth(sql: str) -> int:
    depth = maximum = 0
    in_string = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if character == "'":
            if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string and character == "(":
            depth += 1
            maximum = max(maximum, depth)
        elif not in_string and character == ")":
            depth -= 1
        index += 1
    return maximum


def _ascii_alnum_multiset(value: str) -> list[str]:
    return sorted(
        character
        for character in value.casefold()
        if character.isascii() and character.isalnum()
    )


def _conformance_values() -> list[str]:
    values = [
        "password",
        "pass word",
        "pass-word",
        "pass_word",
        "pass.word",
        "pass/word",
        "p a s s w o r d",
        "PASSWORD",
        "PassWord",
        "api-key",
        "api_key",
        "api key",
        "API KEY",
        "api\u2022key",
        "api$key",
        "AP\u0130-KEY",
        "API\u212aEY",
        "api\u0000key",
        "apricotKey",
        "appleKey",
        "not-a-secret",
        "username",
        "key",
        "tokenizer",
        "secretive",
        "apikeyish",
        "passwordless",
        "aipkey",
        "nekto",
        "opssward",
        "apikey1",
        "apikey!",
        "!apikey",
        "\u0000password",
        "pass\u0000word",
        "\u0000",
        "",
        "  ",
        "\u00e9",
        "\u4e2d\u6587",
        "caf\u00e9",
        "na\u00efve",
    ]
    for key in sorted(_FORBIDDEN_SECRET_KEYS):
        values.extend((key, key.upper(), key.title(), " ".join(key)))
    for code, folded in _UNICODE_CASEFOLD_ASCII_MAP:
        values.append(f"api{chr(code)}key")
        values.append(f"{folded or 'x'}{chr(code)}")
    return list(dict.fromkeys(values))


def _evaluate_sql(values: list[str]) -> dict[str, bool]:
    """Evaluate the predicate on a bare connection: no UDFs, no application state."""
    expression = secret_key_forbidden_sql_expression("key")
    connection = sqlite3.connect(":memory:")
    try:
        statement = f"SELECT CASE WHEN {expression} THEN 1 ELSE 0 END FROM (SELECT ? AS key)"
        return {
            value: bool(connection.execute(statement, (value,)).fetchone()[0])
            for value in values
        }
    finally:
        connection.close()


def test_generated_predicate_never_misses_a_forbidden_key():
    values = _conformance_values()
    verdicts = _evaluate_sql(values)
    missed = sorted(
        value for value in values if is_forbidden_secret_key(value) and not verdicts[value]
    )
    assert missed == []


def test_generated_predicate_only_over_rejects_the_two_accepted_classes():
    """The SQL guard is deliberately stricter than Python in exactly two ways.

    It fails closed on any embedded NUL, and its per-letter multiset test rejects
    anagrams of forbidden keys. Both behaviours predate this repair and are part of
    the accepted raw-DML contract, so they are pinned here rather than removed.
    """
    values = _conformance_values()
    verdicts = _evaluate_sql(values)
    unexpected = []
    for value in values:
        if not verdicts[value] or is_forbidden_secret_key(value):
            continue
        fails_closed_on_nul = "\u0000" in value
        is_anagram_of_forbidden_key = any(
            _ascii_alnum_multiset(value) == sorted(key) for key in _FORBIDDEN_SECRET_KEYS
        )
        if not (fails_closed_on_nul or is_anagram_of_forbidden_key):
            unexpected.append(value)
    assert unexpected == []


def test_generated_predicate_accepts_the_benign_vocabulary():
    benign = [
        "apricotKey",
        "appleKey",
        "not-a-secret",
        "username",
        "key",
        "tokenizer",
        "secretive",
        "passwordless",
        "apikeyish",
        "caf\u00e9",
        "na\u00efve",
        "",
        "  ",
    ]
    assert _evaluate_sql(benign) == {value: False for value in benign}


def test_generated_predicate_stays_within_the_fixed_parser_stack_budget():
    """Regression guard for the SQLite 3.45.1 parser-stack overflow.

    The pre-repair generator produced a 369,679-character expression nesting 98
    parentheses, which SQLite 3.45.1 rejected with "parser stack overflow" while
    preparing ``CREATE TRIGGER``. This assertion fails against that implementation.
    """
    expression = secret_key_forbidden_sql_expression("key")
    assert _nesting_depth(expression) <= _MAX_GENERATED_NESTING
    connection = sqlite3.connect(":memory:")
    try:
        # The real embedding is a CREATE TRIGGER, which is what overflowed.
        connection.execute("CREATE TABLE probe (key TEXT)")
        connection.execute(
            "CREATE TRIGGER probe_guard BEFORE INSERT ON probe "
            f"WHEN {expression} BEGIN SELECT RAISE(ABORT, 'forbidden'); END"
        )
    finally:
        connection.close()


def test_generated_predicate_keeps_the_normalized_vocabulary_intact():
    """The repair must not change which keys the predicate targets."""
    assert _FORBIDDEN_SECRET_KEYS == {
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
    assert normalize_secret_key("API-Key") == "apikey"


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("secret-guard") / "phase5.sqlite3"
    upgrade_database(database)
    return database


def test_migrated_raw_guard_rejects_every_forbidden_key_variant(migrated_database: Path):
    variants: list[str] = []
    for key in sorted(_FORBIDDEN_SECRET_KEYS):
        variants.extend(
            (
                key,
                key.upper(),
                key.title(),
                "-".join(key),
                "_".join(key),
                " ".join(key),
                ".".join(key),
                "\u2022".join(key),
            )
        )
    connection = sqlite3.connect(migrated_database)
    try:
        for variant in variants:
            payload = json.dumps({"nested": {variant: "SENTINEL_RAW_SECRET"}})
            with pytest.raises(sqlite3.IntegrityError, match="metadata"):
                connection.execute(
                    "UPDATE model_catalogue_entries SET metadata_json = ? WHERE id = ?",
                    (payload, _SEEDED_MODEL),
                )
    finally:
        connection.close()


def test_migrated_raw_guard_accepts_benign_nested_metadata(migrated_database: Path):
    payload = json.dumps({"nested": {"apricotKey": "benign"}, "list": [1, 2]})
    connection = sqlite3.connect(migrated_database)
    try:
        connection.execute(
            "UPDATE model_catalogue_entries SET metadata_json = ? WHERE id = ?",
            (payload, _SEEDED_MODEL),
        )
        assert (
            connection.execute(
                "SELECT metadata_json FROM model_catalogue_entries WHERE id = ?",
                (_SEEDED_MODEL,),
            ).fetchone()[0]
            == payload
        )
        connection.rollback()
    finally:
        connection.close()
