"""Generate baseline + candidate predicate SQL as standalone SELECT bodies.

Writes:
  baseline_expr.sql / candidate_expr.sql  -- the raw predicate expression (for column `v`)
  baseline_probe.sql / candidate_probe.sql -- `SELECT v, (expr) FROM t;`
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "work/defect-evidence/sqlite-secret-guard-compat-20260930-01/scratch"))

import bots5.core.secrets as baseline  # noqa: E402
import candidate_secrets as candidate  # noqa: E402

OUT = Path(__file__).resolve().parent

b = baseline.secret_key_forbidden_sql_expression("v")
c = candidate.secret_key_forbidden_sql_expression("v")

(OUT / "baseline_expr.sql").write_text(b)
(OUT / "candidate_expr.sql").write_text(c)
(OUT / "baseline_probe.sql").write_text(f"SELECT v, ({b}) FROM t;")
(OUT / "candidate_probe.sql").write_text(f"SELECT v, ({c}) FROM t;")

print("baseline len", len(b))
print("candidate len", len(c))


def depth(s: str) -> int:
    best = cur = 0
    for ch in s:
        if ch == "(":
            cur += 1
            best = max(best, cur)
        elif ch == ")":
            cur -= 1
    return best


print("baseline depth", depth(b))
print("candidate depth", depth(c))
print("baseline replace count", b.count("replace("))
print("candidate replace count", c.count("replace("))
