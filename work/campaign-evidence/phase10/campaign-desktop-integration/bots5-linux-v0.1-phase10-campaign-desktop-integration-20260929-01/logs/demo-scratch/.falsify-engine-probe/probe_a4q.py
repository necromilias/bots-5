"""A4: v2 run with legacy-shape usage.json -> gate accepts under-counted cache?"""
import asyncio, hashlib, json, sys, os, shutil
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
REPO="/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0,REPO+"/src"); sys.path.insert(0,REPO)
TMP=Path("/tmp/falsify_phase10")
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

root=TMP/"a4legacygate"; shutil.rmtree(root,ignore_errors=True); root.mkdir(parents=True)
job_path,_=make_job_tree(root, threshold=0.025)
bridge=CampaignBridge(root/".bots5"/"runs", provider_factory=lambda j: {"openrouter":FakeRoutedProvider()})
bridge.load_job(job_path)
prepared=bridge.prepare_full_run("op")
async def drive():
    bridge.approve_and_start(prepared); return await bridge.run_to_completion()
res=asyncio.run(drive()); rd=res.run_dir; job=prepared.job; synth=job.synthesis
# durable stage records EXPENSIVE (7.00 each => 14.00 >> 0.025)
for sid in ("w1","w2"):
    q=rd/"stages"/f"{sid}.att1.json"; m=json.loads(q.read_text()); m["cost_usd"]="7.00"; m["cost_known"]=True; q.write_text(json.dumps(m))
# usage.json replaced by LEGACY shape (no per_attempt) claiming cheap costs
(rd/"usage.json").write_text(json.dumps({
  "stages":{"w1":{"cost_usd":"0.01","cost_known":True},"w2":{"cost_usd":"0.01","cost_known":True}},
  "aggregate":{"cost_usd_known_sum":"0.02","cost_status":"known","cost_complete":True,"unknown_cost_stage_ids":[]}}))
snap_pf=build_preflight_snapshot(job); sel=read_selection(rd)
da={d:sel.get(d,1) for d in synth.depends_on}; dd={}; tx=[]
for dep in synth.depends_on:
    data=(rd/"stages"/f"{dep}.att{da[dep]}.md").read_bytes()
    dd[dep]=hashlib.sha256(data).hexdigest(); tx.append((dep,data.decode()))
osnap=OperationSnapshot(operation="synthesis_rerun",target_run_id=res.run_id,stage_id=synth.id,
    attempt_number=next_attempt_number(rd,synth.id),model=synth.model,
    provider_route=dict(declared_provider_routes(job)[synth.provider]),
    system_message=snap_pf.system_messages[synth.id],user_message=render_synthesis_user_message(tx),
    dependency_attempts=da,dependency_digests=dd,preflight_digest=snap_pf.preflight_digest,operation_digest="")
osnap=replace(osnap,operation_digest=osnap.compute_digest())
ap=ApprovalRecord(approval_id="ap-lg",approved_at="n",approved_by="op",
    preflight_digest=osnap.preflight_digest,scope="synthesis_rerun",
    target={"run_id":res.run_id,"stage_id":synth.id,"attempt_number":osnap.attempt_number})
prov=FakeRoutedProvider()
try:
    rec=asyncio.run(rerun_synthesis(job,{"openrouter":prov},run_dir=rd,run_id=res.run_id,snapshot=osnap,approval=ap))
    print(f"RESULT(legacy cache cheap, stages expensive): DISPATCHED calls={len(prov.calls)} state={rec.state.value}")
except Exception as e:
    print(f"RESULT: refused {type(e).__name__}: {e}; calls={len(prov.calls)}")
