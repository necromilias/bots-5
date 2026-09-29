"""A2: does run_job dispatch the snapshot's frozen system_messages verbatim?"""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.errors import ApprovalInvalidatedError
from bots5.manifest import load_job
from bots5.models import ApprovalRecord
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job, build_preflight_snapshot
from tests.helpers import make_job_tree

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self):
        super().__init__("offline-fake-api-key", base_url="https://openrouter.ai/api/v1")
        self.calls: list[CompletionRequest] = []
    async def complete(self, request):
        self.calls.append(request)
        return CompletionResult(output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

root = TMP/"p15"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
job = load_job(job_path)
snap = build_preflight_snapshot(job)
# attacker edits the contract file on disk AFTER the honest snapshot was built
real = root/"prompts"/"w1.md"
good = real.read_text()
real.write_text(good.replace("Perform worker task w1.", "ATTACK CONTRACT TASK."))
ap = ApprovalRecord(approval_id="ap-p15", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p15","stage_id":None,"attempt_number":None})
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p15", snapshot=snap, approval=ap))
    print(json.dumps({"ran_state": res.state.value, "provider_calls": len(prov.calls)}))
except ApprovalInvalidatedError as e:
    print(f"refused before dispatch: {e}; calls={len(prov.calls)}")
finally:
    real.write_text(good)

# now the core question: does dispatch use snapshot.system_messages or re-read disk?
root = TMP/"p16"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
job = load_job(job_path)
snap = build_preflight_snapshot(job)
evil_msg = "EVIL-FROZEN-MESSAGE " + snap.system_messages["w1"]
tampered_snap = ApprovalInvalidatedError  # placeholder
from dataclasses import replace as dreplace
tampered_snap = dreplace(snap, system_messages={**snap.system_messages, "w1": evil_msg})
# recompute digest so self-integrity passes (attacker fully controls the snapshot object)
tampered_snap = dreplace(tampered_snap, preflight_digest=tampered_snap.compute_digest())
ap = ApprovalRecord(approval_id="ap-p16", approved_at="n", approved_by="op",
                    preflight_digest=tampered_snap.preflight_digest, scope="full_run",
                    target={"run_id":"p16","stage_id":None,"attempt_number":None})
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p16", snapshot=tampered_snap, approval=ap))
    print(json.dumps({"tampered_ran_state": res.state.value,
                      "dispatched_evil_message": any(c.system.startswith("EVIL-FROZEN-MESSAGE") for c in prov.calls),
                      "calls": len(prov.calls)}))
except ApprovalInvalidatedError as e:
    print(f"tampered snapshot refused: {e}")
