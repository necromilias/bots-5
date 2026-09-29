"""Phase 10 evidence-regeneration contracts (design rows T0.4, T0.5, T1.2, T1.3).

Test contracts written from the SEALED DESIGN (``DESIGN_SEAL_v4.json`` and the
design documents in the campaign pack), not from the implementation:

- ``REGENERATION_AND_STALE_SYNTHESIS.md`` §1-§8: append-only sibling worker
  regeneration, explicit selection, the mechanical staleness predicate, the
  explicit synthesis rerun, and the M-6 read-only rule for version-1 runs;
- ``CAMPAIGN_EVIDENCE_EVOLUTION.md`` §2/§4/§5: the flat attempt grammar,
  dual accounting (cumulative vs derived selected spend), and the additive
  append-only event vocabulary;
- ``PREFLIGHT_APPROVAL_STATE_MACHINE.md`` §3/§4: one-shot durable approval
  consumption, route locking, selection/bytes binding before dispatch;
- Mick clarifications O-2/O-3 (``parcel-v2/NORMALIZED_CORRECTIONS.md``):
  a sibling cancelled during a generic run failure keeps its own ``cancelled``
  cause while the run stays FAILED with ``internal_error``.

Coverage map (each numbered requirement is its own test):
1. attempt preservation when regenerating a worker;
2. sibling independence between stages;
3. explicit model change on the new attempt only + locked provider route;
4. mechanical staleness both ways across explicit reselection;
5. explicit synthesis rerun (new attempt, old preserved, selection moves only
   on success, result.md mirrors the new output);
6. append-only regeneration/rerun event kinds;
7. selection/bytes binding refuses a rerun approval invalidated by a later
   regeneration + reselection, before dispatch;
8. version-1 runs are read-only for regeneration and for rerun;
9. replaying a consumed approval makes no second provider request;
10. dual accounting: cumulative spend includes the new attempt while selected
    spend is derived from selection.json;
11. O-2: cancelled sibling keeps ``cancelled`` + provider-side uncertainty
    while the run is FAILED with ``internal_error``;
12. D-12 (oracle V-6): an output-write fault during the in-place terminal
    update leaves the attempt metadata un-advanced (never a durable
    succeeded state advertising an unreadable artifact).

Every test is deterministic and offline: providers are route-faithful fakes
(subclasses of the real ``OpenRouterProvider`` so the engine's kind-specific
provider-object route validation accepts them) that never touch the network.
Fixtures are built on disk under pytest ``tmp_path``; the one retained
version-1 fixture is copied READ-ONLY from ``evidence/**`` (never mutated).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bots5.core.campaign import CampaignBridge
from bots5.cli import main as cli_main
from bots5.errors import ApprovalInvalidatedError, Bots5Error, ProviderError, StorageError, ValidationError
from bots5.events import EVENT_TYPES
from bots5.manifest import load_job
from bots5.models import ApprovalRecord, OperationSnapshot, StageRecord, StageState
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import (
    build_preflight_snapshot,
    declared_provider_routes,
    regenerate_worker,
    rerun_synthesis,
)
from bots5.storage import (
    approval_consumed,
    atomic_write_text,
    create_run_tree,
    load_run_view,
    load_stage_view,
    persist_stage_attempt,
    reconstruct_run_state,
)

from .helpers import make_job_tree

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pricing():
    return {"entries": [{"provider": "openrouter", "input_usd_per_1m": "1.25",
                         "output_usd_per_1m": "2.50", "rate_source": "operator test citation",
                         "observed_at": "2026-09-29T12:00:00Z"}]}
EVIDENCE_ROOT = REPO_ROOT / "evidence"

# Retained version-1 run directory (never written to; copied into tmp_path).
GOLDEN_V1_RUN = (
    EVIDENCE_ROOT
    / "v0.1-final-worker-boundary"
    / "bots5-v0.1-final-worker-boundary-canary-20260828T233010Z"
    / "canary"
    / ".bots5"
    / "runs"
    / "bots5-v0.1-final-worker-boundary-live-conformanc-20260828T233156Z-581f7132"
)
BOUNDARY_RUN_ID = "bots5-v0.1-final-worker-boundary-live-conformanc-20260828T233156Z-581f7132"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class FakeRoutedProvider(OpenRouterProvider):
    """Route-faithful offline fake.

    A subclass of the real ``OpenRouterProvider`` so the engine's
    kind-specific provider-object route validation (isinstance + base_url)
    accepts it, with ``complete()`` fully faked: no endpoint, no network, no
    environment dependency (the API key is a fake literal). Cost is
    configurable per model so dual accounting can distinguish attempts.
    """

    def __init__(
        self,
        *,
        results: dict[str, str] | None = None,
        failures: dict[str, Exception] | None = None,
        delays: dict[str, float] | None = None,
        costs: dict[str, Decimal] | None = None,
        on_request=None,
        base_url: str = OPENROUTER_BASE_URL,
    ):
        super().__init__("offline-fake-api-key", base_url=base_url)
        self.results = dict(results or {})
        self.failures = dict(failures or {})
        self.delays = dict(delays or {})
        self.costs = dict(costs or {})
        self.on_request = on_request
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if self.on_request is not None:
            self.on_request(request)
        delay = self.delays.get(request.model, 0)
        if delay:
            await asyncio.sleep(delay)
        failure = self.failures.get(request.model)
        if failure is not None:
            raise failure
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
            known_cost_usd=self.costs.get(request.model, Decimal("0.01")),
            duration_seconds=0.001,
        )


class _FakeOpenAICompatible(OpenAICompatibleProvider):
    """Wrong-kind fake used to prove route locking refuses a kind swap."""

    def __init__(self, base_url: str):
        super().__init__(base_url)
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        raise AssertionError("a swapped-kind provider must never be dispatched")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _snapshot_tree(root: Path) -> dict[str, str]:
    """Content fingerprint of every file under ``root``."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _att_meta(run_dir: Path, stage_id: str, attempt: int) -> dict:
    path = run_dir / "stages" / f"{stage_id}.att{attempt}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _selection(run_dir: Path) -> dict[str, int]:
    path = run_dir / "selection.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["selected_attempts"]


