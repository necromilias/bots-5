"""Implementation-level audit of the repaired predicate.

Covers: persisted trigger SQL markers/messages, expression shape, stage-limit
robustness (1..20 -> varying stage counts), alias/paren balance, and a runtime
proof that the correlated EXISTS sees the NEW row per-row.

Run from repo root:
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python \
      work/.../falsifier/impl/implementation_audit.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from bots5.core import secrets as candidate
from bots5.infrastructure.persistence import sqlite as s
from tests._authority_test_support import upgrade_database

SCRATCH = Path(__file__).resolve().parent / "scratch"
OUT = Path(__file__).resolve().parent
DB = SCRATCH / "fresh_candidate.sqlite3"

report: dict = {}

# --- 1. markers and abort messages in persisted trigger SQL ------------------
conn = sqlite3.connect(DB)
trigger_sql = {
    r[0]: str(r[1])
    for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
}
repaired = [
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
]
marker_results = {}
for name in repaired:
    sql = trigger_sql[name]
    folded = sql.casefold()
    declared = s._PHASE5_TRIGGER_MARKERS[name]
    present = all(m in folded for m in declared)
    # extract RAISE(ABORT, '...') message
    import re

    msg = re.search(r"raise\s*\(\s*abort\s*,\s*'((?:''|[^'])*)'\s*\)", sql, re.I)
    marker_results[name] = {
        "markers_present": present,
        "declared_markers": list(declared),
        "abort_message": msg.group(1) if msg else None,
        "length": len(sql),
    }
report["trigger_markers"] = marker_results
print("marker/message audit:", json.dumps(marker_results, indent=2))

# --- 2. fresh-DB authoritative startup revalidation --------------------------
from bots5.infrastructure.persistence import sqlite as s_mod  # noqa: E402

calls = {"schema": 0, "behavior": 0}
_orig_schema = s_mod._validate_phase5_schema
_orig_behavior = s_mod._validate_phase5_trigger_behavior


def _counting_schema(connection):
    calls["schema"] += 1
    return _orig_schema(connection)


def _counting_behavior(connection):
    calls["behavior"] += 1
    return _orig_behavior(connection)


s_mod._validate_phase5_schema = _counting_schema
s_mod._validate_phase5_trigger_behavior = _counting_behavior
try:
    from tests._authority_test_support import SQLiteAppStateStore as _TestStore

    store = _TestStore.open(DB)
    store.close()
finally:
    s_mod._validate_phase5_schema = _orig_schema
    s_mod._validate_phase5_trigger_behavior = _orig_behavior

report["validate_counts"] = calls
report["authoritative_open"] = "passed"
print("authoritative open invoked validators:", calls)
assert calls["schema"] >= 1 and calls["behavior"] >= 1, calls

# --- 3. expression shape -----------------------------------------------------
EXPR = candidate.secret_key_forbidden_sql_expression("new.metadata_json")


def nesting(sql: str) -> int:
    depth = mx = 0
    in_str = False
    i = 0
    while i < len(sql):
        c = sql[i]
        if c == "'":
            if in_str and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
        elif not in_str and c == "(":
            depth += 1
            mx = max(mx, depth)
        elif not in_str and c == ")":
            depth -= 1
        i += 1
    return mx


report["expression"] = {
    "bytes": len(EXPR),
    "nesting_depth": nesting(EXPR),
    "replace_calls": EXPR.count("replace("),
    "stages": EXPR.count("_bots5_casefold_stage_"),
    "has_ascii_nonalnum_constant": hasattr(candidate, "_ASCII_NON_ALNUM_CODES"),
    "alias_": candidate._FORBIDDEN_KEY_SQL_ALIAS,
    "stage_limit": candidate._CASE_FOLD_STAGE_LIMIT,
}
print("expression shape:", json.dumps(report["expression"], indent=2))

# --- 4. stage-limit robustness across 1..25 ----------------------------------
orig_limit = candidate._CASE_FOLD_STAGE_LIMIT
corpus = [
    "password", "pass-word", "PASSWORD", "api\u00dfkey", "AP\u0130-KEY",
    "api\u212aey", "token", "tok en", "apricotKey", "", "\x00token",
    "\ufb03x", "secret", "s-e-c-r-e-t", "apikeyvalue", "a" * 200,
    "client\u017fsecret", "authorization", "refresh token",
]
stage_results = {}
for limit in [1, 2, 3, 5, 9, 10, 18, 19, 20, 25]:
    candidate._CASE_FOLD_STAGE_LIMIT = limit
    expr = candidate.secret_key_forbidden_sql_expression("v")
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t(v TEXT)")
        c.executemany("INSERT INTO t(v) VALUES (?)", ((x,) for x in corpus))
        got = {r[0] for r in c.execute(f"SELECT v FROM t WHERE {expr}").fetchall()}
        c.close()
        # reference verdict from python normalization
        ref = {x for x in corpus if candidate.normalize_secret_key(x) in candidate._FORBIDDEN_SECRET_KEYS}
        # SQL also fails closed on NUL and over-rejects anagrams; only require no
        # forbidden-key miss and no syntax error.
        missed = ref - got
        stage_results[limit] = {
            "stages": limit is not None and expr.count("_bots5_casefold_stage_"),
            "nesting": nesting(expr),
            "missed_forbidden": sorted(missed),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        stage_results[limit] = {"error": f"{type(exc).__name__}: {exc}"}
candidate._CASE_FOLD_STAGE_LIMIT = orig_limit
report["stage_limit_robustness"] = stage_results
print("stage-limit robustness:", json.dumps(stage_results, indent=2))

# --- 5. correlated EXISTS sees each NEW row independently --------------------
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, key TEXT)")
conn.execute(
    "CREATE TRIGGER g BEFORE INSERT ON probe "
    f"WHEN {candidate.secret_key_forbidden_sql_expression('new.key')} "
    "BEGIN SELECT RAISE(ABORT, 'forbidden'); END"
)
rows = ["benign", "token", "apricotKey", "password", "key", "secret"]
verdicts = {}
for r in rows:
    try:
        conn.execute("INSERT INTO probe(key) VALUES (?)", (r,))
        verdicts[r] = "accepted"
    except sqlite3.IntegrityError:
        verdicts[r] = "rejected"
    conn.rollback()
conn.close()
report["per_row_correlation"] = verdicts
print("per-row correlation:", verdicts)
expected = {"benign": "accepted", "token": "rejected", "apricotKey": "accepted",
            "password": "rejected", "key": "accepted", "secret": "rejected"}
report["per_row_correlation_ok"] = verdicts == expected
print("per-row correlation OK:", verdicts == expected)

# --- 6. internal alias collision probe --------------------------------------
# Column literally named like an internal alias must still work (no leakage).
collide = candidate.secret_key_forbidden_sql_expression("_bots5_casefold_0")
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE t(_bots5_casefold_0 TEXT)")
c.executemany("INSERT INTO t VALUES (?)", [("token",), ("benign",)])
got = {r[0] for r in c.execute(f"SELECT _bots5_casefold_0 FROM t WHERE {collide}").fetchall()}
c.close()
report["alias_collision_probe"] = sorted(got)
print("alias collision probe (expect ['token']):", sorted(got))

(OUT / "implementation_audit.json").write_text(json.dumps(report, indent=2))
print("wrote", OUT / "implementation_audit.json")
