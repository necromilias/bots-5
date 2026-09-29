"""A2: verify the dispatched system message is exactly the approved bytes."""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.manifest import load_job
from bots5.models import ApprovalRecord
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job, build_preflight_snapshot, compile_worker_system_message
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

root = TMP/"p14"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
prompts = root/"prompts"; real = prompts/"w1.md"
good = real.read_text()
link = prompts/"w1-link.md"
os.symlink(real, link)
data = json.loads(job_path.read_text())
data["workers"][0]["system_prompt_path"] = "./prompts/w1-link.md"
job_path.write_text(json.dumps(data))
job = load_job(job_path)
snap = build_preflight_snapshot(job)
approved_sys = snap.system_messages["w1"]
ap = ApprovalRecord(approval_id="ap-p14", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p14","stage_id":None,"attempt_number":None})
evil = root/"evil-w1.md"; evil.write_text(good.replace("Perform worker task w1.", "ATTACK CONTRACT TASK."))
os.unlink(link); os.symlink(evil, link)
prov = FakeRoutedProvider()
res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p14", snapshot=snap, approval=ap))
w1_call = [c for c in prov.calls if c.model=="model-w1"][0]
print(json.dumps({
  "state": res.state.value,
  "dispatched_system_equals_approved_snapshot_system": w1_call.system == approved_sys,
  "dispatched_contains_attack_marker": "ATTACK CONTRACT TASK" in w1_call.system,
  "approved_contains_attack_marker": "ATTACK CONTRACT TASK" in approved_sys,
  "task_line_in_dispatched": [l for l in w1_call.system.splitlines() if "task" in l.lower()][:2],
}, indent=2))