def _events(run_dir: Path) -> list[dict]:
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _make_bridge(tmp_path: Path, provider) -> CampaignBridge:
    job_path, _job = make_job_tree(tmp_path)
    bridge = CampaignBridge(
        tmp_path / ".bots5" / "runs",
        provider_factory=lambda job: {"openrouter": provider},
    )
    bridge.load_job(job_path)
    return bridge


def _approve(bridge: CampaignBridge, prepared):
    """Consume the prepared approval on the running loop; await the engine."""

    async def _drive():
        if prepared.operation == "full_run":
            bridge.approve_and_start(prepared)
        elif prepared.operation == "worker_regeneration":
            bridge.approve_and_regenerate(prepared)
        else:
            bridge.approve_and_rerun_synthesis(prepared)
        return await bridge.run_to_completion()

    return asyncio.run(_drive())


def _run_full_run(tmp_path: Path, provider) -> tuple[CampaignBridge, Path]:
    """A succeeded evidence-v2 run produced by the engine's own writers."""
    bridge = _make_bridge(tmp_path, provider)
    prepared = bridge.prepare_full_run("test-operator", pricing_evidence=_pricing())
    result = _approve(bridge, prepared)
    assert result.state.value == "succeeded"
    return bridge, result.run_dir


def test_cli_paid_regeneration_requires_pricing_before_provider_and_accepts_pricing_file(tmp_path, monkeypatch):
    run_provider = FakeRoutedProvider()
    _bridge, run_dir = _run_full_run(tmp_path, run_provider)
    job_path = tmp_path / "job.json"
    operation_provider = FakeRoutedProvider()
    builds = []
    monkeypatch.setattr("bots5.cli._build_providers", lambda job: (builds.append(job) or {"openrouter": operation_provider}))
    base_args = ["regenerate", run_dir.name, "w1", "--model", "model-w1-cli",
                 "--job", str(job_path), "--runs-dir", str(tmp_path / ".bots5" / "runs")]

    before = _snapshot_tree(run_dir)
    assert cli_main(base_args) == 0  # no consent remains a zero-write preflight
    assert builds == [] and operation_provider.calls == []
    assert cli_main(base_args + ["--approve", "--actor", "operator"]) == 1
    assert builds == [] and operation_provider.calls == []
    assert _snapshot_tree(run_dir) == before

    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps(_pricing()), encoding="utf-8")
    assert cli_main(base_args + ["--approve", "--actor", "operator", "--pricing", str(pricing_path)]) == 0
    assert len(builds) == 1 and len(operation_provider.calls) == 1
    approvals = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (run_dir / "approvals").glob("*.json")]
    latest = next(item for item in approvals if item.get("target", {}).get("stage_id") == "w1")
    assert latest["pricing_evidence"]["entries"][0]["rate_source"] == "operator test citation"


def _copy_retained_v1_run(tmp_path: Path) -> Path:
    """Copy the retained v1 fixture READ-ONLY into tmp_path (never evidence/**)."""
    assert GOLDEN_V1_RUN.is_dir(), f"missing retained evidence run fixture: {GOLDEN_V1_RUN}"
    assert EVIDENCE_ROOT in GOLDEN_V1_RUN.parents
    target = tmp_path / GOLDEN_V1_RUN.name
    shutil.copytree(GOLDEN_V1_RUN, target)
    return target


def _synth_job(tmp_path: Path):
    base = tmp_path / "job"
    base.mkdir()
    job_path, _job = make_job_tree(base)
    return load_job(job_path)


