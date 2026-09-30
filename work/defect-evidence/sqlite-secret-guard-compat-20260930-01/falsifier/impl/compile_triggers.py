"""Compile the PERSISTED trigger SQL (verbatim from the two databases) on isolated
SQLite 3.45.1 and 3.46.0, without touching the host sqlite.

Run from repo root:
  PYTHONDONTWRITEBYTECODE=1 .venv314/bin/python \
      work/.../falsifier/impl/compile_triggers.py
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
BASE_DB = SCRATCH / "fresh_baseline.sqlite3"

REPAIRED = [
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
]

SETUP = [
    "CREATE TABLE model_catalogue_entries(metadata_json TEXT);",
    "CREATE TABLE capability_facts(provenance_json TEXT);",
]


def persisted(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
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


def run_on(lib: Path, trigger_sql: dict[str, str], dbpath: Path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(dbpath) + suffix)
        if p.exists() or p.is_symlink():
            p.unlink()
    sq = Sqlite(lib)
    sq.open(str(dbpath))
    results = {"version": sq.version()}
    for stmt in SETUP:
        rc, msg = sq.execute(stmt)
        if rc != 0:
            results["setup_error"] = msg
    for name in REPAIRED:
        rc, msg = sq.execute(trigger_sql[name])
        results[name] = {"rc": rc, "ok": rc == 0, "error": msg}
    sq.close()
    return results


def main() -> int:
    cand = persisted(CAND_DB)
    base = persisted(BASE_DB)
    print("candidate trigger SQL sizes:", {n: len(cand[n]) for n in REPAIRED})
    print("baseline  trigger SQL sizes:", {n: len(base[n]) for n in REPAIRED})

    out = {}
    lib345 = EVID / "scratch/libsqlite3-3.45.1.so"
    out["candidate_345"] = run_on(lib345, cand, SCRATCH / "compile_cand_345.db")
    out["baseline_345"] = run_on(lib345, base, SCRATCH / "compile_base_345.db")

    # 3.46.0 candidate should succeed too (baseline reconstruction not needed here)
    lib346 = sorted(EVID.glob("scratch/libsqlite3-3.46*.so"))
    if lib346:
        out["candidate_346"] = run_on(lib346[0], cand, SCRATCH / "compile_cand_346.db")

    print(json.dumps(out, indent=2))
    (IMPL / "compile_triggers_result.json").write_text(json.dumps(out, indent=2))

    cand_fail = [n for n in REPAIRED if not out["candidate_345"][n]["ok"]]
    base_fail = [n for n in REPAIRED if out["baseline_345"][n]["ok"]]
    print("candidate 3.45.1 failures:", cand_fail)
    print("baseline  3.45.1 unexpectedly-compiled:", base_fail)
    ok = not cand_fail and not base_fail
    print("DISCRIMINATION", "OK" if ok else "BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
