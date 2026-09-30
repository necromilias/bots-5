"""Build a pristine pre-repair database and diff its Phase 5 schema hashes against
the candidate's. Expect exactly the four forbidden-predicate triggers to differ.

Run from repo root with the BASELINE tree first on PYTHONPATH:
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=work/baseline-t0/src:. .venv314/bin/python \
      work/.../falsifier/impl/baseline_hashes.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import bots5
from bots5.infrastructure.persistence import sqlite as s
from tests._authority_test_support import upgrade_database

print("bots5 source:", bots5.__file__)
assert "baseline-t0" in bots5.__file__, "baseline tree is not first on PYTHONPATH"

SCRATCH = Path(__file__).resolve().parent / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB = SCRATCH / "fresh_baseline.sqlite3"
if DB.exists():
    DB.unlink()
upgrade_database(DB)

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT name, sql FROM sqlite_master WHERE name IN ({})".format(
        ",".join("?" for _ in s._PHASE5_SCHEMA_OBJECTS)
    ),
    s._PHASE5_SCHEMA_OBJECTS,
).fetchall()
tnames = [
    r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()
]
conn.close()

derived = {
    str(name): hashlib.sha256(
        s._normalise_sql_fragment(str(sql or "")).encode("utf-8")
    ).hexdigest()
    for name, sql in rows
}

declared = s._PHASE5_SCHEMA_SHA256
print("baseline declared entries:", len(declared), "derived:", len(derived))
base_mismatch = {n: (declared.get(n), derived.get(n)) for n in declared if declared.get(n) != derived.get(n)}
print("baseline's own declared-vs-derived mismatches:", base_mismatch)

# candidate hashes derived earlier by derive_hashes.py
cand = json.loads((SCRATCH / "derived_hashes.json").read_text())["hashes"]
differing = sorted(n for n in derived if derived[n] != cand.get(n))
print("objects whose SQL differs candidate-vs-baseline:", len(differing))
for n in differing:
    print("  DIFF", n)
expected = {
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
}
print("differs as expected:", set(differing) == expected)
print("all trigger names present:", all(n in tnames for n in expected))
print("total triggers in baseline db:", len(tnames))
sys.exit(0 if set(differing) == expected and not base_mismatch else 1)