def _regen_binding(job, run_id: str, stage_id: str, model: str):
    """A well-formed regeneration OperationSnapshot + ApprovalRecord pair.

    Built exactly like the desktop seam builds one (frozen preflight digest,
    binding attempt number, frozen route), through the public models API.
    """
    preflight = build_preflight_snapshot(job)
    route = dict(declared_provider_routes(job)["openrouter"])
    snapshot = OperationSnapshot(
        operation="worker_regeneration",
        target_run_id=run_id,
        stage_id=stage_id,
        attempt_number=2,
        model=model,
        provider_route=route,
        system_message=preflight.system_messages["w1"],
        user_message="frozen worker payload",
        dependency_attempts={},
        dependency_digests={},
        preflight_digest=preflight.preflight_digest,
        operation_digest="",
    )
    snapshot = replace(snapshot, operation_digest=snapshot.compute_digest())
    approval = ApprovalRecord(
        approval_id="approval-20260101T000000Z-00000001",
        approved_at="2026-01-01T00:00:00Z",
        approved_by="test-operator",
        preflight_digest=snapshot.preflight_digest,
        scope=f"worker_regeneration:{stage_id}",
        target={"run_id": run_id, "stage_id": stage_id, "attempt_number": 2},
    )
    return snapshot, approval


# ---------------------------------------------------------------------------
# 1. Attempt preservation (T0.4 / T1.2)
# ---------------------------------------------------------------------------


def test_worker_regeneration_preserves_original_attempt_and_creates_sibling(tmp_path):
    provider = FakeRoutedProvider(results={"model-w1": "worker-one-output"})
    bridge, run_dir = _run_full_run(tmp_path, provider)

    original_stage_bytes = _snapshot_tree(run_dir / "stages")
    run_json_before = (run_dir / "run.json").read_bytes()
    result_md_before = (run_dir / "result.md").read_bytes()
    files_before = set(_snapshot_tree(run_dir))

    bridge.adopt_run(run_dir)
    prepared = bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing())
    assert prepared.attempt_number == 2
    _approve(bridge, prepared)

    # The sibling attempt is APPENDED: exactly the new attempt files, the
    # materialized implicit selection, and the one-shot approval marker are
    # new; nothing was removed.
    after_files = set(_snapshot_tree(run_dir))
    assert after_files - files_before == {
        "stages/w1.att2.json",
        "stages/w1.att2.md",
        "selection.json",
        f"approvals/{prepared.approval.approval_id}.json",
    }
    assert files_before - after_files == set()

    # Every byte of att1 and of every other stage is untouched, and the run
    # document and result mirror were not rewritten by the regeneration.
    stages_after = _snapshot_tree(run_dir / "stages")
    for name, digest in original_stage_bytes.items():
        assert stages_after[name] == digest, f"regeneration modified {name}"
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert (run_dir / "result.md").read_bytes() == result_md_before

    sibling = _att_meta(run_dir, "w1", 2)
    assert sibling["attempt_number"] == 2
    assert sibling["state"] == "succeeded"
    assert sibling["preflight_digest"] == prepared.approval.preflight_digest
    assert (run_dir / "stages" / "w1.att2.md").read_text(encoding="utf-8") == (
        "output:model-w1-alt"
    )

    # The selection is unchanged by the execution itself (regeneration never
    # auto-selects), so the reader still resolves the ORIGINAL attempt.
    assert _selection(run_dir) == {"w1": 1, "w2": 1, "synth": 1}
    _run_view, stages_view, _usage = load_run_view(run_dir)
    by_id = {stage["stage_id"]: stage for stage in stages_view}
    assert by_id["w1"]["attempt_number"] == 1
    _meta, output = load_stage_view(run_dir, "w1")
    assert output == "worker-one-output"


# ---------------------------------------------------------------------------
# 2. Sibling independence (T1.2)
# ---------------------------------------------------------------------------


def test_regeneration_of_one_stage_leaves_other_stage_attempts_untouched(tmp_path):
    provider = FakeRoutedProvider()
    bridge, run_dir = _run_full_run(tmp_path, provider)
    bridge.adopt_run(run_dir)

    _approve(bridge, bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing()))
    after_first = _snapshot_tree(run_dir / "stages")
    assert set(after_first) == {
        "w1.att1.json", "w1.att1.md", "w1.att2.json", "w1.att2.md",
        "w2.att1.json", "w2.att1.md", "synth.att1.json", "synth.att1.md",
    }

    _approve(bridge, bridge.prepare_regeneration("w2", "model-w2-alt", "test-operator", pricing_evidence=_pricing()))
    after_second = _snapshot_tree(run_dir / "stages")
    # Regenerating w2 changed no other stage's attempts — w1's original AND
    # regenerated attempts and synthesis are byte-identical.
    for name, digest in after_first.items():
        assert after_second[name] == digest, f"regenerating w2 modified {name}"
    assert set(after_second) - set(after_first) == {"w2.att2.json", "w2.att2.md"}


# ---------------------------------------------------------------------------
# 3. Explicit model change + locked provider route (T0.4, HSF-5)
# ---------------------------------------------------------------------------


