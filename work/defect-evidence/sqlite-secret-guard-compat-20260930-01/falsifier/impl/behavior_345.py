"""Runtime behavior of the verbatim persisted candidate triggers on isolated
SQLite 3.45.1: forbidden keys abort, benign keys pass, and json_tree keys drive
the correlated EXISTS per-row.

Run from repo root:
  PYTHONDONTWRITEBYTECODE=1 .venv314/bin/python \
      work/.../falsifier/impl/behavior_345.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

IMPL = Path(__file__).resolve().parent
SCRATCH = IMPL / "scratch"
EVID = IMPL.parent.parent
sys.path.insert(0, str(EVID / "scratch"))
from ctypes_sqlite import Sqlite  # noqa: E402

CAND_DB = SCRATCH / "fresh_candidate.sqlite3"
REPAIRED = [
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
]


def persisted() -> dict[str, str]:
    conn = sqlite3.connect(CAND_DB)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN ({})".format(
                ",".join("?" for _ in REPAIRED)
            ),
            REPAIRED,
        ).fetchall()
    finally:
        conn.close()
    return {str(n): str(s) for n, s in rows}


def main() -> int:
    lib = EVID / "scratch/libsqlite3-3.45.1.so"
    dbpath = SCRATCH / "behavior_345.db"
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(dbpath) + suffix)
        if p.exists():
            p.unlink()
    sq = Sqlite(lib)
    sq.open(str(dbpath))
    assert sq.version() == "3.45.1", sq.version()
    results: dict[str, object] = {"version": sq.version()}

    for stmt in (
        "CREATE TABLE model_catalogue_entries(metadata_json TEXT);",
        "CREATE TABLE capability_facts(provenance_json TEXT);",
    ):
        rc, msg = sq.execute(stmt)
        assert rc == 0, msg
    triggers = persisted()
    for name in REPAIRED:
        rc, msg = sq.execute(triggers[name])
        assert rc == 0, (name, msg)

    def attempt(label: str, sql: str) -> dict:
        rc, msg = sq.execute(sql)
        return {"rc": rc, "aborted": rc != 0, "message": msg}

    cases = {}
    # metadata INSERT
    cases["metadata_insert_benign"] = attempt(
        "m_i_benign",
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES ('{\"apricotKey\":1}');",
    )
    cases["metadata_insert_forbidden"] = attempt(
        "m_i_forbidden",
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES ('{\"nested\":{\"token\":1}}');",
    )
    cases["metadata_insert_sep_insertion"] = attempt(
        "m_i_sep",
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES ('{\"to-ken\":1}');",
    )
    cases["metadata_insert_unicode_fold"] = attempt(
        "m_i_uni",
        # KELVIN SIGN folds to 'k': api<KELVIN>ey == apikey
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES "
        "('{\"api' || char(0x212A) || 'ey\":1}');",
    )
    cases["metadata_insert_nul"] = attempt(
        "m_i_nul",
        # NUL inside the *key*; SQL literal cannot carry a raw NUL, so build it.
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES "
        "('{\"a' || char(0) || 'b\":1}');",
    )
    cases["metadata_insert_benign_fold"] = attempt(
        "m_i_benign_fold",
        # 'ss' insertion between api and key normalises to apisskey: benign
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES ('{\"api\u00dfkey\":1}');",
    )
    # seed one row for updates (benign)
    rc, msg = sq.execute(
        "INSERT INTO model_catalogue_entries(metadata_json) VALUES ('{\"ok\":1}');"
    )
    assert rc == 0, msg
    cases["metadata_update_benign"] = attempt(
        "m_u_benign",
        "UPDATE model_catalogue_entries SET metadata_json='{\"appleKey\":2}';",
    )
    cases["metadata_update_forbidden"] = attempt(
        "m_u_forbidden",
        "UPDATE model_catalogue_entries SET metadata_json='{\"PASSWORD\":2}';",
    )
    # capability provenance
    cases["provenance_insert_benign"] = attempt(
        "p_i_benign",
        "INSERT INTO capability_facts(provenance_json) VALUES "
        "('{\"reason\":\"because\",\"field\":\"x\",\"catalogue_revision\":2}');",
    )
    cases["provenance_insert_forbidden"] = attempt(
        "p_i_forbidden",
        "INSERT INTO capability_facts(provenance_json) VALUES ('{\"secret\":1}');",
    )
    cases["provenance_insert_illegal_field"] = attempt(
        "p_i_illegal",
        "INSERT INTO capability_facts(provenance_json) VALUES ('{\"other\":1}');",
    )
    rc, msg = sq.execute(
        "INSERT INTO capability_facts(provenance_json) VALUES ('{\"reason\":\"r\"}');"
    )
    assert rc == 0, msg
    cases["provenance_update_benign"] = attempt(
        "p_u_benign",
        "UPDATE capability_facts SET provenance_json='{\"field\":\"y\"}';",
    )
    cases["provenance_update_forbidden"] = attempt(
        "p_u_forbidden",
        "UPDATE capability_facts SET provenance_json='{\"TOKEN\":1}';",
    )

    sq.close()
    print(json.dumps(cases, indent=2))
    (IMPL / "behavior_345_result.json").write_text(json.dumps(cases, indent=2))

    ok = (
        cases["metadata_insert_benign"]["aborted"] is False
        and cases["metadata_insert_forbidden"]["aborted"] is True
        and cases["metadata_insert_sep_insertion"]["aborted"] is True
        and cases["metadata_insert_unicode_fold"]["aborted"] is True
        and cases["metadata_insert_nul"]["aborted"] is True
        and cases["metadata_insert_benign_fold"]["aborted"] is False
        and cases["metadata_update_benign"]["aborted"] is False
        and cases["metadata_update_forbidden"]["aborted"] is True
        and cases["provenance_insert_benign"]["aborted"] is False
        and cases["provenance_insert_forbidden"]["aborted"] is True
        and cases["provenance_insert_illegal_field"]["aborted"] is True
        and cases["provenance_update_benign"]["aborted"] is False
        and cases["provenance_update_forbidden"]["aborted"] is True
    )
    print("BEHAVIOR_345", "OK" if ok else "BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
