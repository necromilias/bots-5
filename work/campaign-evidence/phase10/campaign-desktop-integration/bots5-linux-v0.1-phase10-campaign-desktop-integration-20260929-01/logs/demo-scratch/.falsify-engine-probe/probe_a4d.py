"""A4: rerun_synthesis cost gate — honest inflated per_attempt vs durable stage records."""
import asyncio, hashlib, json, sys, os, shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.core.campaign import CampaignBridge
from bots5.models import ApprovalRecord, OperationSnapshot
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import build_preflight_snapshot, declared_provider_routes, next_attempt_number, rerun_synthesis
from bots5.rendering import render_synthesis_user_message
from bots5.storage import read_selection
from tests.helpers import make_job_tree

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self):
        super().__init__("offline-fake-api-key", base_url="https://openrouter.ai/api/v1")
        self.calls=[]
    async def complete(self, request):
        self.calls.append(request)
        return CompletionResult(output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

root = TMP/"a4gate2"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root, threshold=0.025)
provider = FakeRoutedProvider()
bridge = CampaignBridge(root/".bots5"/"runs", provider_factory=lambda j: {"openrouter": provider})
bridge.load_job(job_path)
prepared = bridge.prepare_full_run("op")
async def drive():
    bridge.approve_and_start(prepared); return await bridge.run_to_completion()
res = asyncio.run(drive())
run_dir = res.run_dir
job = prepared.job; synth = job.synthesis
snap_pf = build_preflight_snapshot(job)
selection = read_selection(run_dir)
dep_attempts = {dep: selection.get(dep,1) for dep in synth.depends_on}
dep_digests={}; texts=[]
for dep in synth.depends_on:
    data=(run_dir/"stages"/f"{dep}.att{dep_attempts[dep]}.md").read_bytes()
    dep_digests[dep]=hashlib.sha256(data).hexdigest(); texts.append((dep,data.decode()))
route = dict(declared_provider_routes(job)[synth.provider])
op_snap = OperationSnapshot(operation="synthesis_rerun", target_run_id=res.run_id,
    stage_id=synth.id, attempt_number=next_attempt_number(run_dir, synth.id), model=synth.model,
    provider_route=route, system_message=snap_pf.system_messages[synth.id],
    user_message=render_synthesis_user_message(texts), dependency_attempts=dep_attempts,
    dependency_digests=dep_digests, preflight_digest=snap_pf.preflight_digest, operation_digest="")
op_snap = replace(op_snap, operation_digest=op_snap.compute_digest())
ap = ApprovalRecord(approval_id="ap-gate2", approved_at="n", approved_by="op",
                    preflight_digest=op_snap.preflight_digest, scope="synthesis_rerun",
                    target={"run_id":res.run_id,"stage_id":synth.id,"attempt_number":op_snap.attempt_number})

usage = json.loads((run_dir/"usage.json").read_text())
# INFLATE ONLY the usage.json cache (per_attempt + cumulative + selected + aggregate),
# leaving durable stage attempt records at 0.01 each.
for key,entry in usage["per_attempt"].items():
    entry["cost_usd"]="5.00"; entry["cost_known"]=True
for k in ("cumulative_spend","selected_spend","aggregate"):
    usage[k]["cost_usd_known_sum"]="15.00"
(run_dir/"usage.json").write_text(json.dumps(usage))
prov2 = FakeRoutedProvider()
try:
    rec = asyncio.run(rerun_synthesis(job, {"openrouter": prov2}, run_dir=run_dir, run_id=res.run_id,
                                       snapshot=op_snap, approval=ap))
    print(f"RESULT: rerun DISPATCHED state={rec.state.value} calls={len(prov2.calls)} "
          f"(gate threshold was 0.025; durable worker cost was 0.02)")
except Exception as e:
    print(f"RESULT: refused {type(e).__name__}: {e}; calls={len(prov2.calls)}")
# what does the projection show now?
from bots5.core.campaign import project_run
p = project_run(run_dir)
print(json.dumps({"projected_selected": p.selected_spend.get("cost_usd_known_sum"),
                  "live_subtotal": p.live_cost["known_subtotal_usd"],
                  "warnings": list(p.integrity_warnings)[:2]}, indent=2))