def test_worker_regeneration_explicit_model_change_does_not_silently_change_provider_route(
    tmp_path,
):
    provider = FakeRoutedProvider()
    bridge, run_dir = _run_full_run(tmp_path, provider)
    bridge.adopt_run(run_dir)

    prepared = bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing())
    _approve(bridge, prepared)

    sibling = _att_meta(run_dir, "w1", 2)
    original = _att_meta(run_dir, "w1", 1)
    assert sibling["requested_model"] == "model-w1-alt"  # recorded on the NEW attempt only
    assert original["requested_model"] == "model-w1"  # the original is untouched
    assert provider.calls[-1].model == "model-w1-alt"  # the dispatch used the new model
    assert sibling["provider"] == original["provider"] == "openrouter"  # route unchanged

    # The provider ROUTE is locked: a route change is refused with the typed
    # error before any dispatch, and writes nothing.
    tree_before = _snapshot_tree(run_dir)
    calls_before = len(provider.calls)
    route_prepared = bridge.prepare_regeneration("w1", "model-w1-alt2", "test-operator", pricing_evidence=_pricing())
    swapped_base = FakeRoutedProvider(base_url="https://other.example/api/v1")
    swapped_kind = _FakeOpenAICompatible("http://127.0.0.1:9/v1")
    for swapped in (swapped_base, swapped_kind):
        with pytest.raises(ApprovalInvalidatedError):
            asyncio.run(
                regenerate_worker(
                    route_prepared.job,
                    {"openrouter": swapped},
                    run_dir=route_prepared.run_dir,
                    run_id=route_prepared.run_id,
                    stage_id=route_prepared.stage_id,
                    model=route_prepared.model,
                    snapshot=route_prepared.snapshot,
                    approval=route_prepared.approval,
                )
            )
    assert swapped_base.calls == []
    assert swapped_kind.calls == []
    assert len(provider.calls) == calls_before
    assert _snapshot_tree(run_dir) == tree_before
    assert not (run_dir / "stages" / "w1.att3.json").exists()


# ---------------------------------------------------------------------------
# 4. Mechanical staleness, both ways (T0.5 §4.1)
# ---------------------------------------------------------------------------


def test_staleness_predicate_is_bidirectional_across_reselection(tmp_path):
    provider = FakeRoutedProvider(
        results={"model-w1": "worker-one-output", "model-w1-alt": "worker-one-regenerated"}
    )
    bridge, run_dir = _run_full_run(tmp_path, provider)

    def _freshness() -> dict:
        return reconstruct_run_state(run_dir)["stages"]["synth"]["synthesis_freshness"]

    # An initial-run synthesis is FRESH as soon as it exists (§3.1): selection
    # defaults to attempt 1 and the recorded digests match.
    assert _freshness()["classification"] == "FRESH"

    bridge.adopt_run(run_dir)
    _approve(bridge, bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing()))
    assert _freshness()["classification"] == "FRESH"  # selection has not moved

    # Explicit selection move to attempt 2 -> the run reads STALE ...
    bridge.select_attempt("w1", 2)
    stale = _freshness()
    assert stale["classification"] == "STALE"
    assert stale["integrity_warning"] is False  # selection moved: no digest anomaly
    assert bridge.projection(run_dir).synthesis_freshness == "STALE"
    state_map = reconstruct_run_state(run_dir)
    assert state_map["stages"]["w1"]["selected_attempt"] == 2
    assert state_map["stages"]["w1"]["available_attempts"] == [1, 2]
    _run_view, stages_view, _usage = load_run_view(run_dir)
    by_id = {stage["stage_id"]: stage for stage in stages_view}
    assert by_id["w1"]["attempt_number"] == 2
    assert by_id["synth"]["attempt_number"] == 1

    # ... and selecting attempt 1 again reads FRESH: staleness is reversible
    # and no evidence was destroyed in either direction.
    bridge.select_attempt("w1", 1)
    assert _freshness()["classification"] == "FRESH"
    assert bridge.projection(run_dir).synthesis_freshness == "FRESH"


# ---------------------------------------------------------------------------
# 5. Explicit synthesis rerun (T0.5 §5/§6)
# ---------------------------------------------------------------------------


