"""Produce a small, human-auditable raw evidence table of baseline vs candidate
SQL results plus the Python reference, including attempted counterexamples."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / "src"))
import bots5.core.secrets as baseline  # noqa: E402

EXPR_B = (HERE / "baseline_expr.sql").read_text()
EXPR_C = (HERE / "candidate_expr.sql").read_text()

SAMPLES = [
    None, "", "token", "TOKEN", "Token", "nekto", "enotk", "otken",
    "to!ken", "t_o-k.e n", "to ken", "apricotKey", "token1", "1token",
    "tokenx", "xtoken", "tokén", "tok\u00e9n", "password",
    "pa\u00dfword", "pa\u1e9eword", "pa\u017fsword", "pa\u1e98ssord",
    "pass\u1e98ord", "to\u212aen", "api\u0130ey", "toke\u0149",
    "acces\ufb05oken", "refre\u017fhtoken", "secret\u0307", "password\u00e9",
    "p\u00e9ssword", "password\x00x", "\x00token", "to\x00ken",
    "password\x00", "\x00", "token\n", "token\t", "token ",
    "a" * 100000, "token" + "!" * 50000, "!" * 100000 + "token",
    "\U0001f642token", "token\U0001f642",
]

con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE t(v)")
con.executemany("INSERT INTO t(v) VALUES (?)", [(s,) for s in SAMPLES])
rows = list(con.execute(f"SELECT rowid, ({EXPR_B}), ({EXPR_C}) FROM t"))
con.close()

lines = ["| # | value (repr, truncated) | baseline SQL | candidate SQL | Python | agree |",
         "|---|---|---|---|---|---|"]
for rid, b, c in rows:
    v = SAMPLES[rid - 1]
    py = baseline.is_forbidden_secret_key(v)
    agree = "yes" if bool(b) == bool(c) else "**NO**"
    vr = repr(v)
    if len(vr) > 60:
        vr = vr[:57] + "..."
    vr = vr.replace("|", "\\|")
    lines.append(f"| {rid} | `{vr}` | {b} | {c} | {py} | {agree} |")
out = "\n".join(lines)
(HERE / "evidence_table.md").write_text(out + "\n")
print(out)
