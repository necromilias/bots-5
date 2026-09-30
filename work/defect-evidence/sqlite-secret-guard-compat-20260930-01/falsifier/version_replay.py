"""End-to-end version replay: same JSON docs through the guard trigger on
isolated SQLite 3.45.1 (candidate) and on SQLite 3.50.4 (baseline + candidate).
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parent / "scratch"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRATCH))
sys.path.insert(0, str(HERE.parents[3] / "src"))
from ctypes_sqlite import Sqlite  # noqa: E402
import bots5.core.secrets as baseline  # noqa: E402
import candidate_secrets as candidate  # noqa: E402
import json_stress  # noqa: E402

EXPR_B = baseline.secret_key_forbidden_sql_expression("key")
EXPR_C = candidate.secret_key_forbidden_sql_expression("key")


def safe_json_object(expr):
    return ("json_valid(NEW.metadata_json) = 1 AND json_type(NEW.metadata_json) = 'object' "
            "AND NOT EXISTS (SELECT 1 FROM json_tree(NEW.metadata_json) "
            f"WHERE key IS NOT NULL AND {expr})")


def replay_python(expr, docs):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(metadata_json TEXT)")
    con.execute("CREATE TRIGGER g BEFORE INSERT ON t WHEN NOT ("
                + safe_json_object(expr) + ") BEGIN SELECT RAISE(ABORT,'bad'); END")
    out = []
    for d in docs:
        try:
            con.execute("INSERT INTO t VALUES (?)", (d,))
            out.append(1)
        except sqlite3.IntegrityError:
            out.append(0)
    con.close()
    return out


def replay_345(expr, docs, dbpath):
    for suffix in ("", "-journal", "-wal"):
        p = Path(str(dbpath) + suffix)
        if p.exists():
            p.unlink()
    sq = Sqlite(SCRATCH / "libsqlite3-3.45.1.so")
    sq.open(str(dbpath))
    rc, msg = sq.execute("CREATE TABLE t(metadata_json TEXT);")
    assert rc == 0, msg
    rc, msg = sq.execute("CREATE TRIGGER g BEFORE INSERT ON t WHEN NOT ("
                         + safe_json_object(expr) + ") BEGIN SELECT RAISE(ABORT,'bad'); END;")
    if rc != 0:
        return None, msg
    out = []
    for d in docs:
        escaped = d.replace("'", "''")
        rc, msg = sq.execute(f"INSERT INTO t VALUES ('{escaped}');")
        out.append(1 if rc == 0 else 0)
    sq.close()
    return out, ""


def main() -> None:
    rng = random.Random(4242)
    docs = ['{"a":[1,2,3]}', '{"a":[{"password":1}]}', '{"0":"x"}',
            '{"a":{"b":{"c":{"password":1}}}}', '{"arr":[[["secret"]]]}',
            '[{"token":1}]', '{"\u00df":1}']
    for _ in range(1500):
        docs.append(json.dumps(json_stress.rand_value(rng)))
    docs += ['{"nekto":1}', '{"password\\u0000x":1}', '{"pa\\u00dfword":1}']

    b50 = replay_python(EXPR_B, docs)
    c50 = replay_python(EXPR_C, docs)
    c345, err = replay_345(EXPR_C, docs, HERE / "replay_cand345.db")
    if c345 is None:
        print("3.45.1 candidate CREATE FAILED", err)
        return
    diffs_bc = [i for i in range(len(docs)) if b50[i] != c50[i]]
    diffs_v = [i for i in range(len(docs)) if c50[i] != c345[i]]
    print("docs", len(docs))
    print("baseline(3.50) vs candidate(3.50):", len(diffs_bc))
    for i in diffs_bc[:10]:
        print("  DIFF", repr(docs[i])[:160], b50[i], c50[i])
    print("candidate(3.50) vs candidate(3.45.1):", len(diffs_v))
    for i in diffs_v[:10]:
        print("  VDIFF", repr(docs[i])[:160], c50[i], c345[i])
    (HERE / "replay_result.json").write_text(json.dumps({
        "docs": len(docs), "bc_diffs": len(diffs_bc), "version_diffs": len(diffs_v)}))


if __name__ == "__main__":
    main()