def test_explicit_synthesis_rerun_creates_new_attempt_and_preserves_old(tmp_path):
    provider = FakeRoutedProvider(results={"model-synth": "synthesis-one"})
    bridge, run_dir = _run_full_run(tmp_path, provider)

    att1_json = (run_dir / "stages" / "synth.att1.json").read_bytes()
    att1_md = (run_dir / "stages" / "synth.att1.md").read_bytes()
    result_md_before = (run_dir / "result.md").read_bytes()
    assert result_md_before == att1_md  # single-leaf mirror of the selected attempt

    bridge.adopt_run(run_dir)
    # First rerun FAILS: selection moves only on success (§5 execution 4).
    provider.failures["model-synth"] = ProviderError("boom")
    failed_record = _approve(bridge, bridge.prepare_synthesis_rerun("test-operator", pricing_evidence=_pricing()))
    assert failed_record.state.value == "failed"
    failed = _att_meta(run_dir, "synth", 2)
    assert failed["state"] == "failed"
    assert failed["failure"]["type"] == "ProviderError"
    assert _selection(run_dir)["synth"] == 1  # unchanged by the failed rerun
    assert (run_dir / "result.md").read_bytes() == result_md_before
    assert (run_dir / "stages" / "synth.att1.json").read_bytes() == att1_json

    # Second rerun SUCCEEDS: new attempt, selection moves, result.md mirrors.
    failed_json = (run_dir / "stages" / "synth.att2.json").read_bytes()
    del provider.failures["model-synth"]
    record = _approve(bridge, bridge.prepare_synthesis_rerun("test-operator", pricing_evidence=_pricing()))
    assert (record.id, record.attempt_number, record.state.value) == ("synth", 3, "succeeded")
    assert _selection(run_dir)["synth"] == 3
    assert (run_dir / "result.md").read_text(encoding="utf-8") == (
        (run_dir / "stages" / "synth.att3.md").read_text(encoding="utf-8")
    )
    # Earlier synthesis attempts are preserved byte-identically (§5 execution 3).
    assert (run_dir / "stages" / "synth.att1.json").read_bytes() == att1_json
    assert (run_dir / "stages" / "synth.att1.md").read_bytes() == att1_md
    assert (run_dir / "stages" / "synth.att2.json").read_bytes() == failed_json

    # The new attempt permanently records what it consumed (§3 provenance).
    third = _att_meta(run_dir, "synth", 3)
    assert third["consumed_dependencies"] == {"w1": 1, "w2": 1}
    assert third["dependency_digests"] == {
        "w1": hashlib.sha256((run_dir / "stages" / "w1.att1.md").read_bytes()).hexdigest(),
        "w2": hashlib.sha256((run_dir / "stages" / "w2.att1.md").read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------------------
# 6. Append-only events (T1.3)
# ---------------------------------------------------------------------------


def test_events_regeneration_and_staleness_are_appended_without_rewriting_history(tmp_path):
    provider = FakeRoutedProvider()
    bridge, run_dir = _run_full_run(tmp_path, provider)
    events_path = run_dir / "events.jsonl"

    after_run = events_path.read_bytes()
    assert all(event["event"] in EVENT_TYPES for event in _events(run_dir))
    assert {event["run_id"] for event in _events(run_dir)} == {run_dir.name}

    bridge.adopt_run(run_dir)
    _approve(bridge, bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing()))
    after_regen = events_path.read_bytes()
    assert after_regen.startswith(after_run)  # not one earlier byte modified
    kinds = [event["event"] for event in _events(run_dir)]
    assert "worker_regeneration_started" in kinds
    assert "worker_regeneration_finished" in kinds

    bridge.select_attempt("w1", 2)
    after_select = events_path.read_bytes()
    assert after_select.startswith(after_regen)
    selected = [event for event in _events(run_dir) if event["event"] == "attempt_selected"]
    assert selected[-1]["stage_id"] == "w1"
    assert selected[-1]["meta"] == {"attempt_number": 2, "previous_attempt": 1}

    _approve(bridge, bridge.prepare_synthesis_rerun("test-operator", pricing_evidence=_pricing()))
    after_rerun = events_path.read_bytes()
    assert after_rerun.startswith(after_select)
    kinds = [event["event"] for event in _events(run_dir)]
    assert "synthesis_rerun_started" in kinds
    assert "synthesis_rerun_finished" in kinds
    parsed = _events(run_dir)
    assert all(event["event"] in EVENT_TYPES for event in parsed)  # vocabulary stays sealed
    assert {event["run_id"] for event in parsed} == {run_dir.name}


# ---------------------------------------------------------------------------
# 7. Selection/bytes binding before dispatch (T0.5 §5 precondition 5, F-01)
# ---------------------------------------------------------------------------


def test_synthesis_rerun_refused_when_selection_or_bytes_changed_after_approval(tmp_path):
    provider = FakeRoutedProvider()
    bridge, run_dir = _run_full_run(tmp_path, provider)
    bridge.adopt_run(run_dir)

    # The rerun approval is prepared FIRST, binding the then-current selection
    # (w1 attempt 1) and its output digest.
    prepared = bridge.prepare_synthesis_rerun("test-operator", pricing_evidence=_pricing())
    assert prepared.snapshot.dependency_attempts == {"w1": 1, "w2": 1}

    # THEN a dependency is regenerated and reselected.
    _approve(bridge, bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing()))
    bridge.select_attempt("w1", 2)

    tree_before = _snapshot_tree(run_dir)
    calls_before = len(provider.calls)
    with pytest.raises(ApprovalInvalidatedError, match="binding"):
        asyncio.run(
            rerun_synthesis(
                prepared.job,
                {"openrouter": provider},
                run_dir=prepared.run_dir,
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            )
        )
    assert len(provider.calls) == calls_before  # refused BEFORE any provider request
    assert _snapshot_tree(run_dir) == tree_before  # refused writes nothing
    assert not (run_dir / "stages" / "synth.att2.json").exists()


# ---------------------------------------------------------------------------
# 8. Version-1 runs are read-only (T0.4/T0.5, M-6 repair)
# ---------------------------------------------------------------------------


