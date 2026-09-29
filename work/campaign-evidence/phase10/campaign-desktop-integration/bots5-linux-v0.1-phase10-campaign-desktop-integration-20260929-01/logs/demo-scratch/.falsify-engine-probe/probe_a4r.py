"""A4: NaN per_attempt -> projection selected_spend 'NaN' status known."""
import asyncio, json, sys, shutil
from decimal import Decimal
from pathlib import Path
REPO="/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0,REPO+"/src"); sys.path.insert(0,REPO)
TMP=Path("/tmp/falsify_phase10")
from bots5.core.campaign import CampaignBridge, project_run
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from tests.helpers import make_job_tree

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self):
        super().__init__("offline-fake-api-key", base_url="https://openrouter.ai/api/v1")
    async def complete(self, request):
        return CompletionResult(output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

root=TMP/"a4nanproj"; shutil.rmtree(root,ignore_errors=True); root.mkdir(parents=True)
job_path,_=make_job_tree(root)
bridge=CampaignBridge(root/".bots5"/"runs", provider_factory=lambda j: {"openrouter":FakeRoutedProvider()})
bridge.load_job(job_path)
prepared=bridge.prepare_full_run("op")
async def drive():
    bridge.approve_and_start(prepared); return await bridge.run_to_completion()
res=asyncio.run(drive()); rd=res.run_dir
u=json.loads((rd/"usage.json").read_text())
u["per_attempt"]["w1.att1"]["cost_usd"]="NaN"
(rd/"usage.json").write_text(json.dumps(u))
p=project_run(rd)
print(json.dumps({"selected_spend": p.selected_spend, "live": p.live_cost,
                  "warnings": list(p.integrity_warnings)}, indent=2, default=str))
