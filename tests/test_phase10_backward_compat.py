"""Gate-first backward-compatibility fixtures (Phase 10 milestone M0.0).

These tests are the regression net that must exist and pass BEFORE any
storage/models mutation (IMPLEMENTATION_SEQUENCE.md M0.0). They prove, against
the CURRENT unmodified engine, that version-1 run directories remain fully
readable and that the readers/CLI behave exactly as they do today.

Coverage map (task M0.0):
1. ``load_run_view`` / ``load_stage_view`` over a real v1 run directory;
2. ``locate_run_dir`` resolution;
3. CLI ``status`` / ``inspect`` against the same runs (disk only, no network);
4. the v1 layout is read as-is with no migration (run.json,
   stages/<stage>.json, stages/<stage>.md, result.md, usage.json,
   events.jsonl, job.resolved.json);
5. each terminal stage/run state real or synthesised fixtures can produce
   (succeeded / failed / skipped / timed_out) reads truthfully;
6. known / partial / unknown cost aggregation reads truthfully.

Fixtures:
- Real retained run directories are copied READ-ONLY from ``evidence/**``
  into pytest ``tmp_path`` (never into the repository tree, never mutated in
  place). The three retained runs used here are:
    * ``evidence/v0.1-final-worker-boundary/.../.bots5/runs/
      bots5-v0.1-final-worker-boundary-live-conformanc-20260828T233156Z-581f7132``
      (complete v1 layout: run.json, job.resolved.json, result.md, usage.json,
      events.jsonl, stages/*.json, stages/*.md — the post-telemetry V0.2 shape),
    * ``evidence/v0.1-live-conformance/bots5-v0.1-live-conformance-canary-20260828T105057Z-266e14c8``
      (older V0 shape: no ``completion`` key on stage records),
    * ``evidence/v0.1-completion-telemetry/bots5-v0.1-live-conformance-canary-20260828T125203Z-7214f83a``
      (failed run with a skipped synthesis).
- Other cases are synthesised to the exact v1 writer shape using the engine's
  own writers (``runner.run_job`` with deterministic fake providers — the same
  ``tests/helpers.py`` fake used by the existing suite). No network, no
  provider endpoints, no dependency installs (see ``tests/conftest.py`` for
  the autouse socket block).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from bots5.cli import main
from bots5.errors import ProviderError, ValidationError
from bots5.events import EVENT_TYPES
from bots5.manifest import load_job, validate_referenced_files
from bots5.paths import locate_run_dir
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.runner import run_job
from bots5.storage import load_run_view, load_stage_view

from .helpers import FakeProvider, make_job_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = REPO_ROOT / "evidence"

# Retained v1 run directories (never written to; copied into tmp_path).
GOLDEN_BOUNDARY_RUN = (
    EVIDENCE_ROOT
    / "v0.1-final-worker-boundary"
    / "bots5-v0.1-final-worker-boundary-canary-20260828T233010Z"
    / "canary"
    / ".bots5"
    / "runs"
    / "bots5-v0.1-final-worker-boundary-live-conformanc-20260828T233156Z-581f7132"
)
GOLDEN_OLD_SHAPE_RUN = (
    EVIDENCE_ROOT
    / "v0.1-live-conformance"
    / "bots5-v0.1-live-conformance-canary-20260828T105057Z-266e14c8"
)
GOLDEN_FAILED_RUN = (
    EVIDENCE_ROOT
    / "v0.1-completion-telemetry"
    / "bots5-v0.1-live-conformance-canary-20260828T125203Z-7214f83a"
)

BOUNDARY_RUN_ID = "bots5-v0.1-final-worker-boundary-live-conformanc-20260828T233156Z-581f7132"
OLD_SHAPE_RUN_ID = "bots5-v0.1-live-conformance-canary-20260828T105057Z-266e14c8"
FAILED_RUN_ID = "bots5-v0.1-live-conformance-canary-20260828T125203Z-7214f83a"


def _copy_evidence_run(tmp_path: Path, source: Path) -> Path:
    """Copy a retained evidence run read-only into tmp_path (never evidence/**)."""
    assert source.is_dir(), f"missing retained evidence run fixture: {source}"
    assert EVIDENCE_ROOT in source.parents, f"{source} is not under evidence/**"
    target = tmp_path / source.name
    shutil.copytree(source, target)
    return target


def _snapshot_run_dir(run_dir: Path) -> dict[str, str]:
    """Content fingerprint of every file under a run directory."""
    return {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }


def _assert_v1_markers(run: dict, usage: dict, run_dir: Path) -> None:
    """The directory must be unmistakably version 1: no v2 marker, no v2 artifacts."""
    assert "evidence_version" not in run
    assert "evidence_version" not in usage
    assert not (run_dir / "selection.json").exists()
    assert not (run_dir / "preflight.json").exists()
    assert not (run_dir / "approvals").exists()
    assert not list((run_dir / "stages").glob("*.att*"))


def _assert_v1_layout_files(run_dir: Path, stages: list[dict], *, with_result_md: bool) -> None:
    expected = ["run.json", "usage.json", "events.jsonl", "job.resolved.json"]
    if with_result_md:
        expected.append("result.md")
    for name in expected:
        assert (run_dir / name).is_file(), f"missing v1 artifact: {name}"
    for stage in stages:
        stage_id = stage["stage_id"]
        meta_json = run_dir / "stages" / f"{stage_id}.json"
        meta_md = run_dir / "stages" / f"{stage_id}.md"
        assert meta_json.is_file(), f"missing stage metadata: {meta_json.name}"
        if stage.get("output_path"):
            # v1 writers emit stages/<id>.md exactly when output_path was recorded.
            assert stage["output_path"] == f"stages/{stage_id}.md"
            assert meta_md.is_file(), f"missing stage output: {meta_md.name}"
        else:
            assert not meta_md.exists(), f"unexpected stage output: {meta_md.name}"


def _run_synth_job(tmp_path: Path, provider, *, run_id: str, **job_kwargs):
    path, job_dict = make_job_tree(tmp_path, **job_kwargs)
    job = load_job(path)
    validate_referenced_files(job)
    return asyncio.run(run_job(job, {"openrouter": provider}, run_id=run_id))


# ---------------------------------------------------------------------------
# 1. Golden v1 run directory through load_run_view / load_stage_view
# ---------------------------------------------------------------------------


def test_golden_v0_run_directory_loads_through_load_run_view(tmp_path):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_BOUNDARY_RUN)

    run, stages, usage = load_run_view(run_dir)

    assert run["run_id"] == BOUNDARY_RUN_ID
    assert run["state"] == "succeeded"
    assert run["synthesis_skipped_reason"] is None
    assert run["stage_order"] == ["extractor", "analyst", "adversary", "synthesis"]
    assert [stage["stage_id"] for stage in stages] == run["stage_order"]
    assert run["stages"] == {stage["stage_id"]: stage for stage in stages}
    assert run["usage"] == usage["aggregate"]

    for stage in stages:
        assert stage["state"] == "succeeded"
        assert stage["cost_known"] is True
        assert stage["completion"] == {"finish_reason": "stop", "complete": True}
        assert stage["failure"] is None
    assert {s["stage_id"]: s["cost_usd"] for s in stages} == {
        "extractor": "0.0003814",
        "analyst": "0.000429",
        "adversary": "0.00414975",
        "synthesis": "0.0063375",
    }

    assert usage["aggregate"]["cost_status"] == "known"
    assert usage["aggregate"]["cost_usd_known_sum"] == "0.01129765"
    assert usage["aggregate"]["cost_complete"] is True
    assert usage["aggregate"]["unknown_cost_stage_ids"] == []
    assert usage["aggregate"]["total_tokens_known_sum"] == 5360
    _assert_v1_markers(run, usage, run_dir)
    _assert_v1_layout_files(run_dir, stages, with_result_md=True)

    # The result.md mirror is the synthesis stage output (single-leaf contract).
    assert (run_dir / "result.md").read_text(encoding="utf-8") == (
        run_dir / "stages" / "synthesis.md"
    ).read_text(encoding="utf-8")


def test_golden_v0_stage_directory_loads_through_load_stage_view(tmp_path):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_BOUNDARY_RUN)

    for stage_id in ("extractor", "analyst", "adversary", "synthesis"):
        meta, output = load_stage_view(run_dir, stage_id)

        assert meta["stage_id"] == stage_id
        assert meta["state"] == "succeeded"
        assert meta["requested_model"]
        assert meta["output_path"] == f"stages/{stage_id}.md"
        assert output is not None
        assert output == (run_dir / "stages" / f"{stage_id}.md").read_text(encoding="utf-8")

    # Stage-order independence: a stage read directly still resolves its output.
    meta, output = load_stage_view(run_dir, "adversary")
    assert meta["returned_model"] == "google/gemini-3.7-flash"
    assert output.startswith("#")
    assert "Adversarial Review Report" in output


def test_retained_v0_run_without_completion_telemetry_reads_truthfully(tmp_path):
    """Older V0 shape: stage records carry no ``completion`` key at all."""
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_OLD_SHAPE_RUN)

    run, stages, usage = load_run_view(run_dir)

    assert run["run_id"] == OLD_SHAPE_RUN_ID
    assert run["state"] == "succeeded"
    assert run["stage_order"] == ["extractor", "analyst", "adversary", "synthesis"]
    for stage in stages:
        assert stage["state"] == "succeeded"
        assert "completion" not in stage  # the reader must not fabricate telemetry
        assert stage["cost_known"] is True
    assert usage["aggregate"]["cost_usd_known_sum"] == "0.004089100"
    assert usage["aggregate"]["cost_status"] == "known"
    assert usage["aggregate"]["cost_complete"] is True
    _assert_v1_markers(run, usage, run_dir)


def test_retained_failed_run_reads_skipped_synthesis_truthfully(tmp_path):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_FAILED_RUN)

    run, stages, usage = load_run_view(run_dir)

    assert run["state"] == "failed"
    assert run["synthesis_skipped_reason"] == "dependency_incomplete"
    by_id = {stage["stage_id"]: stage for stage in stages}
    assert by_id["extractor"]["state"] == "succeeded"
    assert by_id["analyst"]["state"] == "succeeded"
    # An incomplete worker output still reads truthfully as succeeded-but-incomplete.
    assert by_id["adversary"]["state"] == "succeeded"
    assert by_id["adversary"]["completion"] == {"finish_reason": "length", "complete": False}

    synthesis = by_id["synthesis"]
    assert synthesis["state"] == "skipped"
    assert synthesis["output_path"] is None
    assert synthesis["failure"] == {
        "type": "dependency_incomplete",
        "message": "synthesis dependency did not complete normally",
        "provider_side_outcome_unknown": False,
    }

    meta, output = load_stage_view(run_dir, "synthesis")
    assert meta["state"] == "skipped"
    assert output is None
    meta, output = load_stage_view(run_dir, "adversary")
    assert output == (run_dir / "stages" / "adversary.md").read_text(encoding="utf-8")

    assert usage["aggregate"]["cost_status"] == "known"
    assert usage["aggregate"]["cost_usd_known_sum"] == "0.002133175"
    assert usage["stages"]["synthesis"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "cost_usd": "0",
        "cost_known": True,
    }
    # A skipped synthesis leaves no result mirror behind.
    assert not (run_dir / "result.md").exists()
    _assert_v1_markers(run, usage, run_dir)
    _assert_v1_layout_files(run_dir, stages, with_result_md=False)


# ---------------------------------------------------------------------------
# 4. v1 layout is read as-is with no migration
# ---------------------------------------------------------------------------


def test_v1_layout_is_read_as_is_without_migration(tmp_path):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_BOUNDARY_RUN)
    before = _snapshot_run_dir(run_dir)

    run, stages, usage = load_run_view(run_dir)
    for stage_id in run["stage_order"]:
        load_stage_view(run_dir, stage_id)

    assert _snapshot_run_dir(run_dir) == before  # zero bytes written, no migration

    _assert_v1_layout_files(run_dir, stages, with_result_md=True)
    _assert_v1_markers(run, usage, run_dir)


def test_retained_v1_events_log_remains_parseable(tmp_path):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_BOUNDARY_RUN)

    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    parsed = [json.loads(line) for line in lines]  # every line parses
    assert all(event["event"] in EVENT_TYPES for event in parsed)
    assert {event["run_id"] for event in parsed} == {BOUNDARY_RUN_ID}
    assert parsed[0]["event"] == "run_started"
    assert parsed[-1]["event"] == "run_succeeded"


# ---------------------------------------------------------------------------
# 3. CLI status / inspect against the same retained runs (disk only)
# ---------------------------------------------------------------------------


def test_old_run_directory_v0_schema_remains_inspectable(tmp_path, capsys):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_BOUNDARY_RUN)
    runs_dir = tmp_path
    before = _snapshot_run_dir(run_dir)

    assert main(["status", BOUNDARY_RUN_ID, "--runs-dir", str(runs_dir)]) == 0
    status_text = capsys.readouterr().out
    assert "run_id: " + BOUNDARY_RUN_ID in status_text
    assert "state: succeeded" in status_text
    assert "extractor: model=openai/gpt-5.6-luna state=succeeded" in status_text
    assert "completion=complete finish_reason='stop'" in status_text
    assert "output=stages/extractor.md" in status_text
    assert "aggregate_cost: 0.01129765 status=known complete=True" in status_text

    assert main(["inspect", BOUNDARY_RUN_ID, "synthesis", "--runs-dir", str(runs_dir)]) == 0
    inspect_text = capsys.readouterr().out
    assert "stage_id: synthesis" in inspect_text
    assert "model: google/gemini-3.7-flash" in inspect_text
    assert "state: succeeded" in inspect_text
    assert "completion: complete" in inspect_text
    assert "finish_reason: 'stop'" in inspect_text
    assert "--- output ---" in inspect_text
    assert "### Explicit Facts" in inspect_text

    assert _snapshot_run_dir(run_dir) == before  # CLI reading writes nothing


def test_cli_status_and_inspect_handle_pre_telemetry_v0_shape(tmp_path, capsys):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_OLD_SHAPE_RUN)
    runs_dir = tmp_path

    assert main(["status", OLD_SHAPE_RUN_ID, "--runs-dir", str(runs_dir)]) == 0
    status_text = capsys.readouterr().out
    assert "state: succeeded" in status_text
    assert "extractor: model=openai/gpt-5.6-luna state=succeeded" in status_text
    assert "completion=not_available" in status_text
    assert "aggregate_cost: 0.004089100 status=known complete=True" in status_text

    assert main(["inspect", OLD_SHAPE_RUN_ID, "synthesis", "--runs-dir", str(runs_dir)]) == 0
    inspect_text = capsys.readouterr().out
    assert "completion: not_available" in inspect_text
    assert "finish_reason: None" in inspect_text
    assert "--- output ---" in inspect_text
    assert "### Integrated Live-Conformance Report" in inspect_text


def test_cli_status_reports_failed_run_and_skipped_synthesis(tmp_path, capsys):
    run_dir = _copy_evidence_run(tmp_path, GOLDEN_FAILED_RUN)
    runs_dir = tmp_path

    assert main(["status", FAILED_RUN_ID, "--runs-dir", str(runs_dir)]) == 1
    status_text = capsys.readouterr().out
    assert "state: failed" in status_text
    assert "synthesis: model=google/gemini-3.7-flash state=skipped" in status_text
    assert (
        "failure=dependency_incomplete: synthesis dependency did not complete normally"
        in status_text
    )
    assert "aggregate_cost: 0.002133175 status=known complete=True" in status_text

    assert main(["inspect", FAILED_RUN_ID, "synthesis", "--runs-dir", str(runs_dir)]) == 0
    inspect_text = capsys.readouterr().out
    assert "state: skipped" in inspect_text
    assert "--- failure ---" in inspect_text
    assert "dependency_incomplete: synthesis dependency did not complete normally" in inspect_text
    assert "--- output ---" not in inspect_text  # no output was ever produced


# ---------------------------------------------------------------------------
# 2. locate_run_dir resolution
# ---------------------------------------------------------------------------


def test_locate_run_dir_resolves_run_ids_inside_runs_dir(tmp_path):
    runs_root = tmp_path / "runs"
    target = runs_root / BOUNDARY_RUN_ID
    shutil.copytree(GOLDEN_BOUNDARY_RUN, target)

    located = locate_run_dir(BOUNDARY_RUN_ID, runs_root)
    assert located == target.resolve(strict=False)
    assert located.is_dir()

    plain = locate_run_dir("plain-run-id", tmp_path / "does-not-exist-yet")
    assert plain == (tmp_path / "does-not-exist-yet" / "plain-run-id").resolve(strict=False)


def test_locate_run_dir_rejects_invalid_or_escaping_run_ids(tmp_path):
    for bad_id in ("", "..", "nested/escape", "-leading-dash"):
        with pytest.raises(ValidationError):
            locate_run_dir(bad_id, tmp_path / "runs")


# ---------------------------------------------------------------------------
# 5. Terminal states synthesised with the engine's own writers read truthfully
# ---------------------------------------------------------------------------


def test_synthesised_v1_run_failed_worker_stage_reads_truthfully(tmp_path):
    provider = FakeProvider(
        results={"model-w1": "one"},
        failures={"model-w2": ProviderError("boom")},
    )

    result = _run_synth_job(tmp_path, provider, run_id="compat-provider-error")

    assert result.state.value == "failed"
    run, stages, usage = load_run_view(result.run_dir)
    assert run["state"] == "failed"
    assert run["synthesis_skipped_reason"] == "dependency_failed"
    by_id = {stage["stage_id"]: stage for stage in stages}
    assert by_id["w1"]["state"] == "succeeded"
    assert by_id["w2"]["state"] == "failed"
    assert by_id["w2"]["failure"]["type"] == "ProviderError"
    assert by_id["w2"]["failure"]["message"] == "boom"
    assert by_id["w2"]["failure"]["provider_side_outcome_unknown"] is False
    assert by_id["synth"]["state"] == "skipped"
    assert by_id["synth"]["failure"]["type"] == "dependency_failed"

    meta, output = load_stage_view(result.run_dir, "w1")
    assert output == "one"
    meta, output = load_stage_view(result.run_dir, "w2")
    assert output is None  # a failed stage has no output file
    assert not (result.run_dir / "result.md").exists()
    assert usage["aggregate"]["cost_status"] == "partial"
    assert usage["aggregate"]["unknown_cost_stage_ids"] == ["w2"]
    _assert_v1_markers(run, usage, result.run_dir)
    _assert_v1_layout_files(result.run_dir, stages, with_result_md=False)


def test_synthesised_v1_run_stage_request_timeout_reads_truthfully(tmp_path):
    provider = FakeProvider(delays={"model-w1": 0.05})
    path, job_dict = make_job_tree(tmp_path)
    job_dict["workers"][0]["timeout_seconds"] = 0.001
    path.write_text(json.dumps(job_dict), encoding="utf-8")
    job = load_job(path)
    result = asyncio.run(run_job(job, {"openrouter": provider}, run_id="compat-stage-timeout"))

    run, stages, usage = load_run_view(result.run_dir)
    assert run["state"] == "failed"
    by_id = {stage["stage_id"]: stage for stage in stages}
    assert by_id["w1"]["state"] == "failed"
    assert by_id["w1"]["failure"]["type"] == "request_timeout"
    assert by_id["w1"]["failure"]["message"] == "provider request timed out"
    assert by_id["w1"]["failure"]["provider_side_outcome_unknown"] is True
    assert by_id["w2"]["state"] == "succeeded"  # sibling unaffected
    assert by_id["synth"]["state"] == "skipped"
    assert by_id["synth"]["failure"]["type"] == "dependency_failed"
    meta, output = load_stage_view(result.run_dir, "w1")
    assert output is None


def test_synthesised_v1_run_overall_timeout_persists_timed_out_readably(tmp_path, capsys):
    provider = FakeProvider(delays={"model-w1": 0.1, "model-w2": 0.1})

    result = _run_synth_job(tmp_path, provider, run_id="compat-run-timeout", run_timeout=0.01)

    assert result.state.value == "timed_out"
    run, stages, usage = load_run_view(result.run_dir)
    assert run["state"] == "timed_out"
    by_id = {stage["stage_id"]: stage for stage in stages}
    for worker_id in ("w1", "w2"):
        assert by_id[worker_id]["state"] == "failed"
        assert by_id[worker_id]["failure"]["type"] == "run_timed_out"
        assert by_id[worker_id]["failure"]["provider_side_outcome_unknown"] is True
        assert by_id[worker_id]["output_path"] is None
    # Synthesis was never dispatched: it reads as skipped with the timeout cause.
    assert by_id["synth"]["state"] == "skipped"
    assert by_id["synth"]["failure"]["type"] == "run_timed_out"
    for stage_id in ("w1", "w2", "synth"):
        meta, output = load_stage_view(result.run_dir, stage_id)
        assert output is None
    assert usage["aggregate"]["cost_status"] == "partial"
    assert usage["aggregate"]["unknown_cost_stage_ids"] == ["w1", "w2"]
    assert not (result.run_dir / "result.md").exists()

    assert main(["status", "compat-run-timeout", "--runs-dir", str(result.run_dir.parent)]) == 1
    status_text = capsys.readouterr().out
    assert "state: timed_out" in status_text
    assert "w1: model=model-w1 state=failed" in status_text
    assert "synth: model=model-synth state=skipped" in status_text
    assert "aggregate_cost: 0 status=partial complete=False" in status_text


# ---------------------------------------------------------------------------
# 6. known / partial / unknown cost aggregation reads truthfully
# ---------------------------------------------------------------------------


class _VariableCostProvider(FakeProvider):
    """Deterministic fake whose per-model known cost is configurable."""

    def __init__(self, unknown_models: set[str]):
        super().__init__()
        self._unknown_models = unknown_models

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        return CompletionResult(
            output_text=f"ok:{request.model}",
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id=f"req-{request.model}",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=(
                None if request.model in self._unknown_models else Decimal("0.25")
            ),
            duration_seconds=0.001,
        )


def test_cost_aggregation_known_partial_unknown_reads_truthfully(tmp_path):
    # known: the retained golden run already pins the "known" aggregate.
    run, _stages, usage = load_run_view(_copy_evidence_run(tmp_path / "known", GOLDEN_BOUNDARY_RUN))
    assert usage["aggregate"]["cost_status"] == "known"
    assert usage["aggregate"]["cost_complete"] is True
    assert usage["aggregate"]["unknown_cost_stage_ids"] == []
    assert run["usage"] == usage["aggregate"]

    # partial: exactly one stage has unknown cost.
    (tmp_path / "partial").mkdir()
    result = _run_synth_job(
        tmp_path / "partial",
        _VariableCostProvider(unknown_models={"model-w2"}),
        run_id="compat-partial-cost",
    )
    usage = json.loads((result.run_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["aggregate"]["cost_status"] == "partial"
    assert usage["aggregate"]["cost_usd_known_sum"] == "0.50"
    assert usage["aggregate"]["cost_complete"] is False
    assert usage["aggregate"]["unknown_cost_stage_ids"] == ["w2"]
    run = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["state"] == "succeeded"  # unknown cost never fails a run
    assert run["usage"] == usage["aggregate"]

    # unknown: every stage cost is unknown.
    (tmp_path / "unknown").mkdir()
    result = _run_synth_job(
        tmp_path / "unknown",
        _VariableCostProvider(
            unknown_models={"model-w1", "model-w2", "model-synth"}
        ),
        run_id="compat-unknown-cost",
    )
    usage = json.loads((result.run_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["aggregate"]["cost_status"] == "unknown"
    assert usage["aggregate"]["cost_usd_known_sum"] == "0"
    assert usage["aggregate"]["cost_complete"] is False
    assert usage["aggregate"]["unknown_cost_stage_ids"] == ["w1", "w2", "synth"]
    run = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["usage"] == usage["aggregate"]