def test_regeneration_refused_for_evidence_version_1_run_writing_nothing(tmp_path):
    v1_dir = _copy_retained_v1_run(tmp_path)
    job = _synth_job(tmp_path)
    snapshot, approval = _regen_binding(job, BOUNDARY_RUN_ID, "extractor", "model-extractor-alt")
    provider = FakeRoutedProvider()
    before = _snapshot_tree(v1_dir)

    with pytest.raises(ValidationError, match="read-only"):
        asyncio.run(
            regenerate_worker(
                job,
                {"openrouter": provider},
                run_dir=v1_dir,
                run_id=BOUNDARY_RUN_ID,
                stage_id="extractor",
                model="model-extractor-alt",
                snapshot=snapshot,
                approval=approval,
            )
        )
    assert _snapshot_tree(v1_dir) == before  # nothing was written
    assert provider.calls == []  # no provider request


def test_synthesis_rerun_refused_for_evidence_version_1_run_writing_nothing(tmp_path):
    v1_dir = _copy_retained_v1_run(tmp_path)
    job = _synth_job(tmp_path)
    # The snapshot content is never consulted: the M-6 version gate is
    # precondition 0 and precedes every binding assertion.
    preflight = build_preflight_snapshot(job)
    snapshot = OperationSnapshot(
        operation="synthesis_rerun",
        target_run_id=BOUNDARY_RUN_ID,
        stage_id="synthesis",
        attempt_number=2,
        model="model-synthesis",
        provider_route=dict(declared_provider_routes(job)["openrouter"]),
        system_message=preflight.system_messages["w1"],
        user_message="frozen synthesis payload",
        dependency_attempts={},
        dependency_digests={},
        preflight_digest=preflight.preflight_digest,
        operation_digest="",
    )
    snapshot = replace(snapshot, operation_digest=snapshot.compute_digest())
    approval = ApprovalRecord(
        approval_id="approval-20260101T000000Z-00000002",
        approved_at="2026-01-01T00:00:00Z",
        approved_by="test-operator",
        preflight_digest=snapshot.preflight_digest,
        scope="synthesis_rerun",
        target={"run_id": BOUNDARY_RUN_ID, "stage_id": "synthesis", "attempt_number": 2},
    )
    provider = FakeRoutedProvider()
    before = _snapshot_tree(v1_dir)

    with pytest.raises(ValidationError, match="read-only"):
        asyncio.run(
            rerun_synthesis(
                job,
                {"openrouter": provider},
                run_dir=v1_dir,
                run_id=BOUNDARY_RUN_ID,
                snapshot=snapshot,
                approval=approval,
            )
        )
    assert _snapshot_tree(v1_dir) == before  # nothing was written
    assert provider.calls == []  # no provider request


# ---------------------------------------------------------------------------
# 9. Approval replay (T0.3, one-shot §3.1)
# ---------------------------------------------------------------------------


def test_replayed_consumed_approval_makes_no_second_provider_request(tmp_path):
    provider = FakeRoutedProvider()
    bridge, run_dir = _run_full_run(tmp_path, provider)
    bridge.adopt_run(run_dir)
    prepared = bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing())
    _approve(bridge, prepared)
    assert approval_consumed(run_dir, prepared.approval.approval_id)

    tree_before = _snapshot_tree(run_dir)
    calls_before = len(provider.calls)
    # A SECOND use of the SAME approval, through the engine entry point
    # directly (bypassing any in-process bridge guard) to prove the DURABLE
    # one-shot consumption marker is what refuses the replay.
    with pytest.raises(ApprovalInvalidatedError, match="already consumed"):
        asyncio.run(
            regenerate_worker(
                prepared.job,
                {"openrouter": provider},
                run_dir=prepared.run_dir,
                run_id=prepared.run_id,
                stage_id=prepared.stage_id,
                model=prepared.model,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            )
        )
    assert len(provider.calls) == calls_before  # no provider request
    assert _snapshot_tree(run_dir) == tree_before  # nothing written
    assert not (run_dir / "stages" / "w1.att3.json").exists()  # no additional attempt


# ---------------------------------------------------------------------------
# 10. Dual accounting (F-07, CAMPAIGN_EVIDENCE_EVOLUTION.md §4)
# ---------------------------------------------------------------------------


