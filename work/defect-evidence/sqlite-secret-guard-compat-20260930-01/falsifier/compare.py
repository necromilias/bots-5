"""Compare baseline vs candidate predicates.

Stage 1: exact Python mirror over the full adversarial corpus.
Stage 2: real SQLite (3.50.4 via venv python) evaluation of both SQL predicates
         on the targeted corpus + disagreements + a random sample.
Stage 3: candidate SQL on isolated SQLite 3.45.1 (ctypes) for the same subset.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_cases  # noqa: E402

EXPR_B = (HERE / "baseline_expr.sql").read_text()
EXPR_C = (HERE / "candidate_expr.sql").read_text()


def run(cases):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v)")
    con.executemany("INSERT INTO t(id, v) VALUES (?, ?)", list(enumerate(cases)))
    rows_b = {i: r for i, r in con.execute(f"SELECT id, ({EXPR_B}) FROM t")}
    rows_c = {i: r for i, r in con.execute(f"SELECT id, ({EXPR_C}) FROM t")}
    con.close()
    return rows_b, rows_c


def main() -> None:
    cases = json.loads((HERE / "cases.json").read_text())
    print("cases loaded", len(cases))

    mirror_b = [gen_cases.pred_sql(c, True) for c in cases]
    mirror_c = [gen_cases.pred_sql(c, False) for c in cases]
    pyref = [gen_cases.pred_python(c) for c in cases]

    mirror_disagree = [i for i in range(len(cases)) if mirror_b[i] != mirror_c[i]]
    print("mirror baseline != candidate:", len(mirror_disagree))
    for i in mirror_disagree[:20]:
        print("  DISAGREE", repr(cases[i]), mirror_b[i], mirror_c[i])

    vs_py_b = [i for i in range(len(cases)) if bool(mirror_b[i]) != pyref[i]]
    vs_py_c = [i for i in range(len(cases)) if bool(mirror_c[i]) != pyref[i]]
    print("mirror baseline != python:", len(vs_py_b),
          "(false positives:", sum(1 for i in vs_py_b if mirror_b[i]), ")")
    print("mirror candidate != python:", len(vs_py_c),
          "(false positives:", sum(1 for i in vs_py_c if mirror_c[i]), ")")

    # ---- SQL subset ----
    rng = random.Random(7)
    targeted_count = len(gen_cases.targeted())
    subset_idx = list(range(min(targeted_count, len(cases))))
    subset_idx += mirror_disagree
    subset_idx += [i for i in range(len(cases)) if i < 3000]  # exhaustive head
    subset_idx += rng.sample(range(len(cases)), 3000)
    subset_idx = sorted(set(subset_idx))
    sub_cases = [cases[i] for i in subset_idx]
    print("SQL subset size", len(sub_cases))

    sq_b, sq_c = run(sub_cases)
    bad_mirror = []
    bad_equiv = []
    for pos, original_index in enumerate(subset_idx):
        case = sub_cases[pos]
        mb = mirror_b[original_index]
        mc = mirror_c[original_index]
        sb = sq_b[pos]
        sc = sq_c[pos]
        sb_bool = bool(sb)
        sc_bool = bool(sc)
        if sb_bool != bool(mb) or (mc is not None and sc_bool != bool(mc)):
            bad_mirror.append((case, mb, mc, sb, sc))
        if sb_bool != sc_bool:
            bad_equiv.append((case, sb, sc, mb, mc))

    print("mirror-vs-SQL mismatches:", len(bad_mirror))
    for row in bad_mirror[:20]:
        print("   MISMATCH", repr(row[0]), "mirror", row[1], row[2], "sql", row[3], row[4])
    print("SQL baseline != SQL candidate:", len(bad_equiv))
    for row in bad_equiv[:40]:
        print("   EQFAIL", repr(row[0]), "baseline", row[1], "candidate", row[2],
              "mirror", row[3], row[4])

    # dump SQL evidence rows to a JSON file
    evidence = []
    for pos, original_index in enumerate(subset_idx):
        evidence.append({
            "value_repr": repr(sub_cases[pos]) if sub_cases[pos] is not None else None,
            "baseline_sql": sq_b[pos],
            "candidate_sql": sq_c[pos],
            "mirror_baseline": mirror_b[original_index],
            "mirror_candidate": mirror_c[original_index],
            "python": pyref[original_index],
        })
    (HERE / "sql_evidence_350.json").write_text(json.dumps(evidence, indent=1))
    print("wrote sql_evidence_350.json")

    (HERE / "mirror_vs_python_summary.txt").write_text(
        f"cases={len(cases)}\n"
        f"mirror_disagree={len(mirror_disagree)}\n"
        f"mirror_baseline_vs_python={len(vs_py_b)} fp={sum(1 for i in vs_py_b if mirror_b[i])}\n"
        f"mirror_candidate_vs_python={len(vs_py_c)} fp={sum(1 for i in vs_py_c if mirror_c[i])}\n"
        f"sqlish_mirror_mismatch={len(bad_mirror)}\n"
        f"sql_equivalence_failures={len(bad_equiv)}\n"
    )


if __name__ == "__main__":
    main()
