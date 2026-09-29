"""A4: CLI status aggregate_cost line from a tampered usage.json on a v2 run."""
import asyncio, json, sys, os, shutil, subprocess
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.core.campaign import CampaignBridge
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

root = TMP/"a4cli"; shutil.rmtree(root, ignore_errors=True); root.mkdir(parents=True)
job_path,_ = make_job_tree(root)
bridge = CampaignBridge(root/".bots5"/"runs", provider_factory=lambda j: {"openrouter": FakeRoutedProvider()})
bridge.load_job(job_path)
prepared = bridge.prepare_full_run("op")
async def drive():
    bridge.approve_and_start(prepared); return await bridge.run_to_completion()
res = asyncio.run(drive()); rd=res.run_dir
u=json.loads((rd/"usage.json").read_text())
u["aggregate"]["cost_usd_known_sum"]="999.99"
(rd/"usage.json").write_text(json.dumps(u))
env=dict(os.environ); env["PYTHONPATH"]=REPO+"/src"
p=subprocess.run([sys.executable,"-m","bots5.cli","status",res.run_id,"--runs-dir",str(root/".bots5/runs")],
                 capture_output=True,text=True,cwd=str(root),env=env)
print("exit:",p.returncode)
print("\n".join(l for l in p.stdout.splitlines() if "aggregate" in l or "stale" in l.lower() or "selected" in l))
