"""Compile the four Phase 5 row-guard trigger shapes on isolated SQLite 3.45.1
using the candidate expression, and (for contrast) the baseline expression."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parent / "scratch"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scratch"))
from ctypes_sqlite import Sqlite  # noqa: E402

sys.path.insert(0, str(HERE.parents[3] / "src"))
import bots5.core.secrets as baseline  # noqa: E402
import candidate_secrets as candidate  # noqa: E402


def safe_json_object(expr, column):
    return (f"json_valid({column}) = 1 AND json_type({column}) = 'object' AND NOT EXISTS ("
            f"SELECT 1 FROM json_tree({column}) WHERE key IS NOT NULL AND {expr})")


def safe_capability_provenance(expr, column):
    return (f"({safe_json_object(expr, column)} AND NOT EXISTS ("
            f"SELECT 1 FROM json_each({column}) "
            "WHERE key NOT IN ('reason', 'field', 'catalogue_revision')) "
            f"AND (json_type({column}, '$.reason') IS NULL OR json_type({column}, '$.reason') IN ('text', 'null')) "
            f"AND (json_type({column}, '$.field') IS NULL OR json_type({column}, '$.field') IN ('text', 'null')) "
            f"AND (json_type({column}, '$.catalogue_revision') IS NULL OR json_type({column}, '$.catalogue_revision') IN ('integer', 'null')) "
            f"AND (json_type({column}, '$.reason') IS NULL OR length(json_extract({column}, '$.reason')) <= 256) "
            f"AND (json_type({column}, '$.field') IS NULL OR length(json_extract({column}, '$.field')) <= 256) "
            f"AND (json_type({column}, '$.catalogue_revision') IS NULL OR json_extract({column}, '$.catalogue_revision') >= 0))")


def statements(expr):
    return [
        ("CREATE TABLE model_catalogue_entries(metadata_json TEXT);", "setup"),
        ("CREATE TABLE capability_facts(provenance_json TEXT);", "setup"),
        (f"CREATE TRIGGER phase5_model_catalogue_metadata_validate_insert "
         f"BEFORE INSERT ON model_catalogue_entries WHEN NOT ({safe_json_object(expr, 'NEW.metadata_json')}) "
         f"BEGIN SELECT RAISE(ABORT, 'Phase 5 model catalogue metadata is malformed'); END;",
         "metadata_insert"),
        (f"CREATE TRIGGER phase5_model_catalogue_metadata_validate_update "
         f"BEFORE UPDATE OF metadata_json ON model_catalogue_entries WHEN NOT ({safe_json_object(expr, 'NEW.metadata_json')}) "
         f"BEGIN SELECT RAISE(ABORT, 'Phase 5 model catalogue metadata is malformed'); END;",
         "metadata_update"),
        (f"CREATE TRIGGER phase5_capability_fact_provenance_validate_insert "
         f"BEFORE INSERT ON capability_facts WHEN NOT ({safe_capability_provenance(expr, 'NEW.provenance_json')}) "
         f"BEGIN SELECT RAISE(ABORT, 'Phase 5 capability provenance is malformed'); END;",
         "provenance_insert"),
        (f"CREATE TRIGGER phase5_capability_fact_provenance_validate_update "
         f"BEFORE UPDATE OF provenance_json ON capability_facts WHEN NOT ({safe_capability_provenance(expr, 'NEW.provenance_json')}) "
         f"BEGIN SELECT RAISE(ABORT, 'Phase 5 capability provenance is malformed'); END;",
         "provenance_update"),
    ]


def run_ctypes(lib: Path, expr, dbpath: Path):
    for suffix in ("", "-journal", "-wal"):
        p = Path(str(dbpath) + suffix)
        if p.exists():
            p.unlink()
    sq = Sqlite(lib)
    sq.open(str(dbpath))
    out = []
    for sql, label in statements(expr):
        rc, msg = sq.execute(sql)
        out.append((label, rc, msg))
    sq.close()
    return out


def run_python(expr):
    con = sqlite3.connect(":memory:")
    out = []
    for sql, label in statements(expr):
        try:
            con.execute(sql)
            out.append((label, 0, ""))
        except Exception as exc:  # noqa: BLE001
            out.append((label, 1, str(exc)))
    con.close()
    return out


def run_cli(cli: Path, expr, dbpath: Path):
    for suffix in ("", "-journal", "-wal"):
        p = Path(str(dbpath) + suffix)
        if p.exists():
            p.unlink()
    out = []
    for sql, label in statements(expr):
        proc = subprocess.run([str(cli), str(dbpath)], input=sql + ";",
                              capture_output=True, text=True)
        out.append((label, proc.returncode, (proc.stderr or "").strip()))
    return out


def main() -> None:
    lib345 = SCRATCH / "libsqlite3-3.45.1.so"
    cli346 = SCRATCH / "sqlite3-3460000"
    cli345 = SCRATCH / "sqlite3-3450100"
    expr_c = candidate.secret_key_forbidden_sql_expression("key")
    expr_b = baseline.secret_key_forbidden_sql_expression("key")
    print("candidate expr bytes", len(expr_c.encode()))
    print("baseline expr bytes", len(expr_b.encode()))

    print("\n== 3.45.1 (ctypes) candidate ==")
    for label, rc, msg in run_ctypes(lib345, expr_c, HERE / "ft345c.db"):
        print(f"  {label}: rc={rc} {msg[:80]}")
    print("\n== 3.45.1 (ctypes) baseline ==")
    for label, rc, msg in run_ctypes(lib345, expr_b, HERE / "ft345b.db"):
        print(f"  {label}: rc={rc} {msg[:80]}")
    if cli346.exists():
        print("\n== 3.46.0 (CLI) candidate ==")
        for label, rc, msg in run_cli(cli346, expr_c, HERE / "ft346c.db"):
            print(f"  {label}: rc={rc} {msg[:80]}")
        print("\n== 3.46.0 (CLI) baseline ==")
        for label, rc, msg in run_cli(cli346, expr_b, HERE / "ft346b.db"):
            print(f"  {label}: rc={rc} {msg[:80]}")
    print("\n== 3.50.4 (python) candidate/baseline ==")
    for expr, name in ((expr_c, "candidate"), (expr_b, "baseline")):
        res = run_python(expr)
        print(f"  {name}:", [(label, rc) for label, rc, _ in res])


if __name__ == "__main__":
    main()
