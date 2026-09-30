"""Definitive compatibility proof for the repaired Phase 5 forbidden-key guard.

Takes the four real trigger definitions out of a database built by the repaired
migration, recreates them on an isolated SQLite 3.45.1 (stock Ubuntu 24.04) and on
the current runtime, and checks that the raw-DML guard behaves exactly like the
Python predicate.

Usage: PYTHONPATH=<repo>/src:<repo> <repo>/.venv314/bin/python compat_proof.py <scratch-dir>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE / "scratch"))

from bots5.core.secrets import (  # noqa: E402
    _FORBIDDEN_SECRET_KEYS,
    is_forbidden_secret_key,
)

from ctypes_sqlite import Sqlite  # noqa: E402

TRIGGERS = (
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
)


def trigger_sql(database: Path, name: str) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, name
    return str(row[0])


def probe_statements(database: Path) -> list[str]:
    """Minimal tables plus the four real triggers taken from ``database``."""
    statements = [
        "CREATE TABLE model_catalogue_entries (metadata_json TEXT)",
        "CREATE TABLE capability_facts (provenance_json TEXT)",
    ]
    statements.extend(trigger_sql(database, name) for name in TRIGGERS)
    return statements


def corpus() -> list[str]:
    values = [
        "password", "pass word", "pass-word", "pass_word", "pass.word", "pass/word",
        "PASSWORD", "PassWord", "p a s s w o r d", "api-key", "api_key", "api key",
        "API KEY", "apikey", "apiKeyValue", "api\u2022key", "api$key", "AP\u0130-KEY",
        "token", "secret", "authorization", "client_secret", "refresh-token",
        "access token", "apricotKey", "appleKey", "not-a-secret", "username",
        "key", "tokenizer", "secretive", "apikeyish", "passwordless",
        "pa\u00dfword", "pa\u017fsword", "acces\ufb05oken", "\u212aey", "to\u212aen",
        "aipkey", "nekto", "opssward", "apikey1", "apikey!", "!apikey",
        "\u0000password", "pass\u0000word", "\u0000", "apikey\u0000",
        "", "  ", "\u00e9", "\u4e2d\u6587", "caf\u00e9", "na\u00efve",
    ]
    for key in sorted(_FORBIDDEN_SECRET_KEYS):
        values.extend((key, key.upper(), key.title(), key.replace("", " ").strip()))
    return list(dict.fromkeys(values))


def create_on_345(scratch: Path, statements: list[str], label: str) -> tuple[int, list[str]]:
    engine = Sqlite(scratch / "libsqlite3-3.45.1.so")
    database = scratch / f"create_{label}.db"
    if database.exists():
        database.unlink()
    engine.open(str(database))
    created, failures = 0, []
    for statement in statements:
        code, message = engine.execute(statement)
        if code == 0:
            created += 1
        else:
            failures.append(f"{statement.split()[0:5]}: {message}")
    engine.close()
    return created, failures


def dml_on_345(scratch: Path, statements: list[str], values: list[str]) -> list[str]:
    engine = Sqlite(scratch / "libsqlite3-3.45.1.so")
    database = scratch / "dml_345.db"
    if database.exists():
        database.unlink()
    engine.open(str(database))
    for statement in statements:
        code, message = engine.execute(statement)
        assert code == 0, message
    rejected = []
    for value in values:
        payload = json.dumps({value: 1})
        rows, error = engine.query(
            "INSERT INTO model_catalogue_entries (metadata_json) VALUES (?)", (payload,)
        )
        if error:
            rejected.append(value)
    engine.close()
    return rejected


def dml_current(statements: list[str], values: list[str]) -> list[str]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            connection.execute(statement)
        rejected = []
        for value in values:
            payload = json.dumps({value: 1})
            try:
                connection.execute(
                    "INSERT INTO model_catalogue_entries (metadata_json) VALUES (?)",
                    (payload,),
                )
            except sqlite3.IntegrityError:
                rejected.append(value)
        return rejected
    finally:
        connection.close()


def main() -> int:
    scratch = Path(sys.argv[1]).resolve()
    repaired = scratch / "new_head.db"
    prerepair = scratch / "old_head.db"
    values = corpus()

    for label, database in (("pre-repair", prerepair), ("repaired", repaired)):
        statements = probe_statements(database)
        created, failures = create_on_345(scratch, statements, label)
        print(f"3.45.1 create [{label}]: {created}/{len(statements)} statements ok")
        for failure in failures:
            print(f"    FAIL {failure}")

    statements = probe_statements(repaired)
    rejected_345 = dml_on_345(scratch, statements, values)
    rejected_now = dml_current(statements, values)
    expected = {value for value in values if is_forbidden_secret_key(value)}

    print(f"runtime versions: 3.45.1={Sqlite(scratch / 'libsqlite3-3.45.1.so').version()} current={sqlite3.sqlite_version}")
    print(f"corpus values: {len(values)}; python-forbidden: {len(expected)}")
    print(f"3.45.1 rejected: {len(rejected_345)}; current rejected: {len(rejected_now)}")
    print(f"3.45.1 vs current disagreement: {sorted(set(rejected_345) ^ set(rejected_now))}")
    print(f"forbidden keys the repaired 3.45.1 guard missed: {sorted(expected - set(rejected_345))}")
    print(f"SQL-rejected but python-accepted (accepted quirk): {sorted(set(rejected_345) - expected)}")

    pre_created = create_on_345(scratch, probe_statements(prerepair), "pre-repair-check")[0]
    ok = (
        pre_created < len(TRIGGERS) + 2
        and create_on_345(scratch, statements, "repaired-check")[0] == len(TRIGGERS) + 2
        and not (expected - set(rejected_345))
        and set(rejected_345) == set(rejected_now)
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
