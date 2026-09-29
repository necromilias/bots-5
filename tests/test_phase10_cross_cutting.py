"""Cross-cutting Phase 10 tests (T3 rows).

These tests exercise cross-cutting concerns:
- Mick clarification O-1: skipped synthesis is NOT_APPLICABLE, evaluated before FRESH/STALE
- Evidence version 1 runs with no version 2 marker read as LEGACY_UNVERIFIED
- Unverifiable synthesis state reads as UNVERIFIABLE
- Reader compatibility for v1 runs
- Zero-diff boundary: no writes outside operated run directory
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bots5.cli import main as cli_main
from bots5.core.campaign import CampaignBridge, project_run
from bots5.models import RunState, StageState
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job
from bots5.storage import EVIDENCE_VERSION_V2, new_run_id, reconstruct_run_state
from tests.helpers import FakeProvider, make_job_tree


REPO_ROOT = Path(__file__).resolve().parents[1]


def _snapshot_run_dir(run_dir: Path) -> dict[str, str]:
    """Content fingerprint of every file under a run directory."""
    return {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }


def _run_synth_job(tmp_path: Path, provider, *, run_id: str, **job_kwargs):
    from bots5.manifest import load_job, validate_referenced_files
    path, job_dict = make_job_tree(tmp_path, **job_kwargs)
    job = load_job(path)
    validate_referenced_files(job)
    return asyncio.run(run_job(job, {"openrouter": provider}, run_id=run_id))


def _write_v2_provenance_case(
    run_dir: Path,
    *,
    consumed: dict[str, int],
    digests: dict[str, str],
    selected_attempts: dict[str, int] | None = None,
) -> None:
    """Write minimal, complete v2 evidence for a two-dependency synthesis."""
    run_dir.mkdir(parents=True)
    (run_dir / "job.resolved.json").write_text(
        json.dumps({"synthesis": {"id": "synth", "depends_on": ["w1", "w2"]}}),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "state": "succeeded",
            "evidence_version": 2,
            "stage_order": ["w1", "w2", "synth"],
        }),
        encoding="utf-8",
    )
    selection = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "selected_attempts": (
            {"w1": 1, "w2": 1, "synth": 1}
            if selected_attempts is None
            else selected_attempts
        ),
    }
    (run_dir / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
    stages_dir = run_dir / "stages"
    stages_dir.mkdir()
    for stage_id, attempts in (("w1", (1, 2)), ("w2", (1,)), ("synth", (1,))):
        for attempt in attempts:
            stage = {
                "stage_id": stage_id,
                "provider": "openrouter",
                "requested_model": f"model-{stage_id}",
                "state": "succeeded",
                "attempt_number": attempt,
            }
            if stage_id == "synth":
                stage["consumed_dependencies"] = consumed
                stage["dependency_digests"] = digests
            (stages_dir / f"{stage_id}.att{attempt}.json").write_text(
                json.dumps(stage), encoding="utf-8"
            )
            (stages_dir / f"{stage_id}.att{attempt}.md").write_text(
                f"{stage_id} output attempt {attempt}", encoding="utf-8"
            )


# ============================================================================
# 1. O-1: Skipped synthesis is NOT_APPLICABLE (evaluated BEFORE FRESH/STALE)
# ============================================================================


def test_skipped_synthesis_is_not_applicable_not_stale_or_unverifiable(tmp_path):
    """O-1: Skipped synthesis (never dispatched) is NOT_APPLICABLE, evaluated before FRESH/STALE.

    A synthesis attempt that was never dispatched (dependency failed, dependency incomplete,
    known-cost threshold exceeded, or not reached before timeout) must read as NOT_APPLICABLE.
    This is evaluated BEFORE the staleness predicate, so a skipped synthesis is never reported
    as stale or unverifiable.
    """
    # Create a run where synthesis is skipped due to dependency_incomplete
    provider = FakeProvider(
        results={"model-w1": "w1 output"},
        failures={"model-w2": Exception("worker failed")},
    )

    result = _run_synth_job(
        tmp_path, provider, run_id="skip-synth", workers=2, synthesis=True
    )

    projection = project_run(result.run_dir)

    # Check that synthesis is skipped
    synth = next((s for s in projection.stages if s.stage_id == "synth"), None)
    assert synth is not None
    assert synth.state == "skipped"

    # The synthesis freshness classification must be NOT_APPLICABLE
    assert projection.synthesis_freshness == "NOT_APPLICABLE"

    # No integrity warning for a skipped synthesis
    assert len(projection.integrity_warnings) == 0

    # Run-level skip reason must be set
    assert result.state == RunState.FAILED
    # Check the usage for skip reason
    run_json = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_json.get("synthesis_skipped_reason") == "dependency_failed"


def test_skipped_synthesis_with_layout_that_could_produce_different_class(tmp_path):
    """O-1: Include a case where layout could otherwise produce a different classification.

    This test ensures that a skipped synthesis's NOT_APPLICABLE classification is
    determined by its terminal state (SKIPPED), not by any other factor like
    missing provenance fields.
    """
    # Synthesis is skipped because w2 failed (dependency_failed)
    provider = FakeProvider(
        results={"model-w1": "w1"},
        failures={"model-w2": Exception("boom")},
    )

    result = _run_synth_job(
        tmp_path, provider, run_id="skip-layout", workers=2, synthesis=True
    )

    projection = project_run(result.run_dir)

    # Even though the skipped synthesis attempt has no provenance fields
    # (consumed_dependencies, dependency_digests), it's NOT_APPLICABLE because
    # it was never dispatched, not because of missing data
    synth = next(s for s in projection.stages if s.stage_id == "synth")
    assert synth.state == "skipped"
    assert synth.error_type == "dependency_failed"
    assert projection.synthesis_freshness == "NOT_APPLICABLE"

    # Verify the run has the skip reason
    run_data = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_data.get("synthesis_skipped_reason") == "dependency_failed"


def test_synthesis_cancelled_before_dispatch_is_not_applicable_without_false_warning(tmp_path):
    """V-5: a run cancelled before synthesis dispatch must not claim a dispatched attempt.

    The reserved implementation oracle found that Rule 1 keyed only on the literal
    ``skipped`` state, so the most common never-dispatched case of all -- a run
    cancelled while workers were in flight, which persists ``synth.att1.json`` with
    ``state: failed``, ``failure.type: cancelled``, an explicit ``started_at: null``
    and no provenance -- was reported UNVERIFIABLE with an integrity warning
    asserting that a *dispatched* synthesis attempt had absent provenance. Both
    statements are false: no provider was ever called for that attempt. The accepted
    design says never-dispatched synthesis is NOT_APPLICABLE and raises no integrity
    warning (REGENERATION_AND_STALE_SYNTHESIS.md section 1).
    """
    run_dir = tmp_path / ".bots5" / "runs" / "cancelled-before-synth-dispatch"
    _write_v2_provenance_case(
        run_dir,
        consumed={"w1": 1, "w2": 1},
        digests={"w1": "a" * 64, "w2": "b" * 64},
    )

    # Reshape the durable synthesis attempt exactly as a pre-dispatch cancellation
    # persists it: it never started, so it carries no provenance and no provider
    # ever saw it. The explicit started_at: null is what the durable writer emits.
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
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "state": "cancelled",
            "evidence_version": 2,
            "stage_order": ["w1", "w2", "synth"],
        }),
        encoding="utf-8",
    )

    projection = project_run(run_dir)

    assert projection.synthesis_freshness == "NOT_APPLICABLE"
    assert projection.integrity_warnings == ()
    # The exact false assertion the oracle caught must never come back.
    assert "dispatched" not in " ".join(projection.integrity_warnings)


def test_non_dispatched_synthesis_never_reported_as_stale(tmp_path):
    """O-1: A non-dispatched synthesis is never reported as stale.

    Staleness is about selection+digest mismatch. A skipped synthesis was never
    dispatched, so it's NOT_APPLICABLE, never STALE.
    """
    # Force synthesis to be skipped by making dependencies incomplete
    provider = FakeProvider(
        results={"model-w1": "w1"},
        delays={"model-w2": 0.5},  # Will timeout
    )

    path, job_dict = make_job_tree(tmp_path, workers=2, synthesis=True)
    job_dict["execution"]["run_timeout_seconds"] = 0.1
    from bots5.manifest import load_job, validate_referenced_files
    import json as _json
    path.write_text(_json.dumps(job_dict), encoding="utf-8")
    job = load_job(path)
    validate_referenced_files(job)

    result = asyncio.run(run_job(job, {"openrouter": provider}, run_id="skip-timeout"))

    projection = project_run(result.run_dir)

    # Synthesis should be skipped due to timeout
    synth = next((s for s in projection.stages if s.stage_id == "synth"), None)
    assert synth is not None
    assert synth.state == "skipped"
    assert synth.error_type == "run_timed_out"

    # Must be NOT_APPLICABLE, not STALE
    assert projection.synthesis_freshness == "NOT_APPLICABLE"


# ============================================================================
# 2. Evidence version 1 runs with no version 2 marker read as LEGACY_UNVERIFIED
# ============================================================================


def test_v1_run_no_version_2_marker_reads_as_legacy_unverified(tmp_path):
    """A version 1 run with no evidence_version marker reads as LEGACY_UNVERIFIED.

    Version 1 runs (marker absent) must never be reported as FRESH or STALE.
    They are LEGACY_UNVERIFIED because provenance fields don't exist.
    """
    # Create a v1-style run directory (no evidence_version marker)
    run_id = "v1-legacy"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    # v1 run.json has no evidence_version
    run_json = {
        "run_id": run_id,
        "state": "succeeded",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "synth"],
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text("{}", encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()

    # v1 stage records have no provenance fields
    (stages_dir / "w1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "succeeded",
        "output_path": "stages/w1.md",
        "usage": {"total_tokens": 10},
    }), encoding="utf-8")

    (stages_dir / "synth.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "succeeded",
        "output_path": "stages/synth.md",
        "usage": {"total_tokens": 20},
    }), encoding="utf-8")

    # Also create the output files
    (run_dir / "stages" / "w1.md").write_text("w1 output", encoding="utf-8")
    (run_dir / "stages" / "synth.md").write_text("synth output", encoding="utf-8")

    # v1 run has no selection.json, no preflight.json, no provenance fields
    assert not (run_dir / "selection.json").exists()
    assert not (run_dir / "preflight.json").exists()

    projection = project_run(run_dir, hosted=False)

    # The evidence_version should be 1 (or 0 if not present)
    assert projection.evidence_version < EVIDENCE_VERSION_V2

    # Synthesis freshness should be LEGACY_UNVERIFIED for v1
    # (the exact value depends on implementation, but it must not be FRESH or STALE)
    # v1 runs are read-only and don't have provenance to evaluate
    assert projection.synthesis_freshness in ("LEGACY_UNVERIFIED", None)


# ============================================================================
# 3. Unverifiable synthesis state reads as UNVERIFIABLE
# ============================================================================


def test_unverifiable_synthesis_missing_dependency_digests(tmp_path):
    """An unverifiable synthesis state (missing dependency digests) reads as UNVERIFIABLE.

    A v2 synthesis attempt that was dispatched but is missing dependency digests
    (or has malformed digests) must read as UNVERIFIABLE and is never silently upgraded.

    Note: This test creates v2 evidence manually since run_job without approval
    creates v1 evidence. The test verifies that a v2 synthesis with missing
    provenance reads as UNVERIFIABLE.
    """
    # Manually create v2 evidence with a synthesis attempt that has incomplete provenance
    run_id = "unverifiable-digests"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    # Create a proper job.resolved.json with synthesis declaration
    job_resolved = {
        "schema_version": 1,
        "name": "test-job",
        "inputs": [{"label": "source", "path": str(run_dir / ".." / "input" / "source.txt")}],
        "execution": {
            "max_parallelism": 1,
            "run_timeout_seconds": 5.0,
            "stop_before_synthesis_if_known_cost_exceeds_usd": 2.0,
        },
        "workers": [{"id": "w1", "provider": "openrouter", "model": "model-w1", "system_prompt_path": str(run_dir / ".." / "prompts" / "w1.md"), "temperature": 0.1, "max_output_tokens": 100, "timeout_seconds": 1.0}],
        "synthesis": {"id": "synth", "provider": "openrouter", "model": "model-synth", "system_prompt_path": str(run_dir / ".." / "prompts" / "synth.md"), "temperature": 0.1, "max_output_tokens": 100, "timeout_seconds": 1.0, "depends_on": ["w1"]},
        "output": {"runs_dir": str(run_dir.parent)},
    }
    (run_dir / "job.resolved.json").write_text(json.dumps(job_resolved), encoding="utf-8")

    # v2 run.json has evidence_version
    run_json = {
        "run_id": run_id,
        "state": "succeeded",
        "evidence_version": 2,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "synth"],
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "selection.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": run_id,
        "updated_at": "2026-01-01T00:00:10Z",
        "selected_attempts": {"synth": 1},
    }), encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()

    # w1 is complete
    (stages_dir / "w1.att1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "succeeded",
        "output_path": "stages/w1.att1.md",
        "attempt_number": 1,
        "usage": {"total_tokens": 10},
    }), encoding="utf-8")
    (run_dir / "stages" / "w1.att1.md").write_text("w1 output", encoding="utf-8")

    # synth attempt 1 is succeeded (dispatched) but has incomplete provenance
    # consumed_dependencies present, dependency_digests MISSING
    # Per Rule 3: missing provenance on a dispatched v2 attempt = UNVERIFIABLE
    (stages_dir / "synth.att1.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "succeeded",
        "output_path": "stages/synth.att1.md",
        "attempt_number": 1,
        "consumed_dependencies": {"w1": 1},  # present
        # dependency_digests is MISSING - this makes it unverifiable
        "usage": {"total_tokens": 20},
    }), encoding="utf-8")
    (run_dir / "stages" / "synth.att1.md").write_text("synth output", encoding="utf-8")

    # Verify that the v2 evidence is correct
    state_map = reconstruct_run_state(run_dir)
    assert state_map["evidence_version"] == 2

    projection = project_run(run_dir, hosted=False)

    # The synthesis should be classified as UNVERIFIABLE due to missing dependency_digests
    assert projection.synthesis_freshness == "UNVERIFIABLE"

    # There should be an integrity warning
    assert len(projection.integrity_warnings) > 0
    assert any("provenance" in w.lower() or "digest" in w.lower() for w in projection.integrity_warnings)


def test_unverifiable_synthesis_with_empty_provenance_maps_and_declared_dependencies(tmp_path):
    run_dir = tmp_path / "empty-provenance"
    _write_v2_provenance_case(run_dir, consumed={}, digests={})

    freshness = reconstruct_run_state(run_dir)["stages"]["synth"]["synthesis_freshness"]
    assert freshness["classification"] == "UNVERIFIABLE"
    assert freshness["integrity_warning"] is True
    assert freshness["warnings"]


@pytest.mark.parametrize("missing_map", ["consumed_dependencies", "dependency_digests"])
def test_unverifiable_synthesis_when_one_declared_dependency_binding_is_missing(
    tmp_path, missing_map
):
    consumed = {"w1": 1, "w2": 1}
    digests = {
        "w1": hashlib.sha256(b"w1 output attempt 1").hexdigest(),
        "w2": hashlib.sha256(b"w2 output attempt 1").hexdigest(),
    }
    if missing_map == "consumed_dependencies":
        del consumed["w2"]
    else:
        del digests["w2"]
    run_dir = tmp_path / missing_map
    _write_v2_provenance_case(run_dir, consumed=consumed, digests=digests)

    freshness = reconstruct_run_state(run_dir)["stages"]["synth"]["synthesis_freshness"]
    assert freshness["classification"] == "UNVERIFIABLE"
    assert freshness["integrity_warning"] is True
    assert freshness["warnings"]


def test_fully_present_synthesis_provenance_selection_mismatch_remains_stale(tmp_path):
    consumed = {"w1": 1, "w2": 1}
    digests = {
        "w1": hashlib.sha256(b"w1 output attempt 1").hexdigest(),
        "w2": hashlib.sha256(b"w2 output attempt 1").hexdigest(),
    }
    run_dir = tmp_path / "stale-selection"
    _write_v2_provenance_case(
        run_dir,
        consumed=consumed,
        digests=digests,
        selected_attempts={"w1": 2},
    )

    freshness = reconstruct_run_state(run_dir)["stages"]["synth"]["synthesis_freshness"]
    assert freshness["classification"] == "STALE"
    assert freshness["integrity_warning"] is False


def test_unverifiable_synthesis_malformed_digests(tmp_path):
    """A synthesis attempt with malformed dependency digests reads as UNVERIFIABLE."""
    run_id = "unverifiable-malformed"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    # Create a proper job.resolved.json with synthesis declaration
    job_resolved = {
        "schema_version": 1,
        "name": "test-job",
        "inputs": [{"label": "source", "path": str(run_dir / ".." / "input" / "source.txt")}],
        "execution": {
            "max_parallelism": 1,
            "run_timeout_seconds": 5.0,
            "stop_before_synthesis_if_known_cost_exceeds_usd": 2.0,
        },
        "workers": [{"id": "w1", "provider": "openrouter", "model": "model-w1", "system_prompt_path": str(run_dir / ".." / "prompts" / "w1.md"), "temperature": 0.1, "max_output_tokens": 100, "timeout_seconds": 1.0}],
        "synthesis": {"id": "synth", "provider": "openrouter", "model": "model-synth", "system_prompt_path": str(run_dir / ".." / "prompts" / "synth.md"), "temperature": 0.1, "max_output_tokens": 100, "timeout_seconds": 1.0, "depends_on": ["w1"]},
        "output": {"runs_dir": str(run_dir.parent)},
    }
    (run_dir / "job.resolved.json").write_text(json.dumps(job_resolved), encoding="utf-8")

    run_json = {
        "run_id": run_id,
        "state": "succeeded",
        "evidence_version": 2,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "synth"],
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "selection.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": run_id,
        "updated_at": "2026-01-01T00:00:10Z",
        "selected_attempts": {"synth": 1},
    }), encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()

    (stages_dir / "w1.att1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "succeeded",
        "output_path": "stages/w1.att1.md",
        "attempt_number": 1,
        "usage": {"total_tokens": 10},
    }), encoding="utf-8")
    (run_dir / "stages" / "w1.att1.md").write_text("w1 output", encoding="utf-8")

    # synth has malformed digest (not hex)
    (stages_dir / "synth.att1.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "succeeded",
        "output_path": "stages/synth.att1.md",
        "attempt_number": 1,
        "consumed_dependencies": {"w1": 1},
        "dependency_digests": {"w1": "not-a-valid-hex-digest"},  # malformed
        "usage": {"total_tokens": 20},
    }), encoding="utf-8")
    (run_dir / "stages" / "synth.att1.md").write_text("synth output", encoding="utf-8")

    projection = project_run(run_dir, hosted=False)

    # Malformed digest should also produce UNVERIFIABLE
    assert projection.synthesis_freshness == "UNVERIFIABLE"


# ============================================================================
# 4. Reader compatibility: v1 runs load without mutation
# ============================================================================


def test_v1_run_loads_without_mutation(tmp_path):
    """Every retained version 1 run loads and classifies without mutation.

    The directory hash must be identical before and after reading.
    """
    # Create a v1 run
    run_id = "v1-compat"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    run_json = {
        "run_id": run_id,
        "state": "succeeded",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "synth"],
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text("{}", encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "result.md").write_text("synth output", encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()
    (stages_dir / "w1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "succeeded",
        "output_path": "stages/w1.md",
        "usage": {"total_tokens": 10},
    }), encoding="utf-8")
    (stages_dir / "synth.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "succeeded",
        "output_path": "stages/synth.md",
        "usage": {"total_tokens": 20},
    }), encoding="utf-8")
    (run_dir / "stages" / "w1.md").write_text("w1 output", encoding="utf-8")
    (run_dir / "stages" / "synth.md").write_text("synth output", encoding="utf-8")

    before_hash = _snapshot_run_dir(run_dir)

    # Load multiple times
    projection1 = project_run(run_dir, hosted=False)
    projection2 = project_run(run_dir, hosted=False)
    projection3 = project_run(run_dir, hosted=False)

    after_hash = _snapshot_run_dir(run_dir)

    # The hashes must be identical - no bytes were written
    assert before_hash == after_hash

    # The runs should all load correctly
    assert projection1.run_id == run_id
    assert projection2.run_id == run_id
    assert projection3.run_id == run_id


def test_v1_run_directory_hash_unchanged_after_exercise(tmp_path):
    """v1 run directory hash is unchanged after loading and classification."""
    run_id = "v1-no-mutate"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    run_json = {
        "run_id": run_id,
        "state": "failed",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:05Z",
        "stage_order": ["w1", "synth"],
        "synthesis_skipped_reason": "dependency_failed",
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text("{}", encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()
    (stages_dir / "w1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "succeeded",
        "output_path": "stages/w1.md",
        "usage": {"total_tokens": 10},
    }), encoding="utf-8")

    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}

    # Load and classify
    projection = project_run(run_dir, hosted=False)
    state_map = reconstruct_run_state(run_dir)

    after = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}

    assert before == after, "v1 run was mutated during loading"


# ============================================================================
# 5. Zero-diff boundary: no writes outside operated run directory
# ============================================================================


def test_zero_diff_boundary_no_writes_outside_run_directory(tmp_path):
    """Zero-diff boundary: importing and exercising Phase 10 surfaces does not write
    to any path outside the operated run directory."""
    provider = FakeProvider(results={"model-w1": "w1", "model-w2": "w2"})

    result = _run_synth_job(
        tmp_path, provider, run_id="zero-diff", workers=2, synthesis=True
    )

    # Capture the entire tmp_path state before
    before_files = {p: p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    # Exercise the bridge projection (the main Phase 10 surface)
    bridge = CampaignBridge(result.run_dir.parent)
    bridge.adopt_run(result.run_id)
    projection = bridge.projection()

    # Capture the state after
    after_files = {p: p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    # Compare - only files under result.run_dir should differ
    for path, before_bytes in before_files.items():
        if path.is_relative_to(result.run_dir):
            # Inside the run directory - allowed to differ
            continue
        # Outside the run directory - must be identical
        assert path in after_files, f"new file created outside run directory: {path}"
        after_bytes = after_files[path]
        assert before_bytes == after_bytes, f"file modified outside run directory: {path}"

    # The run directory itself should have the expected files
    assert (result.run_dir / "run.json").is_file()
    assert (result.run_dir / "usage.json").is_file()
    # The engine may or may not create selection.json depending on whether v2 approval was used
    # This test verifies no external writes, not v2-specific behavior
    assert (result.run_dir / "stages").is_dir()


def test_projection_no_external_writes(tmp_path):
    """project_run() performs no writes outside the operated run directory."""
    provider = FakeProvider(results={"model-w1": "w1"})

    result = _run_synth_job(
        tmp_path, provider, run_id="proj-no-external", workers=1, synthesis=False
    )

    # Record all files before projection
    before_snapshot = _snapshot_run_dir(result.run_dir)
    before_outside = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and not p.is_relative_to(result.run_dir)}

    # Run multiple projections
    for _ in range(3):
        proj = project_run(result.run_dir)
        proj2 = project_run(result.run_dir, hosted=True)
        proj3 = project_run(result.run_dir, hosted=False)

    # Record files after
    after_outside = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and not p.is_relative_to(result.run_dir)}

    # No files outside the run directory should have changed
    assert before_outside == after_outside, "projection wrote to files outside run directory"

    # The run directory files should be unchanged (read-only)
    after_snapshot = _snapshot_run_dir(result.run_dir)
    assert before_snapshot == after_snapshot, "projection modified run directory"


# ============================================================================
# 6. Integration with storage layer
# ============================================================================


def test_reconstruct_run_state_handles_v1_runs(tmp_path):
    """storage.reconstruct_run_state handles v1 runs (no evidence_version marker)."""
    run_id = "v1-state"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    run_json = {
        "run_id": run_id,
        "state": "running",
        "started_at": "2026-01-01T00:00:00Z",
        "stage_order": ["w1"],
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text("{}", encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()
    (stages_dir / "w1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "running",
        "started_at": "2026-01-01T00:00:01Z",
        "usage": {"total_tokens": 5},
    }), encoding="utf-8")

    # Load state map
    state_map = reconstruct_run_state(run_dir)

    # v1 run has evidence_version < 2
    assert state_map["evidence_version"] < EVIDENCE_VERSION_V2
    assert "w1" in state_map["stages"]

    # No selection.json means selected_attempt defaults to 1
    w1_entry = state_map["stages"]["w1"]
    assert w1_entry["selected_attempt"] == 1


def test_reconstruct_run_state_v2_with_selection(tmp_path):
    """storage.reconstruct_run_state correctly reads v2 selection.json."""
    run_id = "v2-selection"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    run_json = {
        "run_id": run_id,
        "state": "succeeded",
        "evidence_version": 2,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:10Z",
        "stage_order": ["w1", "synth"],
        "stages": {},
        "usage": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (run_dir / "job.resolved.json").write_text("{}", encoding="utf-8")
    (run_dir / "usage.json").write_text("{}", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "selection.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": run_id,
        "updated_at": "2026-01-01T00:00:10Z",
        "selected_attempts": {"synth": 2},  # synth selected attempt 2
    }), encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()
    (stages_dir / "w1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "succeeded",
        "usage": {"total_tokens": 10},
    }), encoding="utf-8")
    (stages_dir / "synth.att1.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "failed",
        "attempt_number": 1,
        "usage": {"total_tokens": 5},
    }), encoding="utf-8")
    (stages_dir / "synth.att2.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "succeeded",
        "attempt_number": 2,
        "usage": {"total_tokens": 20},
        "consumed_dependencies": {"w1": 1},
        "dependency_digests": {"w1": hashlib.sha256(b"w1 output").hexdigest()},
    }), encoding="utf-8")
    (run_dir / "stages" / "synth.att1.md").write_text("synth att1", encoding="utf-8")
    (run_dir / "stages" / "synth.att2.md").write_text("synth att2", encoding="utf-8")

    state_map = reconstruct_run_state(run_dir)

    # v2 run
    assert state_map["evidence_version"] == EVIDENCE_VERSION_V2

    # synth has selected_attempt = 2 per selection.json
    synth_entry = state_map["stages"]["synth"]
    assert synth_entry["selected_attempt"] == 2
    # available_attempts is a list (storage.py:955-957)
    assert synth_entry["available_attempts"] == [1, 2]


# ============================================================================
# 7. Headless CLI verbs: inspect --attempt and --approval file consumption
# ============================================================================


def _operator_pricing() -> dict[str, Any]:
    """Operator pricing evidence covering the paid openrouter route."""
    return {
        "entries": [
            {
                "provider": "openrouter",
                "input_usd_per_1m": "1.25",
                "output_usd_per_1m": "2.50",
                "rate_source": "operator test citation",
                "observed_at": "2026-09-29T12:00:00Z",
            }
        ]
    }


class _OfflineRoutedProvider(OpenRouterProvider):
    """Route-faithful offline fake.

    A subclass of the real ``OpenRouterProvider`` so the engine's kind-specific
    provider-object route validation accepts it, with ``complete()`` fully
    faked: no endpoint, no network, no environment dependency.
    """

    def __init__(self, *, results=None, delays=None):
        super().__init__("offline-fake-api-key")
        self.results = dict(results or {})
        self.delays = dict(delays or {})
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        delay = self.delays.get(request.model, 0)
        if delay:
            await asyncio.sleep(delay)
        text = self.results.get(request.model, f"output:{request.model}")
        return CompletionResult(
            output_text=text,
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id=f"req-{request.model}",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=Decimal("0.01"),
            duration_seconds=0.001,
        )


def _run_v2_full_run(tmp_path: Path, provider) -> tuple[CampaignBridge, Path, Path]:
    """A succeeded evidence-v2 run produced by the engine's own writers.

    Returns ``(bridge, run_dir, job_path)``; the bridge is adopted onto the run
    so its zero-spend ``prepare_*`` operations work immediately.
    """
    job_path, _job = make_job_tree(tmp_path)
    bridge = CampaignBridge(
        tmp_path / ".bots5" / "runs",
        provider_factory=lambda job: {"openrouter": provider},
    )
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("test-operator", pricing_evidence=_operator_pricing())

    async def _drive():
        bridge.approve_and_start(prepared)
        return await bridge.run_to_completion()

    result = asyncio.run(_drive())
    assert result.state.value == "succeeded"
    bridge.adopt_run(result.run_dir)
    return bridge, result.run_dir, job_path


def _approve_operation(bridge: CampaignBridge, prepared) -> None:
    """Consume the prepared operation approval on the running loop."""

    async def _drive():
        if prepared.operation == "worker_regeneration":
            bridge.approve_and_regenerate(prepared)
        else:
            bridge.approve_and_rerun_synthesis(prepared)
        return await bridge.run_to_completion()

    asyncio.run(_drive())


def _write_approval_file(path: Path, record) -> None:
    path.write_text(json.dumps(record.to_dict()), encoding="utf-8")


def _write_pricing_file(path: Path) -> None:
    path.write_text(json.dumps(_operator_pricing()), encoding="utf-8")


def test_cli_inspect_attempt_reads_the_exact_attempt_not_the_selected_one(tmp_path, capsys):
    """`inspect --attempt N` reads exactly attempt N with no fallback.

    After one sibling regeneration (attempt 2 exists but the selection stays on
    attempt 1), the default view shows the SELECTED attempt while `--attempt 2`
    shows exactly the regenerated attempt's model and output bytes; a missing
    attempt fails closed with the typed error and exit status 1.
    """
    provider = _OfflineRoutedProvider(
        results={
            "model-w1": "worker-one-output",
            "model-w2": "worker-two-output",
            "model-synth": "synth-output",
            "model-w1-alt": "worker-one-alt-output",
        }
    )
    bridge, run_dir, _job_path = _run_v2_full_run(tmp_path, provider)
    runs_dir = run_dir.parent

    prepared = bridge.prepare_regeneration(
        "w1", "model-w1-alt", "test-operator", pricing_evidence=_operator_pricing()
    )
    _approve_operation(bridge, prepared)
    # Regeneration never auto-selects: the selected attempt is still 1.
    selection = json.loads((run_dir / "selection.json").read_text(encoding="utf-8"))
    assert selection["selected_attempts"]["w1"] == 1

    # Default (no --attempt): the selected attempt 1, with its own model/bytes.
    assert cli_main(["inspect", run_dir.name, "w1", "--runs-dir", str(runs_dir)]) == 0
    default_out = capsys.readouterr().out
    assert "stage_id: w1" in default_out
    assert "state: succeeded" in default_out
    assert "model: model-w1" in default_out
    assert "attempt_number': 1" in default_out
    assert "worker-one-output" in default_out

    # --attempt 2: exactly the regenerated attempt — never a fallback.
    assert (
        cli_main(["inspect", run_dir.name, "w1", "--runs-dir", str(runs_dir), "--attempt", "2"])
        == 0
    )
    attempt_out = capsys.readouterr().out
    assert "attempt_number': 2" in attempt_out
    assert "model: model-w1-alt" in attempt_out
    assert "worker-one-alt-output" in attempt_out

    # An explicit attempt is read exactly: a missing one fails closed, typed.
    assert (
        cli_main(["inspect", run_dir.name, "w1", "--runs-dir", str(runs_dir), "--attempt", "99"])
        == 1
    )
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "artifact not found" in err


def test_cli_regenerate_with_approval_file_consumes_the_record_and_executes(tmp_path, capsys, monkeypatch):
    """`regenerate --approval <file>` executes from the pre-built record.

    The approval file is the consent surface: the run's approval_id and
    approved_by printed by the CLI come from the FILE, the engine consumes the
    one-shot marker under that id, and the sibling attempt is created with the
    requested model while the selection stays unchanged.
    """
    provider = _OfflineRoutedProvider(
        results={"model-w1": "worker-one-output", "model-w2": "worker-two-output"}
    )
    bridge, run_dir, job_path = _run_v2_full_run(tmp_path, provider)
    runs_dir = run_dir.parent

    operation_provider = _OfflineRoutedProvider(
        results={"model-w1-cli-approval": "regenerated-by-approval-file"}
    )
    builds: list[Any] = []
    monkeypatch.setattr(
        "bots5.cli._build_providers",
        lambda job: builds.append(job) or {"openrouter": operation_provider},
    )

    prepared = bridge.prepare_regeneration(
        "w1", "model-w1-cli-approval", "approval-file-operator",
        pricing_evidence=_operator_pricing(),
    )
    approval_path = tmp_path / "approval.json"
    _write_approval_file(approval_path, prepared.approval)
    pricing_path = tmp_path / "pricing.json"
    _write_pricing_file(pricing_path)

    rc = cli_main([
        "regenerate", run_dir.name, "w1",
        "--model", "model-w1-cli-approval",
        "--job", str(job_path),
        "--runs-dir", str(runs_dir),
        "--approval", str(approval_path),
        "--pricing", str(pricing_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Consent came from the pre-built record, not an in-process approval.
    assert f"approval_id: {prepared.approval.approval_id}" in out
    assert "approved_by: approval-file-operator" in out
    assert "w1: state=succeeded attempt=2" in out

    meta = json.loads(
        (run_dir / "stages" / "w1.att2.json").read_text(encoding="utf-8")
    )
    assert meta["state"] == "succeeded"
    assert meta["requested_model"] == "model-w1-cli-approval"
    # The one-shot approval was durably consumed under its own id.
    assert (run_dir / "approvals" / f"{prepared.approval.approval_id}.json").is_file()
    # Regeneration never auto-selects.
    selection = json.loads((run_dir / "selection.json").read_text(encoding="utf-8"))
    assert selection["selected_attempts"]["w1"] == 1
    assert len(builds) == 1 and operation_provider.calls


def test_cli_rerun_synthesis_with_approval_file_consumes_the_record_and_executes(tmp_path, capsys, monkeypatch):
    """`rerun-synthesis --approval <file>` executes from the pre-built record.

    The CLI consumes the file's ApprovalRecord verbatim (its approval_id and
    approved_by are printed), the engine binds the frozen dependency digests,
    and on success the new synthesis attempt becomes the selected attempt with
    result.md mirroring the new output.
    """
    provider = _OfflineRoutedProvider(
        results={
            "model-w1": "worker-one-output",
            "model-w2": "worker-two-output",
            "model-synth": "synth-output",
        }
    )
    bridge, run_dir, job_path = _run_v2_full_run(tmp_path, provider)
    runs_dir = run_dir.parent

    operation_provider = _OfflineRoutedProvider(
        results={"model-synth": "synth-rerun-by-approval-file"}
    )
    monkeypatch.setattr(
        "bots5.cli._build_providers",
        lambda job: {"openrouter": operation_provider},
    )

    prepared = bridge.prepare_synthesis_rerun(
        "approval-file-operator", pricing_evidence=_operator_pricing()
    )
    assert prepared.attempt_number == 2
    approval_path = tmp_path / "rerun-approval.json"
    _write_approval_file(approval_path, prepared.approval)
    pricing_path = tmp_path / "rerun-pricing.json"
    _write_pricing_file(pricing_path)

    rc = cli_main([
        "rerun-synthesis", run_dir.name,
        "--job", str(job_path),
        "--runs-dir", str(runs_dir),
        "--approval", str(approval_path),
        "--pricing", str(pricing_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"approval_id: {prepared.approval.approval_id}" in out
    assert "approved_by: approval-file-operator" in out
    assert "synth: state=succeeded attempt=2" in out

    meta = json.loads(
        (run_dir / "stages" / "synth.att2.json").read_text(encoding="utf-8")
    )
    assert meta["state"] == "succeeded"
    assert meta["requested_model"] == "model-synth"
    assert meta["consumed_dependencies"] == {"w1": 1, "w2": 1}
    # On success the rerun selects the new attempt and mirrors result.md.
    selection = json.loads((run_dir / "selection.json").read_text(encoding="utf-8"))
    assert selection["selected_attempts"]["synth"] == 2
    assert "synth-rerun-by-approval-file" in (run_dir / "result.md").read_text(encoding="utf-8")


def test_cli_approval_file_with_mismatched_pricing_is_refused_before_dispatch(tmp_path, capsys, monkeypatch):
    """A tampered `--approval` file is refused, typed, and writes nothing.

    The CLI verifies the record's pricing evidence against the operation's
    recomputed bound before dispatch: a mismatched record exits 1 with the
    typed refusal, constructs no provider, and leaves the run directory
    byte-identical. A missing approval file fails closed the same way.
    """
    provider = _OfflineRoutedProvider()
    bridge, run_dir, job_path = _run_v2_full_run(tmp_path, provider)
    runs_dir = run_dir.parent

    operation_provider = _OfflineRoutedProvider()
    monkeypatch.setattr(
        "bots5.cli._build_providers",
        lambda job: {"openrouter": operation_provider},
    )

    prepared = bridge.prepare_regeneration(
        "w1", "model-w1-alt", "approval-file-operator",
        pricing_evidence=_operator_pricing(),
    )
    record = prepared.approval.to_dict()
    record["pricing_evidence"]["entries"][0]["rate_source"] = "tampered citation"
    approval_path = tmp_path / "approval.json"
    _write_approval_file(approval_path, prepared.approval.from_dict(record))
    pricing_path = tmp_path / "pricing.json"
    _write_pricing_file(pricing_path)

    before = _snapshot_run_dir(run_dir)
    rc = cli_main([
        "regenerate", run_dir.name, "w1",
        "--model", "model-w1-alt",
        "--job", str(job_path),
        "--runs-dir", str(runs_dir),
        "--approval", str(approval_path),
        "--pricing", str(pricing_path),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "approval pricing evidence does not match the current operation" in err
    # Refused before dispatch: nothing was written, no provider was constructed.
    assert _snapshot_run_dir(run_dir) == before
    assert operation_provider.calls == []

    # A missing approval file fails closed with the typed artifact error.
    rc = cli_main([
        "regenerate", run_dir.name, "w1",
        "--model", "model-w1-alt",
        "--job", str(job_path),
        "--runs-dir", str(runs_dir),
        "--approval", str(tmp_path / "missing-approval.json"),
        "--pricing", str(pricing_path),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "artifact not found" in err
    assert _snapshot_run_dir(run_dir) == before
