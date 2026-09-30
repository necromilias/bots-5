"""Full SQL verification of baseline vs candidate over the entire corpus, chunked."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_cases  # noqa: E402

EXPR_B = (HERE / "baseline_expr.sql").read_text()
EXPR_C = (HERE / "candidate_expr.sql").read_text()


def main() -> None:
    cases = json.loads((HERE / "cases.json").read_text())
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v)")
    con.executemany("INSERT INTO t(id, v) VALUES (?, ?)", list(enumerate(cases)))

    mismatches = []
    total = len(cases)
    started = time.time()
    chunk = 8000
    done = 0
    for start in range(0, total, chunk):
        stop = min(start + chunk, total)
        rows_b = {i: r for i, r in con.execute(
            f"SELECT id, ({EXPR_B}) FROM t WHERE id >= ? AND id < ?", (start, stop))}
        rows_c = {i: r for i, r in con.execute(
            f"SELECT id, ({EXPR_C}) FROM t WHERE id >= ? AND id < ?", (start, stop))}
        for i in range(start, stop):
            if bool(rows_b[i]) != bool(rows_c[i]):
                mismatches.append((i, cases[i], rows_b[i], rows_c[i]))
        done = stop
        print(f"progress {done}/{total} mismatches={len(mismatches)} "
              f"elapsed={time.time()-started:.1f}s", flush=True)
    con.close()
    print("FULL SQL baseline != candidate:", len(mismatches))
    for m in mismatches[:50]:
        print("  MISMATCH", repr(m[1])[:200], m[2], m[3])
    (HERE / "full_sql_result.json").write_text(json.dumps({
        "total": total, "mismatches": [
            {"index": m[0], "value_repr": repr(m[1]) if m[1] is not None else None,
             "baseline": m[2], "candidate": m[3]} for m in mismatches]}, indent=1))
    print("done", time.time() - started)


if __name__ == "__main__":
    main()
