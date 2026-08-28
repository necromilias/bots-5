from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from bots5.errors import ProviderError
from bots5.manifest import load_job, validate_referenced_files
from bots5.models import StageState
from bots5.providers.base import CompletionResult
from bots5.runner import run_job

from .helpers import FakeProvider, make_job_tree


def _run(tmp_path, provider, **kwargs):
    path, job_dict = make_job_tree(tmp_path, **kwargs)
    job = load_job(path)
    validate_referenced_files(job)
    return asyncio.run(run_job(job, provider, run_id="test-run"))


def test_worker_and_synthesis_success_persist(tmp_path):
    provider = FakeProvider(
        results={
            "model-w1": "one",
            "model-w2": "two",
            "model-synth": "final",
        }
    )
    result = _run(tmp_path, provider)
    assert result.exit_code == 0
    assert (result.run_dir / "stages" / "w1.md").read_text() == "one"
    assert (result.run_dir / "stages" / "w2.md").read_text() == "two"
    assert (result.run_dir / "result.md").read_text() == "final"


def test_worker_failure_preserves_sibling_and_blocks_synthesis(tmp_path):
    provider = FakeProvider(
        results={"model-w1": "one"},
        failures={"model-w2": ProviderError("boom")},
    )
    result = _run(tmp_path, provider)
    assert result.exit_code == 1
    assert (result.run_dir / "stages" / "w1.md").read_text() == "one"
    assert not (result.run_dir / "result.md").exists()
    synth = json.loads((result.run_dir / "stages" / "synth.json").read_text())
    assert synth["state"] == "skipped"
    assert synth["failure"]["type"] == "dependency_failed"


def test_known_cost_threshold_blocks_synthesis(tmp_path):
    class Costly(FakeProvider):
        async def complete(self, request):
            self.calls.append(request)
            return CompletionResult(
                output_text=f"ok:{request.model}",
                requested_model=request.model,
                known_cost_usd=Decimal("1.50"),
                duration_seconds=0.001,
            )

    provider = Costly()
    result = _run(tmp_path, provider, threshold=2.0)
    assert result.exit_code == 1
    assert len([c for c in result.stages if c.id == "synth" and c.state == StageState.SKIPPED]) == 1
    assert all(call.model != "model-synth" for call in provider.calls)


def test_unknown_partial_cost_aggregation(tmp_path):
    class Partial(FakeProvider):
        async def complete(self, request):
            cost = None if request.model == "model-w2" else Decimal("0.25")
            return CompletionResult(
                output_text=f"ok:{request.model}",
                requested_model=request.model,
                known_cost_usd=cost,
                duration_seconds=0.001,
            )

    result = _run(tmp_path, Partial(), threshold=10.0)
    usage = json.loads((result.run_dir / "usage.json").read_text())
    assert usage["aggregate"]["cost_status"] == "partial"
    assert "w2" in usage["aggregate"]["unknown_cost_stage_ids"]


def test_per_stage_request_timeout(tmp_path):
    provider = FakeProvider(delays={"model-w1": 0.05})
    path, job_dict = make_job_tree(tmp_path)
    job_dict["workers"][0]["timeout_seconds"] = 0.001
    path.write_text(json.dumps(job_dict))
    job = load_job(path)
    result = asyncio.run(run_job(job, provider, run_id="test-run"))
    meta = json.loads((result.run_dir / "stages" / "w1.json").read_text())
    assert meta["state"] == "failed"
    assert meta["failure"]["type"] == "request_timeout"
    assert (result.run_dir / "stages" / "w2.md").exists()


def test_overall_timeout_persists_terminal_state(tmp_path):
    provider = FakeProvider(delays={"model-w1": 0.1, "model-w2": 0.1})
    result = _run(tmp_path, provider, run_timeout=0.01)
    run = json.loads((result.run_dir / "run.json").read_text())
    assert run["state"] == "timed_out"
    for wid in ("w1", "w2"):
        meta = json.loads((result.run_dir / "stages" / f"{wid}.json").read_text())
        assert meta["state"] == "failed"


def test_workers_actually_overlap(tmp_path):
    class Concurrent(FakeProvider):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def complete(self, request):
            self.calls.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return CompletionResult(
                    output_text=f"ok:{request.model}",
                    requested_model=request.model,
                    known_cost_usd=Decimal("0.01"),
                    duration_seconds=0.02,
                )
            finally:
                self.active -= 1

    provider = Concurrent()
    result = _run(tmp_path, provider, workers=3, synthesis=False)
    assert result.exit_code == 0
    assert provider.max_active >= 2


def test_api_key_not_persisted_with_mock_openrouter(tmp_path):
    import httpx
    from bots5.providers.openrouter import OpenRouterProvider

    secret = "super-secret-key"

    async def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "req",
                "model": "returned",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"cost": "0.001"},
            },
        )

    path, job_dict = make_job_tree(tmp_path, workers=1, synthesis=False)
    job = load_job(path)
    provider = OpenRouterProvider(secret, _transport=httpx.MockTransport(handler))
    result = asyncio.run(run_job(job, provider, run_id="secret-run"))
    assert result.exit_code == 0
    for artifact in result.run_dir.rglob("*"):
        if artifact.is_file():
            assert secret not in artifact.read_text(encoding="utf-8")
