#!/usr/bin/env python
"""Oracle v3 probe P7 — V-5 classification matrix and V-6 write ordering, EXECUTED.

  P7a  cancelled-before-dispatch evidence-v2 synthesis  -> NOT_APPLICABLE, 0 warnings,
       no warning containing "dispatched"
  P7b  version-1 run (no evidence_version marker)       -> LEGACY_UNVERIFIED (NOT
       NOT_APPLICABLE), executed directly rather than via a test's slack assertion
  P7c  genuinely DISPATCHED v2 attempt, absent provenance -> UNVERIFIABLE + warning
  P7d  create=False fault-injected output write          -> durable metadata UN-advanced
  P7e  create=False happy path round-trip                -> succeeded + readable artifact
  P7f  create=True overwrite refusal                     -> run dir byte-identical

Run:
  cd <repo> && PYTHONPATH=src .venv314/bin/python <this file>
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from bots5.core.campaign import project_run
from bots5.models import StageRecord, StageState
from bots5.storage import (
    RunDirs,
    StorageError,
    attempt_paths,
    create_run_tree,
    persist_stage_attempt,
)

REPORT: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    REPORT.append(line)


def sha_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _base_v2_run(run_dir: Path, *, state: str = "succeeded") -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "job.resolved.json").write_text(
        json.dumps({"synthesis": {"id": "synth", "depends_on": ["w1", "w2"]}}),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "state": state,
            "evidence_version": 2,
            "stage_order": ["w1", "w2", "synth"],
        }),
        encoding="utf-8",
    )
    (run_dir / "selection.json").write_text(
        json.dumps({"schema_version": 1, "run_id": run_dir.name,
                    "selected_attempts": {"w1": 1, "w2": 1, "synth": 1}}),
        encoding="utf-8",
    )
    stages = run_dir / "stages"
    stages.mkdir()
    for stage_id in ("w1", "w2", "synth"):
        stage = {
            "stage_id": stage_id,
            "provider": "openrouter",
            "requested_model": f"model-{stage_id}",
            "state": "succeeded",
            "attempt_number": 1,
            "started_at": "2026-09-30T00:00:00Z",
            "ended_at": "2026-09-30T00:00:01Z",
        }
        (stages / f"{stage_id}.att1.json").write_text(json.dumps(stage), encoding="utf-8")
        (stages / f"{stage_id}.att1.md").write_text(f"{stage_id} output", encoding="utf-8")


def p7a(tmp: Path) -> None:
    say("=" * 78)
    say("P7a — cancelled BEFORE dispatch (evidence-v2, started_at: null, no provenance)")
    say("=" * 78)
    run_dir = tmp / "p7a-cancelled-before-dispatch"
    _base_v2_run(run_dir, state="cancelled")
    synth_path = run_dir / "stages" / "synth.att1.json"
    synth = json.loads(synth_path.read_text(encoding="utf-8"))
    synth["state"] = "failed"
    synth["started_at"] = None
    synth["failure"] = {
        "type": "cancelled",
        "message": "run cancelled before the stage reached a terminal state",
        "provider_side_outcome_unknown": False,
    }
    synth.pop("consumed_dependencies", None)
    synth.pop("dependency_digests", None)
    synth_path.write_text(json.dumps(synth), encoding="utf-8")

    raw = json.loads(synth_path.read_text(encoding="utf-8"))
    say(f"  raw synth record           : state={raw['state']} "
        f"failure.type={raw['failure']['type']} started_at={raw['started_at']!r} "
        f"provenance keys present: {[k for k in ('consumed_dependencies','dependency_digests') if k in raw]}")

    projection = project_run(run_dir)
    warnings = list(projection.integrity_warnings)
    say(f"  synthesis_freshness        : {projection.synthesis_freshness}")
    say(f"  integrity_warnings ({len(warnings)})   : {warnings}")
    say(f"  any warning mentions 'dispatched': {'dispatched' in ' '.join(warnings)}")
    say(f"  freshness report           : {json.dumps(projection.synthesis_freshness_report, sort_keys=True)}")
    ok = (
        projection.synthesis_freshness == "NOT_APPLICABLE"
        and warnings == []
        and "dispatched" not in " ".join(warnings)
    )
    say(f"  P7a RESULT: {'PASS' if ok else 'FAIL'}")
    say()


def p7b(tmp: Path) -> None:
    say("=" * 78)
    say("P7b — version-1 run, NO evidence_version marker")
    say("=" * 78)
    run_dir = tmp / "p7b-legacy-v1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_dir.name,
        "state": "succeeded",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "synth"],
        "stages": {},
        "usage": {},
    }), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text("{}", encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    stages = run_dir / "stages"
    stages.mkdir()
    # (a) a *succeeded* legacy synthesis
    (stages / "w1.json").write_text(json.dumps({
        "stage_id": "w1", "provider": "openrouter", "requested_model": "model-w1",
        "state": "succeeded", "output_path": "stages/w1.md",
        "usage": {"total_tokens": 10}}), encoding="utf-8")
    (stages / "synth.json").write_text(json.dumps({
        "stage_id": "synth", "provider": "openrouter", "requested_model": "model-synth",
        "state": "succeeded", "output_path": "stages/synth.md",
        "usage": {"total_tokens": 20}}), encoding="utf-8")
    (stages / "w1.md").write_text("w1 output", encoding="utf-8")
    (stages / "synth.md").write_text("synth output", encoding="utf-8")

    projection = project_run(run_dir, hosted=False)
    say(f"  evidence_version           : {projection.evidence_version}")
    say(f"  synthesis_freshness        : {projection.synthesis_freshness}")
    say(f"  integrity_warnings         : {list(projection.integrity_warnings)}")
    ok = projection.synthesis_freshness == "LEGACY_UNVERIFIED"
    say(f"  P7b RESULT (succeeded legacy): {'PASS' if ok else 'FAIL'}")

    # (b) a legacy run whose synthesis FAILED and never started -> must NOT become
    #     NOT_APPLICABLE through the new started_at key: version-1 has no marker.
    synth = json.loads((stages / "synth.json").read_text(encoding="utf-8"))
    synth["state"] = "failed"
    synth["started_at"] = None          # a v1 writer never wrote this, but be adversarial
    synth["failure"] = {"type": "cancelled", "message": "x"}
    (stages / "synth.json").write_text(json.dumps(synth), encoding="utf-8")
    projection2 = project_run(run_dir, hosted=False)
    say(f"  (adversarial) synthesis_freshness with started_at:null on a V1 record: "
        f"{projection2.synthesis_freshness}")
    ok2 = projection2.synthesis_freshness == "LEGACY_UNVERIFIED"
    say(f"  P7b RESULT (started_at-null on v1): {'PASS (still LEGACY_UNVERIFIED)' if ok2 else 'FAIL'}")
    say()


def p7c(tmp: Path) -> None:
    say("=" * 78)
    say("P7c — genuinely DISPATCHED evidence-v2 attempt with ABSENT provenance")
    say("=" * 78)
    run_dir = tmp / "p7c-dispatched-no-provenance"
    _base_v2_run(run_dir, state="succeeded")
    synth_path = run_dir / "stages" / "synth.att1.json"
    synth = json.loads(synth_path.read_text(encoding="utf-8"))
    synth.pop("consumed_dependencies", None)
    synth.pop("dependency_digests", None)
    synth_path.write_text(json.dumps(synth), encoding="utf-8")
    raw = json.loads(synth_path.read_text(encoding="utf-8"))
    say(f"  raw synth record           : state={raw['state']} started_at={raw['started_at']!r} "
        f"provenance keys present: {[k for k in ('consumed_dependencies','dependency_digests') if k in raw]}")
    projection = project_run(run_dir)
    warnings = list(projection.integrity_warnings)
    say(f"  synthesis_freshness        : {projection.synthesis_freshness}")
    say(f"  integrity_warnings         : {warnings}")
    ok = projection.synthesis_freshness == "UNVERIFIABLE" and warnings != []
    say(f"  P7c RESULT: {'PASS (still UNVERIFIABLE with a warning)' if ok else 'FAIL'}")
    say()


def _record(stage_id: str, state: StageState) -> StageRecord:
    return StageRecord(
        id=stage_id,
        provider="openrouter",
        requested_model="model-w1",
        state=state,
    )


def p7d(tmp: Path) -> None:
    say("=" * 78)
    say("P7d — create=False: fault-injected OUTPUT write leaves metadata UN-advanced")
    say("=" * 78)
    dirs = create_run_tree(tmp / "runs", "p7d-run-20260101T000000Z-00000001")
    meta_path = dirs.stages / "w1.att1.json"
    persist_stage_attempt(dirs, _record("w1", StageState.QUEUED), attempt_number=1, create=True)
    persist_stage_attempt(dirs, _record("w1", StageState.RUNNING), attempt_number=1)
    before = meta_path.read_bytes()
    say(f"  metadata before fault      : state={json.loads(before)['state']} "
        f"output_path={json.loads(before)['output_path']!r}")

    # fault injection on the output write only
    import bots5.storage as storage
    real_write = storage.atomic_write_text

    def fault(path, text):  # noqa: ANN001
        raise StorageError(f"injected fault writing {path}")

    storage.atomic_write_text = fault
    try:
        persist_stage_attempt(
            dirs, _record("w1", StageState.SUCCEEDED), attempt_number=1,
            text="worker output", create=False,
        )
        raised = None
    except StorageError as exc:
        raised = exc
    finally:
        storage.atomic_write_text = real_write

    after = meta_path.read_bytes()
    say(f"  exception raised           : {raised!r}")
    say(f"  metadata byte-identical    : {after == before}")
    say(f"  metadata after fault       : state={json.loads(after)['state']} "
        f"output_path={json.loads(after)['output_path']!r}")
    say(f"  output artifact exists     : {(dirs.root / 'stages' / 'w1.att1.md').exists()}")
    ok = raised is not None and after == before and json.loads(after)["state"] == "running"
    say(f"  P7d RESULT: {'PASS' if ok else 'FAIL'}")
    say()


def p7e(tmp: Path) -> None:
    say("=" * 78)
    say("P7e — create=False happy path round-trip")
    say("=" * 78)
    dirs = create_run_tree(tmp / "runs2", "p7e-run-20260101T000000Z-00000002")
    persist_stage_attempt(dirs, _record("w1", StageState.QUEUED), attempt_number=1, create=True)
    persist_stage_attempt(dirs, _record("w1", StageState.RUNNING), attempt_number=1)
    persist_stage_attempt(dirs, _record("w1", StageState.SUCCEEDED), attempt_number=1,
                          text="final output", create=False)
    meta = json.loads((dirs.stages / "w1.att1.json").read_text(encoding="utf-8"))
    out = dirs.root / "stages" / "w1.att1.md"
    say(f"  metadata state             : {meta['state']}  output_path={meta['output_path']!r}")
    say(f"  artifact readable          : {out.is_file()} content={out.read_text()!r}")
    ok = meta["state"] == "succeeded" and out.read_text() == "final output"
    say(f"  P7e RESULT: {'PASS' if ok else 'FAIL'}")
    say()


def p7f(tmp: Path) -> None:
    say("=" * 78)
    say("P7f — create=True overwrite refusal leaves the run dir BYTE-IDENTICAL")
    say("=" * 78)
    dirs = create_run_tree(tmp / "runs3", "p7f-run-20260101T000000Z-00000003")
    persist_stage_attempt(dirs, _record("w1", StageState.SUCCEEDED), attempt_number=1,
                          text="first", create=True)
    before = sha_tree(dirs.root)
    try:
        persist_stage_attempt(dirs, _record("w1", StageState.SUCCEEDED), attempt_number=1,
                              text="second", create=True)
        raised = None
    except StorageError as exc:
        raised = exc
    after = sha_tree(dirs.root)
    say(f"  refusal raised             : {raised!r}")
    say(f"  files before               : {sorted(before)}")
    say(f"  files after                : {sorted(after)}")
    say(f"  byte-identical             : {before == after}")
    say(f"  content still original     : "
        f"{(dirs.root / 'stages' / 'w1.att1.md').read_text()!r}")
    ok = raised is not None and before == after
    say(f"  P7f RESULT: {'PASS' if ok else 'FAIL'}")


def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v3-p7-"))
    p7a(tmp)
    p7b(tmp)
    p7c(tmp)
    p7d(tmp)
    p7e(tmp)
    p7f(tmp)
    say()
    say("=" * 78)
    say("SUMMARY")
    say("=" * 78)
    for line in list(REPORT):  # snapshot: say() appends
        if "RESULT" in line:
            say("  " + line.strip())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(99)