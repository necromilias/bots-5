"""Phase 10 desktop preflight/approval contracts (design rows T0.1, T0.2, T0.3).

Test contracts written from the SEALED DESIGN (``DESIGN_SEAL_v4.json`` and the
design documents in the campaign pack), not from the implementation:

- ``PREFLIGHT_APPROVAL_STATE_MACHINE.md`` §1/§2/§3/§4/§8: zero-spend validation,
  frozen snapshots, one-shot approval binding to digest + scope + target,
  provider-object route validation, and the zero-spend proof points;
- ``CAMPAIGN_EVIDENCE_EVOLUTION.md`` §1/§2: the version marker and the
  preflight.json / attempt-grammar artifact layout;
- ``DESKTOP_SURFACE_AND_LIFECYCLE.md`` §5.2 with Mick clarifications O-3 (the
  stage-level cancellation label is exactly ``error_type = "cancelled"``).

Coverage map (each numbered requirement is its own test):
1. zero-spend validation (no provider, no run directory, unchanged filesystem);
2. preparing an operation writes nothing, constructs no provider, and the
   approval is a separate explicit act;
3. no provider construction or request before the approve step;
4. approval binding carries digest + scope + target and is accepted only when
   all three match (digest mismatch refused);
5. invalidation, one test each: changed input bytes, wrong scope, wrong
   target, and a provider object whose route or kind does not match the
   frozen route;
6. evidence-v2 runs write preflight.json + stages/<id>.att1.json with the
   preflight digest; a legacy run writes exactly the version-1 artifact set;
7. cancellation leaves a durable terminal record with the run-level
   cancelled state and stage error_type "cancelled" — never a success and
   never an internal error.

Every test is deterministic and offline: providers are route-faithful fakes
(subclasses of the real ``OpenRouterProvider`` so the engine's kind-specific
provider-object route validation accepts them) that never touch the network.
Fixtures are built on disk under pytest ``tmp_path``; nothing is copied from
or written into ``evidence/**``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bots5.core.campaign import CampaignBridge
from bots5.errors import ApprovalInvalidatedError
from bots5.manifest import load_job
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider
from bots5.rendering import render_worker_user_message
from bots5.runner import run_job
from bots5.storage import approval_consumed, load_preflight

from .helpers import make_job_tree

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _pricing():
    return {"entries": [{"provider": "openrouter", "input_usd_per_1m": "1.25",
                         "output_usd_per_1m": "2.50", "rate_source": "operator test citation",
                         "observed_at": "2026-09-29T12:00:00Z"}]}


class FakeRoutedProvider(OpenRouterProvider):
    """Route-faithful offline fake.

    A subclass of the real ``OpenRouterProvider`` so the engine's
    kind-specific provider-object route validation (isinstance + base_url)
    accepts it, with ``complete()`` fully faked: no endpoint, no network, no
    environment dependency (the API key is a fake literal). The fake key
    doubles as the "secret" no artifact may ever serialize.
    """

    def __init__(
        self,
        *,
        results: dict[str, str] | None = None,
        delays: dict[str, float] | None = None,
        costs: dict[str, Decimal] | None = None,
        on_request=None,
        base_url: str = OPENROUTER_BASE_URL,
    ):
        super().__init__("offline-fake-api-key", base_url=base_url)
        self.results = dict(results or {})
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
        return CompletionResult(
            output_text=self.results.get(request.model, f"output:{request.model}"),
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
    """Wrong-kind fake used to prove route validation refuses a kind swap."""

    def __init__(self, base_url: str):
        super().__init__(base_url)
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        raise AssertionError("a swapped-kind provider must never be dispatched")


class _WorkingLocalFake(_FakeOpenAICompatible):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        return CompletionResult(output_text="local-output", requested_model=request.model,
                               finish_reason="stop", returned_model=request.model)


class _CountingFactory:
    """Provider factory that records every construction.

    The zero-spend state machine (§1: UNLOADED → JOB_LOADED →
    VALIDATED_ZERO_SPEND → PREFLIGHT_PREPARED) must keep this at zero; only
    the explicit approve step may construct providers.
    """

    def __init__(self, provider: FakeRoutedProvider):
        self.provider = provider
        self.constructions = 0

    def __call__(self, job):
        self.constructions += 1
        return {"openrouter": self.provider}


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


def _events(run_dir: Path) -> list[dict]:
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _prepared_full_run(tmp_path: Path, provider, *, name: str | None = None):
    """A loaded job plus its zero-spend full-run prepared operation.

    By default the job tree is built directly in ``tmp_path`` (the
    ``tests.helpers.make_job_tree`` layout); ``name`` nests an independent
    job tree in a subdirectory of the same tmp_path.
    """
    base = tmp_path if name is None else tmp_path / name
    if name is not None:
        base.mkdir(parents=True, exist_ok=True)
    job_path, _job = make_job_tree(base)
    bridge = CampaignBridge(
        tmp_path / ".bots5" / "runs",
        provider_factory=lambda job: {"openrouter": provider},
    )
    bridge.load_job(job_path)
    return bridge, bridge.prepare_full_run("test-operator", pricing_evidence=_pricing())


def test_paid_approval_without_pricing_is_refused_without_writes_or_provider_calls(tmp_path):
    provider = FakeRoutedProvider()
    factory = _CountingFactory(provider)
    job_path, _job = make_job_tree(tmp_path)
    bridge = CampaignBridge(tmp_path / ".bots5" / "runs", provider_factory=factory)
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("test-operator")
    before = _snapshot_tree(tmp_path)

    with pytest.raises(ApprovalInvalidatedError, match="pricing evidence"):
        bridge.approve_and_start(prepared)

    assert factory.constructions == 0
    assert provider.calls == []
    assert not prepared.run_dir.exists()
    assert _snapshot_tree(tmp_path) == before


def test_paid_pricing_evidence_records_rates_route_bound_and_fixed_basis(tmp_path):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)
    evidence = prepared.approval.pricing_evidence
    assert evidence is not None
    entry, = evidence["entries"]
    assert entry["input_usd_per_1m"] == "1.25"
    assert entry["output_usd_per_1m"] == "2.50"
    assert entry["rate_source"] == "operator test citation"
    assert entry["observed_at"] == "2026-09-29T12:00:00Z"
    assert entry["provider"] == "openrouter"
    assert entry["route"] == prepared.snapshot.provider_routes["openrouter"]
    assert entry["models"] == ["model-w1", "model-w2", "model-synth"]
    basis = entry["basis"]
    assert "ceil(len(compiled_prompt_text) / 4)" in basis
    assert "stage_count=3" in basis
    assert all(f"{stage}(input_token_ceiling=" in basis for stage in ("w1", "w2", "synth"))
    prompts = _worker_prompts(prepared)
    expected = sum(
        (Decimal((len(prompt) + 3) // 4) * Decimal("1.25") + Decimal(100) * Decimal("2.50"))
        / Decimal(1_000_000)
        for prompt in prompts.values()
    )
    assert Decimal(entry["conservative_upper_bound_usd"]) == expected
    assert "input=1.25" in prepared.summary
    assert "operator test citation" in prepared.summary
    assert "2026-09-29T12:00:00Z" in prepared.summary
    assert entry["conservative_upper_bound_usd"] in prepared.summary


def test_pricing_approval_is_bound_to_current_models_and_ceilings(tmp_path):
    provider = FakeRoutedProvider()
    _bridge, prepared = _prepared_full_run(tmp_path, provider)
    evidence = json.loads(json.dumps(prepared.approval.pricing_evidence))
    evidence["entries"][0]["models"] = ["stale-model"]
    approval = replace(prepared.approval, pricing_evidence=evidence)

    with pytest.raises(ApprovalInvalidatedError, match="pricing"):
        asyncio.run(run_job(prepared.job, {"openrouter": provider}, run_id=prepared.run_id,
                            snapshot=prepared.snapshot, approval=approval))
    assert provider.calls == []
    assert not prepared.run_dir.exists()


def test_local_only_approval_needs_no_pricing_evidence(tmp_path):
    job_path, job_data = make_job_tree(tmp_path)
    for spec in job_data["workers"]:
        spec["provider"] = "local_openai"
    job_data["synthesis"]["provider"] = "local_openai"
    job_data["schema_version"] = 2
    job_data["providers"] = {"local_openai": {"base_url": "http://127.0.0.1:9/v1"}}
    job_path.write_text(json.dumps(job_data), encoding="utf-8")
    provider = _WorkingLocalFake("http://127.0.0.1:9/v1")
    bridge = CampaignBridge(tmp_path / ".bots5" / "runs", provider_factory=lambda job: {"local_openai": provider})
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("test-operator")
    assert prepared.approval.pricing_evidence is None
    asyncio.run(_run_local(bridge, prepared))
    assert len(provider.calls) == 3


async def _run_local(bridge, prepared):
    bridge.approve_and_start(prepared)
    return await bridge.run_to_completion()


def _worker_prompts(prepared):
    user = render_worker_user_message([("source", "hello\n")])
    specs = list(prepared.snapshot.worker_specs)
    if prepared.snapshot.synthesis_spec is not None:
        specs.append(prepared.snapshot.synthesis_spec)
    return {spec["id"]: prepared.snapshot.system_messages[spec["id"]] + "\n" + user for spec in specs}


def _run_prepared_full_run(bridge: CampaignBridge, prepared) -> Path:
    async def _drive():
        bridge.approve_and_start(prepared)
        return await bridge.run_to_completion()

    result = asyncio.run(_drive())
    assert result.state.value == "succeeded"
    return result.run_dir


# ---------------------------------------------------------------------------
# 1. Zero-spend validation (T0.1)
# ---------------------------------------------------------------------------


def test_desktop_load_job_preview_creates_no_run_dir_and_no_provider_call(tmp_path):
    job_path, _job = make_job_tree(tmp_path)
    factory = _CountingFactory(FakeRoutedProvider())
    bridge = CampaignBridge(tmp_path / ".bots5" / "runs", provider_factory=factory)
    before = _snapshot_tree(tmp_path)

    summary = bridge.load_job(job_path)
    assert summary["zero_spend"] is True
    validated = bridge.validate()
    assert validated["zero_spend"] is True

    # No provider was ever constructed and no request could exist.
    assert factory.constructions == 0
    assert factory.provider.calls == []
    # No run directory was created (the job's declared runs dir stays absent).
    assert not (tmp_path / ".bots5" / "runs").exists()
    # The filesystem is byte-for-byte unchanged by loading and validating.
    assert _snapshot_tree(tmp_path) == before


# ---------------------------------------------------------------------------
# 2. Preparing an operation is zero-spend; approval is a separate act
# ---------------------------------------------------------------------------


def test_preparing_operation_writes_nothing_and_constructs_no_provider(tmp_path):
    provider = FakeRoutedProvider()
    factory = _CountingFactory(provider)
    job_path, _job = make_job_tree(tmp_path)
    bridge = CampaignBridge(tmp_path / ".bots5" / "runs", provider_factory=factory)
    bridge.load_job(job_path)
    before = _snapshot_tree(tmp_path)

    prepared = bridge.prepare_full_run("test-operator", pricing_evidence=_pricing())
    assert factory.constructions == 0
    assert provider.calls == []
    assert _snapshot_tree(tmp_path) == before  # preparing wrote NOTHING
    assert not prepared.run_dir.exists()  # no run directory either

    # A real v2 run, so the operation-specific prepares can be exercised.
    run_dir = _run_prepared_full_run(bridge, prepared)
    assert factory.constructions == 1  # only the approval constructed providers
    tree_after_run = _snapshot_tree(run_dir)
    bridge.adopt_run(run_dir)

    regen_prepared = bridge.prepare_regeneration("w1", "model-w1-alt", "test-operator", pricing_evidence=_pricing())
    rerun_prepared = bridge.prepare_synthesis_rerun("test-operator", pricing_evidence=_pricing())

    # Preparing regeneration/rerun is read-only too: the run directory is
    # byte-identical, no provider exists, and the new approvals are NOT
    # consumed — approval is a separate explicit operator act.
    assert _snapshot_tree(run_dir) == tree_after_run
    assert factory.constructions == 1
    assert provider.calls != [] and len(provider.calls) == 3  # only the full run spent
    assert not approval_consumed(run_dir, regen_prepared.approval.approval_id)
    assert not approval_consumed(run_dir, rerun_prepared.approval.approval_id)
    assert not (run_dir / "approvals" / f"{regen_prepared.approval.approval_id}.json").exists()
    assert not (run_dir / "approvals" / f"{rerun_prepared.approval.approval_id}.json").exists()


# ---------------------------------------------------------------------------
# 3. No provider before explicit approval (T0.3)
# ---------------------------------------------------------------------------


def test_desktop_requires_explicit_approval_before_any_provider_construction(tmp_path):
    provider = FakeRoutedProvider()
    factory = _CountingFactory(provider)
    job_path, _job = make_job_tree(tmp_path)
    bridge = CampaignBridge(tmp_path / ".bots5" / "runs", provider_factory=factory)
    bridge.load_job(job_path)
    bridge.validate()
    prepared = bridge.prepare_full_run("test-operator", pricing_evidence=_pricing())

    # Through load, validate and prepare: the factory is untouched, so no
    # provider object exists and no provider request can have occurred.
    assert factory.constructions == 0
    assert provider.calls == []
    assert not prepared.run_dir.exists()  # an abandoned approval leaves zero bytes

    async def _drive():
        bridge.approve_and_start(prepared)  # the ONLY provider construction point
        return await bridge.run_to_completion()

    asyncio.run(_drive())

    # Only the approve step constructed the provider and started spending.
    assert factory.constructions == 1
    assert len(provider.calls) == 3  # two workers + synthesis, after approval
    assert prepared.run_dir.is_dir()


# ---------------------------------------------------------------------------
# 4. Approval binding: digest + scope + target (T0.2)
# ---------------------------------------------------------------------------


def test_approval_binding_carries_digest_scope_target_and_is_accepted_when_all_match(
    tmp_path,
):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)
    approval = prepared.approval

    # The approval carries the preflight digest, the scope and the target.
    assert approval.preflight_digest == prepared.snapshot.preflight_digest
    assert approval.scope == "full_run"
    assert approval.target == {
        "run_id": prepared.run_id,
        "stage_id": None,
        "attempt_number": None,
    }

    # All three match: the engine accepts the approval and executes the run.
    result = asyncio.run(
        run_job(
            prepared.job,
            {"openrouter": provider},
            run_id=prepared.run_id,
            snapshot=prepared.snapshot,
            approval=approval,
        )
    )
    assert result.state.value == "succeeded"
    document = load_preflight(result.run_dir)
    assert document["preflight_digest"] == approval.preflight_digest
    assert document["approval"] == approval.to_dict()

    # A tampered digest is refused: identity assertion 1, before anything.
    provider2 = FakeRoutedProvider()
    bridge2, prepared2 = _prepared_full_run(tmp_path, provider2, name="second")
    tampered = replace(prepared2.approval, preflight_digest="0" * 64)
    with pytest.raises(ApprovalInvalidatedError):
        asyncio.run(
            run_job(
                prepared2.job,
                {"openrouter": provider2},
                run_id=prepared2.run_id,
                snapshot=prepared2.snapshot,
                approval=tampered,
            )
        )
    assert provider2.calls == []
    assert not prepared2.run_dir.exists()  # a refused approval creates no run dir


# ---------------------------------------------------------------------------
# 5. Invalidation, one test each (T0.2/T0.3)
# ---------------------------------------------------------------------------


def test_preflight_approval_does_not_silently_apply_to_swapped_input_bytes(tmp_path):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)

    # The referenced input bytes change after the preflight was prepared.
    input_path = tmp_path / "input" / "source.txt"
    input_path.write_text("TAMPERED-BYTES\n", encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    with pytest.raises(ApprovalInvalidatedError):
        asyncio.run(
            run_job(
                prepared.job,
                {"openrouter": provider},
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            )
        )
    assert provider.calls == []  # no provider request
    assert not prepared.run_dir.exists()  # no run directory
    assert _snapshot_tree(tmp_path) == before  # nothing was written at all


def test_approval_refused_when_scope_does_not_match_the_operation(tmp_path):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)
    tampered = replace(prepared.approval, scope="worker_regeneration:w1")

    with pytest.raises(ApprovalInvalidatedError):
        asyncio.run(
            run_job(
                prepared.job,
                {"openrouter": provider},
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=tampered,
            )
        )
    assert provider.calls == []
    assert not prepared.run_dir.exists()


def test_approval_refused_when_target_does_not_match_the_operation(tmp_path):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)
    tampered = replace(
        prepared.approval,
        target={"run_id": "another-run-id", "stage_id": None, "attempt_number": None},
    )

    with pytest.raises(ApprovalInvalidatedError):
        asyncio.run(
            run_job(
                prepared.job,
                {"openrouter": provider},
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=tampered,
            )
        )
    assert provider.calls == []
    assert not prepared.run_dir.exists()


def test_full_run_approval_requires_exact_full_run_target_shape(tmp_path):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)

    for target in (
        {"run_id": prepared.run_id, "stage_id": "w1", "attempt_number": None},
        {"run_id": prepared.run_id, "stage_id": None, "attempt_number": 99},
    ):
        tampered = replace(prepared.approval, target=target)
        with pytest.raises(ApprovalInvalidatedError):
            asyncio.run(
                run_job(
                    prepared.job,
                    {"openrouter": provider},
                    run_id=prepared.run_id,
                    snapshot=prepared.snapshot,
                    approval=tampered,
                )
            )
        assert provider.calls == []
        assert not prepared.run_dir.exists()


def test_provider_object_route_or_kind_swap_is_refused_before_dispatch(tmp_path):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)

    # Route swap: same kind, different base_url than the frozen route.
    swapped_base = FakeRoutedProvider(base_url="https://other.example/api/v1")
    with pytest.raises(ApprovalInvalidatedError):
        asyncio.run(
            run_job(
                prepared.job,
                {"openrouter": swapped_base},
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            )
        )
    assert swapped_base.calls == []

    # Kind swap: a local_openai instance where the frozen route says openrouter.
    swapped_kind = _FakeOpenAICompatible("http://127.0.0.1:9/v1")
    with pytest.raises(ApprovalInvalidatedError):
        asyncio.run(
            run_job(
                prepared.job,
                {"openrouter": swapped_kind},
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            )
        )
    assert swapped_kind.calls == []

    assert not prepared.run_dir.exists()  # refused before any run directory


# ---------------------------------------------------------------------------
# 6. Version-2 artifacts vs the legacy version-1 artifact set
# ---------------------------------------------------------------------------


def test_evidence_version_2_run_writes_preflight_and_attempt_one_records_with_digest(
    tmp_path,
):
    provider = FakeRoutedProvider()
    bridge, prepared = _prepared_full_run(tmp_path, provider)
    approval = prepared.approval
    run_dir = _run_prepared_full_run(bridge, prepared)

    run_doc = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_doc["evidence_version"] == 2  # exactly one added key

    # preflight.json: frozen snapshot metadata + operation binding + approval.
    document = load_preflight(run_dir)
    assert document["doc_schema_version"] == 1
    assert document["operation"] == "full_run"
    assert document["run_id"] == prepared.run_id
    assert document["preflight_digest"] == approval.preflight_digest
    assert document["approval"]["approval_id"] == approval.approval_id
    assert document["pricing_evidence"] == approval.pricing_evidence
    # O-4 naming: route records serialize api_key_source, never a secret value.
    assert set(document["provider_routes"]) == {"openrouter"}
    route = document["provider_routes"]["openrouter"]
    assert set(route) == {"kind", "base_url", "api_key_env_name", "api_key_source"}
    assert route["kind"] == "openrouter"
    assert route["api_key_env_name"] == "OPENROUTER_API_KEY"
    assert route["api_key_source"] == "environment"
    assert "offline-fake-api-key" not in (run_dir / "preflight.json").read_text(
        encoding="utf-8"
    )

    # The one-shot approval marker is durably consumed.
    assert approval_consumed(run_dir, approval.approval_id)
    assert (run_dir / "approvals" / f"{approval.approval_id}.json").is_file()

    # Every stage writes the attempt-1 grammar carrying the preflight digest.
    for stage_id in ("w1", "w2", "synth"):
        meta = _att_meta(run_dir, stage_id, 1)
        assert meta["attempt_number"] == 1
        assert meta["preflight_digest"] == approval.preflight_digest
        assert (run_dir / "stages" / f"{stage_id}.att1.md").is_file()

    usage = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["evidence_version"] == 2
    assert set(usage["per_attempt"]) == {"w1.att1", "w2.att1", "synth.att1"}


def test_v2_usage_includes_completed_worker_while_sibling_is_still_running(tmp_path):
    observation = {}
    run_dir_holder = {}

    def observe_sibling_dispatch(request):
        if request.model != "model-w2":
            return
        run_dir = run_dir_holder["run_dir"]
        usage = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
        w2 = json.loads((run_dir / "stages" / "w2.att1.json").read_text(encoding="utf-8"))
        observation.update(usage=usage, w2=w2)

    provider = FakeRoutedProvider(
        costs={"model-w1": Decimal("0.01"), "model-w2": Decimal("0.02")},
        on_request=observe_sibling_dispatch,
    )
    bridge, prepared = _prepared_full_run(tmp_path, provider)
    run_dir_holder["run_dir"] = prepared.run_dir

    result = asyncio.run(
        run_job(
            prepared.job,
            {"openrouter": provider},
            run_id=prepared.run_id,
            snapshot=prepared.snapshot,
            approval=prepared.approval,
        )
    )

    assert result.state.value == "succeeded"
    assert observation["w2"]["state"] == "running"
    assert observation["usage"]["per_attempt"]["w1.att1"]["cost_usd"] == "0.01"
    assert observation["usage"]["selected_spend"]["cost_usd_known_sum"] == "0.01"


def test_legacy_run_writes_exactly_the_version_1_artifact_set_with_no_extra_files(
    tmp_path,
):
    job_path, _job = make_job_tree(tmp_path)
    job = load_job(job_path)
    provider = FakeRoutedProvider()

    result = asyncio.run(run_job(job, {"openrouter": provider}, run_id="legacy-v1-run"))
    assert result.state.value == "succeeded"
    run_dir = result.run_dir

    actual = {str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file()}
    assert actual == {
        "run.json",
        "job.resolved.json",
        "usage.json",
        "events.jsonl",
        "result.md",
        "stages/w1.json",
        "stages/w1.md",
        "stages/w2.json",
        "stages/w2.md",
        "stages/synth.json",
        "stages/synth.md",
    }
    run_doc = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "evidence_version" not in run_doc  # no version marker on legacy runs
    assert not (run_dir / "preflight.json").exists()
    assert not (run_dir / "selection.json").exists()
    assert not (run_dir / "approvals").exists()
    assert not list((run_dir / "stages").glob("*.att*"))  # no attempt grammar


# ---------------------------------------------------------------------------
# 7. Cancellation leaves a durable terminal record (§5.2, O-3)
# ---------------------------------------------------------------------------


def test_desktop_cancellation_persists_terminal_durable_record_not_success_or_internal_error(
    tmp_path,
):
    provider = FakeRoutedProvider(delays={"model-w1": 0.5, "model-w2": 0.5})
    job_path, _job = make_job_tree(tmp_path)
    bridge = CampaignBridge(
        tmp_path / ".bots5" / "runs",
        provider_factory=lambda job: {"openrouter": provider},
    )
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("test-operator", pricing_evidence=_pricing())

    async def _drive():
        bridge.approve_and_start(prepared)
        await asyncio.sleep(0.05)  # the worker stages reach RUNNING
        await bridge.cancel()  # explicit operator cancellation
        # cancel() itself verifies from disk that the terminal record landed.

    asyncio.run(_drive())

    run_dir = prepared.run_dir
    run_doc = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_doc["state"] == "cancelled"  # durable run-level cancelled state

    # Every stage carries the exact stage-level cancellation label (O-3):
    # a terminal failure cause, never a success and never an internal error.
    for stage_id in ("w1", "w2", "synth"):
        meta = _att_meta(run_dir, stage_id, 1)
        assert meta["state"] == "failed"
        assert meta["failure"]["type"] == "cancelled"
    # Stages that had started keep their provider-side uncertainty.
    for stage_id in ("w1", "w2"):
        meta = _att_meta(run_dir, stage_id, 1)
        assert meta["failure"]["provider_side_outcome_unknown"] is True

    events = _events(run_dir)
    assert any(event["event"] == "run_cancelled" for event in events)
    assert not any(
        event["event"] in ("run_succeeded", "run_failed", "run_timed_out")
        for event in events
    )
    assert not (run_dir / "result.md").exists()  # cancellation is never a success
    assert bridge.projection(run_dir).display_state == "cancelled"
