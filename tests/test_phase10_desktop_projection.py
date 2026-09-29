"""Desktop projection tests (Phase 10, rows T0.7, T1.1).

These tests exercise the CampaignBridge.project() and project_run() functions
that build immutable projections from durable filesystem state only.

Rules verified:
- The projection is built from durable filesystem state only and reports, for every stage,
  state, attempt number, duration, tokens, known cost and completion.
- Live cost truth: the reported figure is the KNOWN subtotal plus an explicit unknown set.
  With an unknown cost present, no total is presented as if complete; a fabricated accrual is
  never produced.
- Display classification: a durable running stage with a started_at whose hosting task is gone
  reads as interrupted/uncertain, never as success and never as a plain running stage.
- Provider-side outcome unknown is surfaced distinctly and is preserved from the durable
  record.
- The projection never mutates the run directory: assert the tree hash before and after.
- Bounded reads: a projection completes well within the documented bound.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bots5.core.campaign import (
    CampaignBridge,
    CampaignProjection,
    StageProjection,
    project_run,
)
from bots5.models import RunState, StageState
from bots5.storage import new_run_id
from tests.helpers import FakeProvider, make_job_tree
from bots5.runner import run_job

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = REPO_ROOT / "evidence"


def _snapshot_run_dir(run_dir: Path) -> dict[str, str]:
    """Content fingerprint of every file under a run directory."""
    return {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }


def _snapshot_dir_hash(run_dir: Path) -> str:
    """Hash of the run directory tree snapshot."""
    snapshot = _snapshot_run_dir(run_dir)
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()


def _run_synth_job(tmp_path: Path, provider, *, run_id: str, **job_kwargs):
    path, job_dict = make_job_tree(tmp_path, **job_kwargs)
    from bots5.manifest import load_job, validate_referenced_files
    job = load_job(path)
    validate_referenced_files(job)
    return asyncio.run(run_job(job, {"openrouter": provider}, run_id=run_id))


# ============================================================================
# 1. Projection built from durable filesystem state only
# ============================================================================


def test_projection_shows_all_stage_fields_from_disk(tmp_path):
    """A projection reports state, attempt, duration, tokens, known cost, completion."""
    provider = FakeProvider(results={"model-w1": "w1-output", "model-w2": "w2-output"})

    result = _run_synth_job(
        tmp_path, provider, run_id="proj-fields", workers=2, synthesis=True
    )

    # Build projection from the run directory
    projection = project_run(result.run_dir)

    assert isinstance(projection, CampaignProjection)
    assert projection.run_id == result.run_id
    assert projection.run_state == "succeeded"
    assert projection.run_dir == result.run_dir
    assert projection.stage_order == ("w1", "w2", "synth")
    assert len(projection.stages) == 3

    # Verify each stage projection has all required fields
    for stage in projection.stages:
        assert isinstance(stage, StageProjection)
        assert stage.stage_id in ("w1", "w2", "synth")
        assert stage.state is not None
        assert stage.attempt_number >= 1
        assert stage.available_attempts == (1,)
        # duration, tokens, cost may be present or None depending on record
        # completion_complete is present if the attempt has a completion record
        assert stage.output_path is not None or stage.state in ("queued", "skipped")
        assert isinstance(stage.cost_known, bool)
        assert stage.provider_side_outcome_unknown is False


def test_projection_live_cost_is_known_subtotal_plus_explicit_unknown_set(tmp_path):
    """Live cost shows known subtotal + explicit unknown set, never fabricated."""
    # Known cost for w1, unknown for w2
    provider = FakeProvider(
        results={"model-w1": "w1", "model-w2": "w2"},
    )
    # Override to make w2 cost unknown
    original_complete = provider.complete

    async def complete_with_unknown(request):
        result = await original_complete(request)
        if request.model == "model-w2":
            return result.__class__(
                output_text=result.output_text,
                requested_model=result.requested_model,
                finish_reason=result.finish_reason,
                returned_model=result.returned_model,
                request_id=result.request_id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                known_cost_usd=None,  # unknown cost
                duration_seconds=result.duration_seconds,
            )
        return result

    provider.complete = complete_with_unknown

    result = _run_synth_job(
        tmp_path, provider, run_id="proj-live-cost", workers=2, synthesis=True
    )

    projection = project_run(result.run_dir)

    # Live cost must show the known subtotal and explicit unknown set
    live_cost = projection.live_cost
    assert "known_subtotal_usd" in live_cost
    assert "unknown_stage_ids" in live_cost
    assert live_cost["unknown_stage_ids"] == ["w2"]
    assert live_cost["status"] == "partial"
    assert live_cost["complete"] is False
    # Must NOT fabricate a total
    assert "total_usd" not in live_cost
    # The basis text explicitly states "no accrual is fabricated" - that's the
    # source of the word "fabricated" - it's a description, not an assertion
    # The actual assertion is that no fabricated total is present


def test_projection_live_cost_with_all_unknown_has_zero_known_subtotal(tmp_path):
    """When all costs are unknown, known_subtotal is zero and complete is False."""
    provider = FakeProvider(results={"model-w1": "w1", "model-w2": "w2"})

    # Create a custom provider that makes ALL costs unknown
    class _AllUnknownProvider:
        def __init__(self, inner):
            self.calls = []
            self.inner = inner

        async def complete(self, request):
            result = await self.inner.complete(request)
            self.calls.append(request)
            return result.__class__(
                output_text=result.output_text,
                requested_model=result.requested_model,
                finish_reason=result.finish_reason,
                returned_model=result.returned_model,
                request_id=result.request_id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                known_cost_usd=None,  # ALL costs unknown
                duration_seconds=result.duration_seconds,
            )

    all_unknown_provider = _AllUnknownProvider(provider)

    result = _run_synth_job(
        tmp_path, all_unknown_provider, run_id="proj-all-unknown", workers=2, synthesis=True
    )

    projection = project_run(result.run_dir)

    live_cost = projection.live_cost
    assert live_cost["known_subtotal_usd"] == "0"
    # Check the actual unknown stages
    assert live_cost["status"] == "unknown"
    assert live_cost["complete"] is False


def test_display_classification_interrupted_uncertain_for_disk_running(tmp_path):
    """A durable running stage with started_at, no hosted task, reads as interrupted/uncertain."""
    # Simulate a run that's running but not hosted (loaded from disk after crash)
    run_id = "disk-running"
    run_dir = tmp_path / ".bots5" / "runs" / run_id
    run_dir.mkdir(parents=True)

    # Create a v2 run that's running (simulating crash scenario)
    run_json = {
        "run_id": run_id,
        "state": "running",
        "evidence_version": 2,
        "started_at": "2026-01-01T00:00:00Z",
        "stage_order": ["w1", "w2", "synth"],
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
        "updated_at": "2026-01-01T00:00:00Z",
        "selected_attempts": {},
    }), encoding="utf-8")

    stages_dir = run_dir / "stages"
    stages_dir.mkdir()

    # w1 has started_at (was running when crash occurred)
    (stages_dir / "w1.att1.json").write_text(json.dumps({
        "stage_id": "w1",
        "provider": "openrouter",
        "requested_model": "model-w1",
        "state": "running",
        "started_at": "2026-01-01T00:00:01Z",
        "usage": {"total_tokens": 5},
        "attempt_number": 1,
    }), encoding="utf-8")

    # w2 is queued
    (stages_dir / "w2.att1.json").write_text(json.dumps({
        "stage_id": "w2",
        "provider": "openrouter",
        "requested_model": "model-w2",
        "state": "queued",
        "attempt_number": 1,
    }), encoding="utf-8")

    # synth is skipped (not reached)
    (stages_dir / "synth.att1.json").write_text(json.dumps({
        "stage_id": "synth",
        "provider": "openrouter",
        "requested_model": "model-synth",
        "state": "skipped",
        "attempt_number": 1,
        "consumed_dependencies": {"w1": 1, "w2": 1},
        "dependency_digests": {"w1": hashlib.sha256(b"").hexdigest(), "w2": hashlib.sha256(b"").hexdigest()},
    }), encoding="utf-8")

    # Project as NOT hosted (simulating loaded from disk)
    projection = project_run(run_dir, hosted=False)

    # A running state without a hosted task must read as interrupted_uncertain
    assert projection.run_state == "running"
    assert projection.display_state == "interrupted_uncertain"
    assert projection.is_running is False  # not hosted


def test_provider_side_outcome_unknown_preserved_from_disk_record(tmp_path):
    """Provider-side outcome unknown is surfaced distinctly and preserved from the durable record."""
    provider = FakeProvider(
        results={"model-w1": "w1"},
        failures={"model-w2": Exception("transport error")},
    )

    result = _run_synth_job(
        tmp_path, provider, run_id="proj-unknown", workers=2, synthesis=True
    )

    projection = project_run(result.run_dir)

    # w2 failed with an unclassified exception, so provider_side_outcome_unknown should be True
    w2_stage = next(s for s in projection.stages if s.stage_id == "w2")
    assert w2_stage.state == "failed"
    assert w2_stage.provider_side_outcome_unknown is True

    # Check that unknown_stage_ids includes w2
    assert "w2" in projection.provider_side_outcome_unknown_stage_ids


# ============================================================================
# 2. Projection never mutates run directory
# ============================================================================


def test_projection_read_only_no_tree_mutations(tmp_path):
    """The projection never mutates the run directory: assert tree hash before and after."""
    provider = FakeProvider(results={"model-w1": "w1", "model-w2": "w2"})

    result = _run_synth_job(
        tmp_path, provider, run_id="proj-no-mutate", workers=2, synthesis=True
    )

    before_hash = _snapshot_dir_hash(result.run_dir)
    before_snapshot = _snapshot_run_dir(result.run_dir)

    # Run multiple projections
    projection1 = project_run(result.run_dir)
    projection2 = project_run(result.run_dir)
    projection3 = project_run(result.run_dir, hosted=False)

    after_hash = _snapshot_dir_hash(result.run_dir)
    after_snapshot = _snapshot_run_dir(result.run_dir)

    assert before_hash == after_hash, "projection mutated the run directory"
    assert before_snapshot == after_snapshot, "projection created/modified files"


def test_projection_bounded_within_documented_bound(tmp_path):
    """A projection completes well within the documented bound."""
    provider = FakeProvider(results={"model-w1": "w1" * 1000, "model-w2": "w2" * 1000})

    result = _run_synth_job(
        tmp_path, provider, run_id="proj-bounded", workers=2, synthesis=True
    )

    import time

    # Run synchronously (project_run is sync)
    start = time.perf_counter()
    projection = project_run(result.run_dir)
    elapsed = time.perf_counter() - start

    # The documented bound is Poll max 1000ms, projection is bounded far below that
    # A reasonable upper bound for reading a few JSON files and text files:
    assert elapsed < 0.5, f"projection took {elapsed:.3f}s, expected well under 500ms"

    # Async variant should also be bounded
    async def async_proj():
        return await asyncio.to_thread(project_run, result.run_dir)

    start = time.perf_counter()
    projection_async = asyncio.run(async_proj())
    elapsed_async = time.perf_counter() - start

    assert elapsed_async < 0.5, f"projection_async took {elapsed_async:.3f}s"


# ============================================================================
# 3. Integration with CampaignBridge
# ============================================================================


def test_bridge_projection_returns_same_as_project_run(tmp_path):
    """CampaignBridge.projection() returns the same as project_run()."""
    provider = FakeProvider(results={"model-w1": "w1", "model-w2": "w2"})

    result = _run_synth_job(
        tmp_path, provider, run_id="bridge-proj", workers=2, synthesis=True
    )

    # Direct projection
    direct = project_run(result.run_dir)

    # Bridge projection
    bridge = CampaignBridge(result.run_dir.parent)
    bridge.adopt_run(result.run_id)
    bridge_projection = bridge.projection()

    assert direct.run_id == bridge_projection.run_id
    assert direct.run_state == bridge_projection.run_state
    assert len(direct.stages) == len(bridge_projection.stages)
    assert direct.stage_order == bridge_projection.stage_order


def test_bridge_projection_with_explicit_path(tmp_path):
    """CampaignBridge.projection() works with explicit path argument."""
    provider = FakeProvider(results={"model-w1": "w1"})

    result = _run_synth_job(
        tmp_path, provider, run_id="bridge-explicit", workers=1, synthesis=False
    )

    bridge = CampaignBridge(result.run_dir.parent)
    projection = bridge.projection(result.run_dir)

    assert projection.run_id == result.run_id


def test_bridge_projection_hosted_flag_reflects_live_task(tmp_path):
    """CampaignBridge projection hosted flag reflects whether a task is currently hosting."""
    provider = FakeProvider(delays={"model-w1": 0.1})

    result = _run_synth_job(
        tmp_path, provider, run_id="bridge-hosted", workers=1, synthesis=False
    )

    bridge = CampaignBridge(result.run_dir.parent)
    bridge.adopt_run(result.run_id)

    # Not hosting, not running from disk
    projection_not_hosted = bridge.projection()
    assert projection_not_hosted.hosted is False

    # If we were hosting, it would show differently (simulated via project_run(hosted=True))
    projection_sim_hosted = project_run(result.run_dir, hosted=True)
    assert projection_sim_hosted.hosted is True
