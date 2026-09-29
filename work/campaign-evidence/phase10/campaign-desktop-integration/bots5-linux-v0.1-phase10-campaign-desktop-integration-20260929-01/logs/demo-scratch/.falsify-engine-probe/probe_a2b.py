"""A2 probes part 2: swap-after-digest, digest-only-tamper, scope-tamper."""
import asyncio, hashlib, json, sys, os
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10"); TMP.mkdir(exist_ok=True); os.chdir(TMP)
from bots5.errors import ApprovalInvalidatedError
from bots5.models import ApprovalRecord
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job, build_preflight_snapshot
from bots5.manifest import load_job
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

def fresh(name):
    root = TMP / name
    import shutil
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True)
    job_path, _ = make_job_tree(root)
    return root, load_job(job_path)

out = {}

# P4: honest snapshot over GOOD bytes; attacker swaps file AFTER snapshot built,
# approval carries a FORGED (mismatched) preflight_digest equal to the evil digest.
root, job = fresh("p4")
inp = next(p for p in root.rglob("source.txt"))
good = inp.read_bytes()
snap = build_preflight_snapshot(job)          # digest over good bytes
inp.write_bytes(b"EVIL\n")
job_evil = load_job(root / "job.json")
evil_snap = build_preflight_snapshot(job_evil)  # attacker recomputes honestly on evil bytes
ap = ApprovalRecord(approval_id="ap-p4", approved_at="now", approved_by="attacker",
                    preflight_digest=evil_snap.preflight_digest, scope="full_run",
                    target={"run_id": "p4-run", "stage_id": None, "attempt_number": None})
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job_evil, {"openrouter": prov}, run_id="p4-run", snapshot=evil_snap, approval=ap))
    out["P4_swap_then_recompute_whole_chain"] = f"ran state={res.state.value} EVIL_dispatched={'EVIL' in prov.calls[0].user}"
except ApprovalInvalidatedError as e:
    out["P4_swap_then_recompute_whole_chain"] = f"refused: {e}"
inp.write_bytes(good)

# P5: GOOD snapshot kept, only the APPROVAL's digest field forged to evil value.
root, job = fresh("p5")
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p5", approved_at="now", approved_by="op",
                    preflight_digest="f"*64, scope="full_run",
                    target={"run_id": "p5-run", "stage_id": None, "attempt_number": None})
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p5-run", snapshot=snap, approval=ap))
    out["P5_forged_approval_digest"] = f"DEFECT ran state={res.state.value}"
except ApprovalInvalidatedError as e:
    out["P5_forged_approval_digest"] = f"sound refused: {e}; calls={len(prov.calls)}"

# P6: worker model changed after snapshot but approval/snapshot rebuilt ONLY
# partially — mutate job object's worker model and rebuild config-consistency.
root, job = fresh("p6")
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p6", approved_at="now", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id": "p6-run", "stage_id": None, "attempt_number": None})
from dataclasses import replace as dreplace
job2 = dreplace(job, workers=tuple(dreplace(w, model="STOLEN-MODEL") for w in job.workers))
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job2, {"openrouter": prov}, run_id="p6-run", snapshot=snap, approval=ap))
    out["P6_model_swap_vs_frozen_snapshot"] = f"DEFECT ran state={res.state.value} models={[c.model for c in prov.calls]}"
except ApprovalInvalidatedError as e:
    out["P6_model_swap_vs_frozen_snapshot"] = f"sound refused: {e}; calls={len(prov.calls)}"

# P7: wrong-scope approval (worker_regeneration scope) against full_run.
root, job = fresh("p7")
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p7", approved_at="now", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="worker_regeneration:w1",
                    target={"run_id": "p7-run", "stage_id": "w1", "attempt_number": 2})
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p7-run", snapshot=snap, approval=ap))
    out["P7_wrong_scope"] = f"DEFECT ran state={res.state.value}"
except ApprovalInvalidatedError as e:
    out["P7_wrong_scope"] = f"sound refused: {e}; calls={len(prov.calls)}"

# P8: legacy path (no snapshot/approval) — prove zero approval binding there by design.
root, job = fresh("p8")
prov = FakeRoutedProvider()
res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p8-run"))
out["P8_legacy_no_approval_needed"] = f"legacy ran state={res.state.value} calls={len(prov.calls)} (contract: headless unchanged)"

print(json.dumps(out, indent=2))
