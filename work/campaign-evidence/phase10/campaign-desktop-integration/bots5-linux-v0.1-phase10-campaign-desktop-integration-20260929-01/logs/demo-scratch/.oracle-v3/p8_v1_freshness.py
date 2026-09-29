#!/usr/bin/env python
"""Oracle v3 probe P8 — version-1 freshness classification, EXECUTED properly.

The repository test ``test_v1_run_no_version_2_marker_reads_as_legacy_unverified``
writes ``job.resolved.json`` as ``{}``, so ``_synthesis_declaration`` returns
``None`` and the projection's freshness is ``None`` -- the test's
``in ("LEGACY_UNVERIFIED", None)`` assertion then passes without ever observing
``LEGACY_UNVERIFIED``.  This probe therefore builds the two shapes that matter:

  P8a  a hand-built version-1 run whose job.resolved.json DECLARES synthesis
       -> must read LEGACY_UNVERIFIED (not NOT_APPLICABLE, not None)
  P8b  the same, with an adversarial explicit ``started_at: null`` on the v1
       record -> the version guard must keep it LEGACY_UNVERIFIED
  P8c  a REAL legacy engine run (run_job with no snapshot/approval, synthesis
       present) -> whatever it writes on disk, classify it and report honestly
  P8d  a REAL version-1-shaped synthetic run whose synthesis is literally
       ``state: skipped`` -> NOT_APPLICABLE (the pre-existing O-1 rule, unchanged)
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from bots5.core.campaign import project_run
from bots5.manifest import load_job, validate_referenced_files
from bots5.runner import run_job
from tests.helpers import FakeProvider, make_job_tree

OUT: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    OUT.append(line)


def build_v1(tmp: Path, *, synth_state: str = "succeeded", started_at_marker: bool = False) -> Path:
    run_dir = tmp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_dir.name,
        "state": "succeeded",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "w2", "synth"],
        "stages": {},
        "usage": {},
        # NO evidence_version key anywhere
    }), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text(json.dumps({
        "synthesis": {"id": "synth", "depends_on": ["w1", "w2"]}
    }), encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    stages = run_dir / "stages"
    stages.mkdir()
    for sid in ("w1", "w2"):
        (stages / f"{sid}.json").write_text(json.dumps({
            "stage_id": sid, "provider": "openrouter",
            "requested_model": f"model-{sid}", "state": "succeeded",
            "output_path": f"stages/{sid}.md", "usage": {"total_tokens": 10},
        }), encoding="utf-8")
        (stages / f"{sid}.md").write_text(f"{sid} out", encoding="utf-8")
    synth: dict = {
        "stage_id": "synth", "provider": "openrouter",
        "requested_model": "model-synth", "state": synth_state,
        "output_path": "stages/synth.md", "usage": {"total_tokens": 20},
    }
    if started_at_marker:
        synth["started_at"] = None
        synth["failure"] = {"type": "cancelled", "message": "x"}
    (stages / "synth.json").write_text(json.dumps(synth), encoding="utf-8")
    (stages / "synth.md").write_text("synth out", encoding="utf-8")
    assert not (run_dir / "selection.json").exists()
    assert not (run_dir / "preflight.json").exists()
    return run_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v3-p8-"))

    say("=" * 78)
    say("P8a — hand-built version-1 run, synthesis DECLARED, state succeeded")
    say("=" * 78)
    rd = build_v1(tmp / "p8a-legacy-declared")
    p = project_run(rd, hosted=False)
    say(f"  evidence_version        : {p.evidence_version}")
    say(f"  synthesis_freshness     : {p.synthesis_freshness}")
    say(f"  integrity_warnings      : {list(p.integrity_warnings)}")
    ok = p.synthesis_freshness == "LEGACY_UNVERIFIED"
    say(f"  P8a RESULT: {'PASS (LEGACY_UNVERIFIED)' if ok else 'FAIL (got %r)' % p.synthesis_freshness}")
    say()

    say("=" * 78)
    say("P8b — version-1 record WITH an adversarial explicit started_at: null")
    say("=" * 78)
    rd = build_v1(tmp / "p8b-legacy-started-null", started_at_marker=True)
    p = project_run(rd, hosted=False)
    say(f"  evidence_version        : {p.evidence_version}")
    say(f"  synthesis_freshness     : {p.synthesis_freshness}")
    say(f"  integrity_warnings      : {list(p.integrity_warnings)}")
    ok = p.synthesis_freshness == "LEGACY_UNVERIFIED"
    say(f"  P8b RESULT: {'PASS (version guard holds)' if ok else 'FAIL (got %r)' % p.synthesis_freshness}")
    say()

    say("=" * 78)
    say("P8c — REAL legacy engine run (run_job, no snapshot/approval, WITH synthesis)")
    say("=" * 78)
    sub = tmp / "p8c"
    sub.mkdir(parents=True, exist_ok=True)
    provider = FakeProvider(results={"model-w1": "w1 out", "model-w2": "w2 out",
                                     "model-synth": "synth out"})
    path, job_dict = make_job_tree(sub, workers=2, synthesis=True)
    job = load_job(path)
    validate_referenced_files(job)
    result = asyncio.run(run_job(job, {"openrouter": provider}, run_id="p8c-legacy"))
    run_json = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    resolved = json.loads((result.run_dir / "job.resolved.json").read_text(encoding="utf-8"))
    say(f"  run.json evidence_version : {run_json.get('evidence_version', '<ABSENT>')!r}")
    say(f"  run.json keys             : {sorted(run_json)}")
    say(f"  job.resolved synthesis    : {resolved.get('synthesis')!r}")
    say(f"  files in run dir          : {sorted(str(x.relative_to(result.run_dir)) for x in result.run_dir.rglob('*') if x.is_file())}")
    p = project_run(result.run_dir, hosted=False)
    say(f"  evidence_version          : {p.evidence_version}")
    say(f"  synthesis_freshness       : {p.synthesis_freshness}")
    say(f"  integrity_warnings        : {list(p.integrity_warnings)}")
    ok = p.synthesis_freshness == "LEGACY_UNVERIFIED"
    say(f"  P8c RESULT: {'PASS (LEGACY_UNVERIFIED)' if ok else 'OBSERVED: %r' % p.synthesis_freshness}")
    say()

    say("=" * 78)
    say("P8d — literal state: skipped on a version-1 record (pre-existing O-1 rule)")
    say("=" * 78)
    rd = build_v1(tmp / "p8d-v1-skipped", synth_state="skipped")
    p = project_run(rd, hosted=False)
    say(f"  evidence_version        : {p.evidence_version}")
    say(f"  synthesis_freshness     : {p.synthesis_freshness}")
    say(f"  integrity_warnings      : {list(p.integrity_warnings)}")
    ok = p.synthesis_freshness == "NOT_APPLICABLE"
    say(f"  P8d RESULT: {'PASS (unchanged O-1 rule)' if ok else 'OBSERVED: %r' % p.synthesis_freshness}")
    say()

    say("=" * 78)
    say("SUMMARY")
    say("=" * 78)
    for line in list(OUT):  # snapshot: say() appends
        if "RESULT" in line:
            say("  " + line.strip())
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        code = 99
    sys.exit(code)