def test_dual_accounting_tracks_cumulative_spend_while_selected_spend_follows_selection(
    tmp_path,
):
    provider = FakeRoutedProvider(
        results={"model-w1": "worker-one-output", "model-w1-alt": "worker-one-regenerated"},
        costs={"model-w1": Decimal("0.01"), "model-w1-alt": Decimal("0.10")},
    )
    bridge, run_dir = _run_full_run(tmp_path, provider)
    usage = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["evidence_version"] == 2
    assert usage["cumulative_spend"]["cost_usd_known_sum"] == "0.03"
    assert usage["selected_spend"]["cost_usd_known_sum"] == "0.03"

    bridge.adopt_run(run_dir)
    _approve(bridge, bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing()))
    usage = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
    # Cumulative spend counts EVERY executed attempt — the hidden sibling is
    # included immediately (financial truth).
    assert usage["cumulative_spend"]["cost_usd_known_sum"] == "0.13"
    assert set(usage["per_attempt"]) == {"w1.att1", "w1.att2", "w2.att1", "synth.att1"}
    assert usage["per_attempt"]["w1.att1"]["cost_usd"] == "0.01"
    assert usage["per_attempt"]["w1.att2"]["cost_usd"] == "0.10"
    # ... while the selected spend does NOT silently follow the new attempt.
    assert usage["selected_spend"]["cost_usd_known_sum"] == "0.03"
    projection = bridge.projection(run_dir)
    assert projection.cumulative_spend["cost_usd_known_sum"] == "0.13"
    assert projection.selected_spend["cost_usd_known_sum"] == "0.03"

    # The selected spend is derived from selection.json: the explicit selection
    # move re-derives it; cumulative spend stays the financial truth.
    bridge.select_attempt("w1", 2)
    projection = bridge.projection(run_dir)
    assert projection.selected_spend["cost_usd_known_sum"] == "0.12"
    assert projection.cumulative_spend["cost_usd_known_sum"] == "0.13"
    # The stored cache still holds the pre-move value; the derived value wins
    # and the disagreement is surfaced, never silently trusted.
    stored = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
    assert stored["selected_spend"]["cost_usd_known_sum"] == "0.03"
    assert any("selected_spend cache" in warning for warning in projection.integrity_warnings)


@pytest.mark.parametrize("operation", ["worker_regeneration", "synthesis_rerun"])
def test_cancelling_regeneration_or_rerun_terminalizes_attempt_without_changing_run(
    tmp_path, operation
):
    provider = FakeRoutedProvider()
    bridge, run_dir = _run_full_run(tmp_path, provider)
    bridge.adopt_run(run_dir)
    original_run = (run_dir / "run.json").read_bytes()
    if operation == "worker_regeneration":
        prepared = bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing())
        provider.delays[prepared.model] = 30.0
        stage_id = prepared.stage_id
    else:
        prepared = bridge.prepare_synthesis_rerun("test-operator", pricing_evidence=_pricing())
        provider.delays[prepared.model] = 30.0
        stage_id = prepared.stage_id
    assert stage_id is not None
    calls_before = len(provider.calls)

    async def _cancel_during_dispatch():
        if operation == "worker_regeneration":
            bridge.approve_and_regenerate(prepared)
        else:
            bridge.approve_and_rerun_synthesis(prepared)
        await asyncio.sleep(0.02)
        assert len(provider.calls) == calls_before + 1
        await bridge.cancel()
        await asyncio.sleep(0)

    asyncio.run(_cancel_during_dispatch())

    attempt = prepared.attempt_number
    cancelled = _att_meta(run_dir, stage_id, attempt)
    assert cancelled["state"] == "failed"
    assert cancelled["failure"]["type"] == "cancelled"
    assert cancelled["failure"]["provider_side_outcome_unknown"] is True
    assert (run_dir / "run.json").read_bytes() == original_run
    assert len(provider.calls) == calls_before + 1  # no automatic retry
    events = _events(run_dir)
    assert any(
        event["event"] == "stage_failed"
        and event.get("meta", {}).get("error_type") == "cancelled"
        for event in events
    )
    finished_event = (
        "worker_regeneration_finished"
        if operation == "worker_regeneration"
        else "synthesis_rerun_finished"
    )
    assert any(
        event["event"] == finished_event
        and event.get("meta", {}).get("state") == "failed"
        and event.get("meta", {}).get("error_type") == "cancelled"
        for event in events
    )


# ---------------------------------------------------------------------------
# 11. Mick clarification O-2 (with O-3's exact stage label)
# ---------------------------------------------------------------------------


def test_cancelled_sibling_keeps_cancelled_cause_while_run_remains_failed_internal_error(
    tmp_path,
):
    job_path, _job = make_job_tree(tmp_path)
    holder: dict[str, FakeRoutedProvider] = {}
    bridge = CampaignBridge(
        tmp_path / ".bots5" / "runs",
        provider_factory=lambda job: {"openrouter": holder["provider"]},
    )
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("test-operator", pricing_evidence=_pricing())
    run_dir = prepared.run_dir

    def _disk_fault_on_w1_output(request: CompletionRequest) -> None:
        if request.model == "model-w1":
            # Deterministic disk fault: w1's output path is a DIRECTORY, so
            # persisting its completion raises StorageError out of the run
            # pipeline — a generic run failure while w2 is still in flight.
            (run_dir / "stages" / "w1.att1.md").mkdir(parents=True, exist_ok=True)

    holder["provider"] = FakeRoutedProvider(
        delays={"model-w2": 0.5}, on_request=_disk_fault_on_w1_output
    )

    async def _drive():
        bridge.approve_and_start(prepared)
        with pytest.raises(Bots5Error):  # the generic failure propagates
            await bridge.run_to_completion()

    asyncio.run(_drive())

    # Run level: FAILED with internal_error — the run is never presented as
    # cancelled (O-2), read from the durable artifacts.
    run_doc = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_doc["state"] == "failed"
    events = _events(run_dir)
    run_failed = [event for event in events if event["event"] == "run_failed"]
    assert len(run_failed) == 1
    assert run_failed[0]["meta"]["error_type"] == "internal_error"
    assert not any(event["event"] == "run_cancelled" for event in events)

    # Stage level: the sibling cancelled during that failure keeps its OWN
    # cause ("cancelled", O-3) and its provider-side uncertainty (O-2).
    sibling = _att_meta(run_dir, "w2", 1)
    assert sibling["state"] == "failed"
    assert sibling["failure"]["type"] == "cancelled"
    assert sibling["failure"]["message"] == (
        "stage was cancelled while the run failed with an internal error"
    )
    assert sibling["failure"]["provider_side_outcome_unknown"] is True
    # The transitional label never persists in-process (O-3/N-4).
    for stage_id in ("w2", "synth"):
        meta = _att_meta(run_dir, stage_id, 1)
        assert meta["failure"]["type"] != "cancelled_pending"


