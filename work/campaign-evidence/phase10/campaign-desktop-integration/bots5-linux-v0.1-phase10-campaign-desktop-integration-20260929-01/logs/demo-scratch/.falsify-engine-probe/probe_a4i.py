"""A4: cache-only tamper (both directions) + display surfaces."""
import asyncio, hashlib, json, sys, os, shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.core.campaign import CampaignBridge, project_run
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

def setup(tag, threshold):
    root = TMP/tag
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True)
    job_path, _ = make_job_tree(root, threshold=threshold)
    provider = FakeRoutedProvider()
    bridge = CampaignBridge(root/".bots5"/"runs", provider_factory=lambda j: {"openrouter": provider})
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("op")
    async def drive():
        bridge.approve_and_start(prepared); return await bridge.run_to_completion()
    res = asyncio.run(drive())
    return root, res, prepared.job

root, res, job = setup("a4gate7", 0.025)
run_dir = res.run_dir; synth = job.synthesis
snap_pf = build_preflight_snapshot(job)
selection = read_selection(run_dir)
dep_attempts={d: selection.get(d,1) for d in synth.depends_on}
dep_digests={}; texts=[]
for dep in synth.depends_on:
    data=(run_dir/"stages"/f"{dep}.att{dep_attempts[dep]}.md").read_bytes()
    dep_digests[dep]=hashlib.sha256(data).hexdigest(); texts.append((dep,data.decode()))
route=dict(declared_provider_routes(job)[synth.provider])
op_snap=OperationSnapshot(operation="synthesis_rerun",target_run_id=res.run_id,stage_id=synth.id,
    attempt_number=next_attempt_number(run_dir,synth.id),model=synth.model,provider_route=route,
    system_message=snap_pf.system_messages[synth.id],user_message=render_synthesis_user_message(texts),
    dependency_attempts=dep_attempts,dependency_digests=dep_digests,
    preflight_digest=snap_pf.preflight_digest,operation_digest="")
op_snap=replace(op_snap,operation_digest=op_snap.compute_digest())
ap=ApprovalRecord(approval_id="ap-gate7",approved_at="n",approved_by="op",
    preflight_digest=op_snap.preflight_digest,scope="synthesis_rerun",
    target={"run_id":res.run_id,"stage_id":synth.id,"attempt_number":op_snap.attempt_number})

# usage.json per_attempt inflated; stage records honest (0.01 each => durable 0.02 < 0.025)
usage=json.loads((run_dir/"usage.json").read_text())
for k,e in usage["per_attempt"].items():
    if k.startswith(("w1.","w2.")): e["cost_usd"]="7.00"; e["cost_known"]=True
(run_dir/"usage.json").write_text(json.dumps(usage))
prov=FakeRoutedProvider()
try:
    rec=asyncio.run(rerun_synthesis(job,{"openrouter":prov},run_dir=run_dir,run_id=res.run_id,snapshot=op_snap,approval=ap))
    print(f"A: inflated cache only -> DISPATCHED calls={len(prov.calls)} state={rec.state.value}")
except Exception as ex:
    print(f"A: refused {type(ex).__name__}: {ex}; calls={len(prov.calls)}")
p=project_run(run_dir)
print("projection after A: selected=",p.selected_spend.get("cost_usd_known_sum"),
      "aggregate_display=",p.aggregate_cost.get("cost_usd_known_sum"),
      "live=",p.live_cost["known_subtotal_usd"], "warnings=",len(p.integrity_warnings))

# B: deflate cache below durable truth; check gate under-count
root2, res2, job2 = setup("a4gate8", 0.025)
run_dir2=res2.run_dir; synth2=job2.synthesis
snap_pf2=build_preflight_snapshot(job2); sel2=read_selection(run_dir2)
da={d: sel2.get(d,1) for d in synth2.depends_on}; dd={}; tx=[]
for dep in synth2.depends_on:
    data=(run_dir2/"stages"/f"{dep}.att{da[dep]}.md").read_bytes()
    dd[dep]=hashlib.sha256(data).hexdigest(); tx.append((dep,data.decode()))
osnap=OperationSnapshot(operation="synthesis_rerun",target_run_id=res2.run_id,stage_id=synth2.id,
    attempt_number=next_attempt_number(run_dir2,synth2.id),model=synth2.model,
    provider_route=dict(declared_provider_routes(job2)[synth2.provider]),
    system_message=snap_pf2.system_messages[synth2.id],user_message=render_synthesis_user_message(tx),
    dependency_attempts=da,dependency_digests=dd,preflight_digest=snap_pf2.preflight_digest,operation_digest="")
osnap=replace(osnap,operation_digest=osnap.compute_digest())
ap2=ApprovalRecord(approval_id="ap-gate8",approved_at="n",approved_by="op",
    preflight_digest=osnap.preflight_digest,scope="synthesis_rerun",
    target={"run_id":res2.run_id,"stage_id":synth2.id,"attempt_number":osnap.attempt_number})
# durable workers expensive
for sid in ("w1","w2"):
    q=run_dir2/"stages"/f"{sid}.att1.json"; m=json.loads(q.read_text()); m["cost_usd"]="7.00"; m["cost_known"]=True; q.write_text(json.dumps(m))
# usage cache shows cheap costs (stale-low)
u=json.loads((run_dir2/"usage.json").read_text())
prov2=FakeRoutedProvider()
try:
    rec=asyncio.run(rerun_synthesis(job2,{"openrouter":prov2},run_dir=run_dir2,run_id=res2.run_id,snapshot=osnap,approval=ap2))
    print(f"B: stale-low cache vs expensive stages -> DISPATCHED calls={len(prov2.calls)} state={rec.state.value}")
except Exception as ex:
    print(f"B: refused {type(ex).__name__}: {ex}; calls={len(prov2.calls)}")
