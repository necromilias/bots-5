"""A4 cost-honesty probes: stale/tampered usage caches."""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.core.campaign import project_run
from bots5.manifest import load_job
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job
from tests.helpers import make_job_tree

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self):
        super().__init__("offline-fake-api-key", base_url="https://openrouter.ai/api/v1")
    async def complete(self, request):
        return CompletionResult(output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

root = TMP/"a4"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
job = load_job(job_path)
res = asyncio.run(run_job(job, {"openrouter": FakeRoutedProvider()}, run_id="a4-run"))
run_dir = res.run_dir
out = {}

# baseline projection
p0 = project_run(run_dir)
out["baseline_selected"] = p0.selected_spend
out["baseline_aggregate"] = p0.aggregate_cost

# tamper the stored selected_spend cache and legacy aggregate to inflate cost
usage = json.loads((run_dir/"usage.json").read_text())
print("usage keys:", sorted(usage.keys()))
evil = "999.99"
for k in ("selected_spend","aggregate"):
    if isinstance(usage.get(k), dict):
        usage[k]["cost_usd_known_sum"] = evil
(run_dir/"usage.json").write_text(json.dumps(usage))
p1 = project_run(run_dir)
out["after_cache_tamper_selected_spend"] = p1.selected_spend
out["after_cache_tamper_aggregate_shown"] = p1.aggregate_cost
out["after_cache_tamper_warnings"] = list(p1.integrity_warnings)
out["after_cache_tamper_live_cost"] = p1.live_cost

# corrupt per_attempt entry (non-decimal string) -> must be unknown, never invented money
usage = json.loads((run_dir/"usage.json").read_text())
for key, entry in usage.get("per_attempt", {}).items():
    entry["cost_usd"] = "NaN-ish"; entry["cost_known"] = True
(run_dir/"usage.json").write_text(json.dumps(usage))
p2 = project_run(run_dir)
out["corrupt_per_attempt_selected"] = p2.selected_spend
out["corrupt_per_attempt_live"] = p2.live_cost
print(json.dumps(out, indent=2, default=str))
