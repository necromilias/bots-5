"""Deterministic Python <-> SQL conformance corpus for the forbidden-key predicate.

Compares, over one fixed corpus:
  * the baseline SQLite predicate (current repository implementation),
  * the candidate repaired SQLite predicate,
  * Python ``is_forbidden_secret_key``.

Run with the repository virtualenv interpreter.
"""
from __future__ import annotations

import importlib.util  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, "src")
sys.path.insert(0, str(HERE / "scratch"))

from bots5.core.secrets import (  # noqa: E402
    _FORBIDDEN_SECRET_KEYS,
    _UNICODE_CASEFOLD_ASCII_MAP,
    is_forbidden_secret_key,
    secret_key_forbidden_sql_expression as candidate_expression,
)

# Pristine pre-repair predicate, extracted with `git show HEAD:src/bots5/core/secrets.py`.
_spec = importlib.util.spec_from_file_location(
    "baseline_secrets", HERE / "scratch" / "baseline_secrets.py"
)
assert _spec is not None and _spec.loader is not None
_baseline_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_baseline_module)
baseline_expression = _baseline_module.secret_key_forbidden_sql_expression

SEPARATORS = [
    "-", "_", ".", "$", " ", "\t", "\n", "\r", "\x0b", "\x00", "!", "@", "#", "%",
    "^", "&", "*", "(", ")", "+", "=", ",", ":", ";", '"', "'", "<", ">", "?",
    "/", "\\", "[", "]", "{", "}", "|", "~", "`", "•", "·", "€", "→", " ",
    "\u200b", "\u2028", "\uff0d", "\u3000", "\U0001f600", "\u0660", "\u06f1",
]
MAP_CHARS = [chr(code) for code, _ in _UNICODE_CASEFOLD_ASCII_MAP]
BENIGN = [
    "apricotKey", "monkey", "tokens", "secrets", "passwordless", "authorized",
    "key", "api", "", "model", "default", "abcdef", "0123456789", "apikey0",
    "apikeyvaluex", "notapikey", "secretz", "tokenz", "tok", "pwd", "pass",
]


def corpus() -> list[str]:
    values: list[str] = []
    for key in sorted(_FORBIDDEN_SECRET_KEYS):
        values.extend({key, key.upper(), key.title(), key.capitalize()})
        values.append("".join(c.upper() if i % 2 else c for i, c in enumerate(key)))
        # every separator at every position
        for separator in SEPARATORS:
            for position in range(len(key) + 1):
                values.append(key[:position] + separator + key[position:])
        # each Unicode case-fold character at every position and alone
        for mapped in MAP_CHARS:
            values.append(mapped)
            for position in range(len(key) + 1):
                values.append(key[:position] + mapped + key[position:])
        # anagrams (deterministic sample of permutations)
        for permutation in itertools.islice(itertools.permutations(key), 6):
            values.append("".join(permutation))
        # extra alphanumeric material must stay benign
        for extra in ("x", "1", "zz", "9"):
            values.append(key + extra)
            values.append(extra + key)
        # separators mixed through the key
        values.append(separator_join(key, "-"))
        values.append(separator_join(key, "_"))
        values.append(separator_join(key, "\u2022"))
    values.extend(BENIGN)
    values.extend(["api\u0000key", "\u0000", "a\u0000", "apikey\u0000x", "tok\u0000en"])
    # mapped-character expansions that must stay benign
    values.extend(["pa\u00dfwordx", "pa\u00df", "\u00df", "stra\u00dfe", "secret\u00df"])
    rng = random.Random(20260930)
    alphabet = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        "-_.$ \t\n\u0000\u2022\u0130\u212a\u00df\u017f\ufb01\u0430\U0001f600"
    )
    for _ in range(4000):
        length = rng.randint(0, 12)
        values.append("".join(rng.choice(alphabet) for _ in range(length)))
    # near-miss mutations of forbidden keys
    for key in sorted(_FORBIDDEN_SECRET_KEYS):
        for _ in range(200):
            chars = list(key)
            index = rng.randrange(len(chars))
            chars[index] = rng.choice(alphabet)
            values.append("".join(chars))
    # de-duplicate, preserving determinism
    return sorted(set(values))


def separator_join(key: str, separator: str) -> str:
    return separator.join(key)


def evaluate(connection: sqlite3.Connection, expression: str) -> list[int]:
    connection.execute("DROP TABLE IF EXISTS corpus")
    connection.execute("CREATE TABLE corpus (key)")
    connection.executemany("INSERT INTO corpus (key) VALUES (?)", [(v,) for v in VALUES])
    rows = connection.execute(
        f"SELECT key, ({expression}) FROM corpus ORDER BY rowid"
    ).fetchall()
    return rows


VALUES = corpus()

if __name__ == "__main__":
    connection = sqlite3.connect(":memory:")
    print("sqlite", sqlite3.sqlite_version, "corpus", len(VALUES))
    baseline = evaluate(connection, baseline_expression("key"))
    candidate = evaluate(connection, candidate_expression("key"))
    disagreements = []
    for (b_key, b_value), (c_key, c_value) in zip(baseline, candidate):
        if b_key != c_key:
            raise SystemExit("corpus ordering drifted")
        if bool(b_value) != bool(c_value):
            disagreements.append((b_key, b_value, c_value))
    print("baseline vs candidate disagreements:", len(disagreements))
    for item in disagreements[:40]:
        print("   ", repr(item))
    # informational: how each SQL predicate relates to Python
    baseline_python = [i for i, (k, v) in enumerate(baseline) if bool(v) != is_forbidden_secret_key(k)]
    candidate_python = [i for i, (k, v) in enumerate(candidate) if bool(v) != is_forbidden_secret_key(k)]
    print("baseline SQL != Python:", len(baseline_python))
    print("candidate SQL != Python:", len(candidate_python))
    print("baseline SQL != Python examples:", [baseline[i][0] for i in baseline_python[:12]])
    if baseline_python != candidate_python:
        raise SystemExit("candidate changed the SQL-vs-Python relationship")
    print("RESULT:", "EQUIVALENT" if not disagreements else "DIVERGENT")
