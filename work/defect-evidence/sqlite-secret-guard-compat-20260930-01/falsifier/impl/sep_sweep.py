"""Exhaustive separator-position sweep: every ASCII non-alphanumeric separator at
every insertion position of every forbidden key and a benign control set, plus
every Unicode fold code point at every position. Baseline vs candidate verdicts.

Run from repo root:
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python \
      work/.../falsifier/impl/sep_sweep.py
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
spec = importlib.util.spec_from_file_location(
    "baseline_secrets_sepsweep", REPO / "work/baseline-t0/src/bots5/core/secrets.py"
)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)

from bots5.core import secrets as candidate  # noqa: E402

BASE = baseline.secret_key_forbidden_sql_expression("v")
CAND = candidate.secret_key_forbidden_sql_expression("v")
FORBIDDEN = sorted(candidate._FORBIDDEN_SECRET_KEYS)

separators = [c for c in range(128) if not chr(c).isalnum()]
fold_codes = [code for code, _ in candidate._UNICODE_CASEFOLD_ASCII_MAP]

values: set[str] = set()
for key in FORBIDDEN:
    for c in separators:
        ch = chr(c)
        for i in range(len(key) + 1):
            values.add(key[:i] + ch + key[i:])
            values.add(key[:i] + ch + ch + key[i:])
            values.add(key[:i] + ch + key[i:] + ch)
    for code in fold_codes:
        ch = chr(code)
        for i in range(len(key) + 1):
            values.add(key[:i] + ch + key[i:])
            values.add(key[:i] + ch + ch + key[i:])
for benign in ["apricotKey", "username", "key", "tokenizer", "secretive",
               "passwordless", "aipkey", "nekto", "opssward"]:
    for c in separators:
        ch = chr(c)
        for i in range(len(benign) + 1):
            values.add(benign[:i] + ch + benign[i:])

values = sorted(values)
print("sweep values:", len(values))

conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE s(v TEXT)")
conn.executemany("INSERT INTO s VALUES (?)", ((v,) for v in values))
base_true = {r[0] for r in conn.execute(f"SELECT v FROM s WHERE {BASE}")}
cand_true = {r[0] for r in conn.execute(f"SELECT v FROM s WHERE {CAND}")}
conn.close()

only_base = base_true - cand_true
only_cand = cand_true - base_true
print("baseline true:", len(base_true), "candidate true:", len(cand_true))
print("only-baseline:", len(only_base))
print("only-candidate:", len(only_cand))
for v in sorted(only_base)[:20]:
    print("  ONLY-BASELINE", repr(v))
for v in sorted(only_cand)[:20]:
    print("  ONLY-CANDIDATE", repr(v))

by_key = {k: sorted(v for v in cand_true if candidate.normalize_secret_key(v) == k)
          for k in FORBIDDEN}
missed_python = [v for v in values
                 if candidate.is_forbidden_secret_key(v) and v not in cand_true]
print("python-forbidden missed:", len(missed_python))
for v in missed_python[:20]:
    print("  MISSED", repr(v))

(REPO / "work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/impl/sep_sweep_result.json").write_text(
    json.dumps({
        "values": len(values),
        "baseline_true": len(base_true),
        "candidate_true": len(cand_true),
        "only_baseline": sorted(only_base),
        "only_candidate": sorted(only_cand),
        "missed_python": missed_python,
    }, indent=2)
)
sys.exit(1 if (only_base or only_cand or missed_python) else 0)
