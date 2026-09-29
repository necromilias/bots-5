"""A2 part 5: symlink swap after approval (self-contained tree)."""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10"); TMP.mkdir(exist_ok=True)
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

root = TMP/"p12"
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
ap = ApprovalRecord(approval_id="ap-p12", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p12-run","stage_id":None,"attempt_number":None})
prov = FakeRoutedProvider()
res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p12-run", snapshot=snap, approval=ap))
out = {"baseline_ok": f"state={res.state.value} calls={len(prov.calls)}"}
# attacker flips the symlink AFTER approval to a different contract
evil = root/"evil-w1.md"; evil.write_text(good.replace("worker task w1", "ATTACK CONTRACT"))
os.unlink(link); os.symlink(evil, link)
prov2 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov2}, run_id="p12-run-b", snapshot=snap, approval=ap))
    out["symlink_swap_diff_content"] = f"DEFECT ran state={res.state.value}"
except ApprovalInvalidatedError as e:
    out["symlink_swap_diff_content"] = f"sound refused: {e}; calls={len(prov2.calls)}"
# attacker points the symlink at byte-identical content OUTSIDE the job tree
outside = TMP/"outside-contract.md"; outside.write_text(good)
os.unlink(link); os.symlink(outside, link)
prov3 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov3}, run_id="p12-run-c", snapshot=snap, approval=ap))
    out["symlink_escape_identical_bytes"] = f"ran state={res.state.value} (content-bound only)"
except ApprovalInvalidatedError as e:
    out["symlink_escape_identical_bytes"] = f"refused: {e}"
print(json.dumps(out, indent=2))
