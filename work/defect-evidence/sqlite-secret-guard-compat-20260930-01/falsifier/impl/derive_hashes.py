"""Independently re-derive every _PHASE5_SCHEMA_SHA256 entry from a fresh DB.

Run from repo root:
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python \
      work/defect-evidence/.../falsifier/impl/derive_hashes.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from bots5.infrastructure.persistence import sqlite as s
from tests._authority_test_support import upgrade_database

SCRATCH = Path(__file__).resolve().parent / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB = SCRATCH / "fresh_candidate.sqlite3"
if DB.exists():
    DB.unlink()

upgrade_database(DB)

import sqlite3

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT name, sql FROM sqlite_master WHERE name IN ({})".format(
        ",".join("?" for _ in s._PHASE5_SCHEMA_OBJECTS)
    ),
    s._PHASE5_SCHEMA_OBJECTS,
).fetchall()
conn.close()

derived = {
    str(name): hashlib.sha256(
        s._normalise_sql_fragment(str(sql or "")).encode("utf-8")
    ).hexdigest()
    for name, sql in rows
}

declared = s._PHASE5_SCHEMA_SHA256

print("objects in _PHASE5_SCHEMA_OBJECTS:", len(s._PHASE5_SCHEMA_OBJECTS))
print("objects found in fresh sqlite_master:", len(derived))
print("entries in _PHASE5_SCHEMA_SHA256:", len(declared))

mismatches = []
for name in s._PHASE5_SCHEMA_OBJECTS:
    d = derived.get(name)
    e = declared.get(name)
    if d != e:
        mismatches.append((name, e, d))
print("MISMATCH COUNT:", len(mismatches))
for name, e, d in mismatches:
    print("MISMATCH", name, "declared=", e, "derived=", d)

extra_declared = set(declared) - set(s._PHASE5_SCHEMA_OBJECTS)
extra_found = set(derived) - set(s._PHASE5_SCHEMA_OBJECTS)
print("declared-but-not-checked:", sorted(extra_declared))
print("checked-but-not-declared:", sorted(extra_found))

repaired = [
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
]
print("--- four repaired trigger hashes (derived) ---")
for name in repaired:
    print(name, derived[name], "match" if derived[name] == declared[name] else "MISMATCH")

# Full manifest of every object's derived hash, for the record.
(SCRATCH / "derived_hashes.json").write_text(
    json.dumps({"count": len(derived), "hashes": derived}, indent=2, sort_keys=True)
)
sys.exit(1 if mismatches else 0)
