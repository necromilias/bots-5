"""A4: v2 run (bridge) with tampered selected_spend cache + legacy aggregate."""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
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

root = TMP/"a4v2"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
provider = FakeRoutedProvider()
bridge = CampaignBridge(root/".bots5"/"runs", provider_factory=lambda j: {"openrouter": provider})
bridge.load_job(job_path)
prepared = bridge.prepare_full_run("op")
async def drive():
    bridge.approve_and_start(prepared); return await bridge.run_to_completion()
res = asyncio.run(drive())
run_dir = res.run_dir
usage = json.loads((run_dir/"usage.json").read_text())
print("v2 usage keys:", sorted(usage.keys()))
out={}
out["clean_selected"] = usage["selected_spend"]["cost_usd_known_sum"]
out["clean_aggregate"] = usage["aggregate"]["cost_usd_known_sum"]
# attacker inflates BOTH caches; per_attempt untouched
for k in ("selected_spend","aggregate"):
    usage[k]["cost_usd_known_sum"] = "999.99"; usage[k]["cost_status"]="known"; usage[k]["cost_complete"]=True
(run_dir/"usage.json").write_text(json.dumps(usage))
p = project_run(run_dir)
out["projected_selected_spend"] = p.selected_spend.get("cost_usd_known_sum")
out["projected_aggregate_display"] = p.aggregate_cost.get("cost_usd_known_sum")
out["warnings"] = list(p.integrity_warnings)
out["live_cost_subtotal"] = p.live_cost["known_subtotal_usd"]
# what does the dock render?
src = open(REPO+"/src/bots5/desktop/campaign_dock.py").read()
import re
agg_uses = [l.strip() for l in src.splitlines() if "aggregate" in l or "selected_spend" in l]
out["dock_lines_referencing_costs"] = agg_uses[:8]
print(json.dumps(out, indent=2, default=str))
