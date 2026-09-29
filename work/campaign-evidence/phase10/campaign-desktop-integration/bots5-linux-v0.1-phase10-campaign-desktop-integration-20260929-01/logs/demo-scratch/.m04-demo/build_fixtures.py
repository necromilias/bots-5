"""M0.4 demo fixtures: one completed v1 run + one completed v2 run (deterministic fakes)."""
from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from tests.helpers import FakeProvider, make_job_tree

import bots5.manifest as manifest
from bots5.models import ApprovalRecord
from bots5.runner import build_preflight_snapshot, run_job
from bots5.storage import now_iso

FIX = REPO / ".m04-demo" / "fix"


class FakeKindProvider(OpenRouterProvider):
    """Deterministic offline fake that keeps the approved openrouter route kind."""

    def __init__(self, results=None):
        super().__init__("demo-key")
        self.results = results or {}
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
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


# --- v1 fixture: legacy run_job (no snapshot/approval) -----------------------
v1_root = FIX / "v1tree"
v1_root.mkdir(parents=True, exist_ok=True)
path1, _ = make_job_tree(v1_root)
job1 = manifest.load_job(path1)
res1 = asyncio.run(run_job(job1, {"openrouter": FakeProvider()}, run_id="demo-v1-run"))
print("v1 run:", res1.run_id, res1.state.value, res1.run_dir)

# --- v2 fixture: full-run v2 mode (snapshot + approval, consumed) ------------
v2_root = FIX / "v2tree"
v2_root.mkdir(parents=True, exist_ok=True)
path2, _ = make_job_tree(v2_root)
job2 = manifest.load_job(path2)
snapshot = build_preflight_snapshot(job2)
approval = ApprovalRecord(
    approval_id="approval-demo-full-run-00000001",
    approved_at=now_iso(),
    approved_by="demo-operator",
    preflight_digest=snapshot.preflight_digest,
    scope="full_run",
    target={"run_id": "demo-v2-run"},
)
res2 = asyncio.run(run_job(job2, {"openrouter": FakeKindProvider()}, run_id="demo-v2-run",
                           snapshot=snapshot, approval=approval))
print("v2 run:", res2.run_id, res2.state.value, res2.run_dir)
run_json = json.loads((res2.run_dir / "run.json").read_text())
print("v2 evidence_version:", run_json.get("evidence_version"))
synth_meta = json.loads((res2.run_dir / "stages" / "synth.att1.json").read_text())
print("synth att1 provenance:", synth_meta.get("consumed_dependencies"))
print("v2 job path:", path2)
print("v1 job path:", path1)
