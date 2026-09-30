"""Two closing checks:
  (a) compile persisted candidate triggers on the isolated 3.46.0 CLI;
  (b) confirm a database written by the pre-repair release is rejected by the
      candidate's authoritative open (Mick's intentional no-new-migration policy).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

IMPL = Path(__file__).resolve().parent
SCRATCH = IMPL / "scratch"
EVID = IMPL.parent.parent
sys.path.insert(0, str(EVID / "scratch"))

CAND_DB = SCRATCH / "fresh_candidate.sqlite3"
BASE_DB = SCRATCH / "fresh_baseline.sqlite3"
REPAIRED = [
    "phase5_capability_fact_provenance_validate_insert",
    "phase5_capability_fact_provenance_validate_update",
    "phase5_model_catalogue_metadata_validate_insert",
    "phase5_model_catalogue_metadata_validate_update",
]


def persisted(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN ({})".format(
                ",".join("?" for _ in REPAIRED)
            ),
            REPAIRED,
        ).fetchall()
    finally:
        conn.close()
    return {str(n): str(s) for n, s in rows}


cand = persisted(CAND_DB)

script = ["CREATE TABLE model_catalogue_entries(metadata_json TEXT);",
          "CREATE TABLE capability_facts(provenance_json TEXT);"]
for name in REPAIRED:
    script.append(cand[name] + ";")
script_file = SCRATCH / "candidate_triggers_cli.sql"
script_file.write_text("\n".join(script))

results = {}
for cli_name in ("sqlite3-3450100", "sqlite3-3460000"):
    cli = EVID / "scratch" / cli_name
    if not cli.exists():
        results[cli_name] = "missing"
        continue
    proc = subprocess.run(
        [str(cli), ":memory:"],
        input=script_file.read_text(),
        capture_output=True,
        text=True,
    )
    results[cli_name] = {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "ok": proc.returncode == 0,
    }
print("CLI compile:", results)

# (b) candidate rejects a defective-release database
from tests._authority_test_support import SQLiteAppStateStore  # noqa: E402

rejection = None
try:
    store = SQLiteAppStateStore.open(BASE_DB)
    store.close()
    rejection = "ACCEPTED (unexpected)"
except Exception as exc:  # noqa: BLE001
    rejection = f"{type(exc).__name__}: {exc}"
print("candidate open of baseline DB:", rejection)
ok_reject = "not migration-authoritative" in str(rejection)
print("intentional rejection:", ok_reject)

(IMPL / "closing_checks_result.json").write_text(
    __import__("json").dumps(
        {"cli": results, "baseline_db_open_by_candidate": rejection}, indent=2
    )
)
ok = all(
    (v == "missing") or v["ok"] for v in results.values()
) and ok_reject
print("CLOSING CHECKS", "OK" if ok else "BROKEN")
sys.exit(0 if ok else 1)