# ---------------------------------------------------------------------------
# 12. D-12 / V-6 durability: a failed output write must not advance metadata
# ---------------------------------------------------------------------------


def _d12_record(stage_id: str, state: StageState) -> StageRecord:
    return StageRecord(
        id=stage_id,
        provider="openrouter",
        requested_model="model-w1",
        state=state,
    )


def test_persist_stage_attempt_output_fault_leaves_metadata_unadvanced(tmp_path, monkeypatch):
    """D-12 (oracle V-6): the in-place terminal update writes output FIRST.

    ``persist_stage_attempt(create=False, text=...)`` is the queued → running →
    terminal transition and its metadata write is what durably advertises the
    terminal state and ``output_path``. The output artifact is therefore
    written BEFORE the metadata: a failure between the two writes must leave
    the metadata un-advanced rather than durable-claiming a succeeded stage
    whose artifact cannot be read (a green dock stage that errors when the
    operator expands it). The create=True claim path keeps its metadata-first
    ordering: an overwrite refusal must leave the directory byte-identical.
    """
    dirs = create_run_tree(
        tmp_path / ".bots5" / "runs", "bots5-d12-durability-20260101T000000Z-00000001"
    )
    meta_path = dirs.stages / "w1.att1.json"

    # The engine's real sequence: exclusive claim (create=True, queued), then
    # the in-place running marker (create=False, no text).
    persist_stage_attempt(dirs, _d12_record("w1", StageState.QUEUED), attempt_number=1, create=True)
    persist_stage_attempt(dirs, _d12_record("w1", StageState.RUNNING), attempt_number=1)
    before = meta_path.read_bytes()
    assert json.loads(before)["state"] == "running"
    assert json.loads(before)["output_path"] is None

    # FAULT-INJECTION: the output (.md) write fails on the terminal update.
    def _disk_fault(path: Path, text: str) -> None:
        raise StorageError(f"injected disk fault writing {path}")

    monkeypatch.setattr("bots5.storage.atomic_write_text", _disk_fault)
    with pytest.raises(StorageError, match="injected disk fault"):
        persist_stage_attempt(
            dirs,
            _d12_record("w1", StageState.SUCCEEDED),
            attempt_number=1,
            text="worker output",
            create=False,
        )

    # THE OLD-ORDERING CATCHER: the durable metadata is byte-identical to the
    # pre-call record — it was NOT advanced to a succeeded state advertising
    # an output that cannot be read. Under the old ordering the metadata was
    # written before the faulting output write, so these assertions fail.
    assert meta_path.read_bytes() == before
    durable = json.loads(meta_path.read_text(encoding="utf-8"))
    assert durable["state"] == "running"
    assert durable["output_path"] is None
    assert not (dirs.root / "stages" / "w1.att1.md").exists()

    # HAPPY PATH proves the ordering rather than merely the exception: with
    # the fault replaced by a recording writer that delegates to the REAL
    # writer (captured at import time), the output lands while the durable
    # metadata is still un-advanced, and the terminal update then round-trips
    # (succeeded metadata + readable artifact).
    wrote_while_unadvanced: list[bool] = []

    def _recording_write(path: Path, text: str) -> None:
        durable_now = json.loads(meta_path.read_text(encoding="utf-8"))
        wrote_while_unadvanced.append(durable_now["state"] != "succeeded")
        atomic_write_text(path, text)  # the REAL writer, bound at import time

    monkeypatch.setattr("bots5.storage.atomic_write_text", _recording_write)
    persist_stage_attempt(
        dirs,
        _d12_record("w1", StageState.SUCCEEDED),
        attempt_number=1,
        text="worker output",
        create=False,
    )
    # The output was durably written while the metadata was NOT yet succeeded.
    assert wrote_while_unadvanced == [True]
    durable = json.loads(meta_path.read_text(encoding="utf-8"))
    assert durable["state"] == "succeeded"
    assert durable["output_path"] == "stages/w1.att1.md"
    assert (dirs.root / "stages" / "w1.att1.md").read_text(encoding="utf-8") == "worker output"
