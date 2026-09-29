"""A2 part 4: symlink swap after approval, correct run_id binding."""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.errors import ApprovalInvalidatedError
from bots5.manifest import load_job
from bots5.models import ApprovalRecord
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job, build_preflight_snapshot

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self):
        super().__init__("offline-fake-api-key", base_url="https://openrouter.ai/api/v1")
        self.calls: list[CompletionRequest] = []
    async def complete(self, request):
        self.calls.append(request)
        return CompletionResult(output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

out = {}
root = TMP/"p11"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
prompts=root/"prompts"; prompts.mkdir()
real=prompts/"w1.md"
shutil.copy(TMP/"p10"/"prompts"/"w1.md", real)
good = real.read_text()
link = prompts/"w1-link.md"
os.symlink(real, link)
job_dict = json.loads((TMP/"p10"/"job.json").read_text())
job_path = root/"job.json"
job_path.write_text(json.dumps(job_dict))
# rebuild input/prompts for p11
inp = root/"input"; inp.mkdir(); (inp/"source.txt").write_text("hello\n")
for wid in ("w2","synth"):
    src = TMP/"p10"/"prompts"/f"{wid}.md"
    if src.exists(): shutil.copy(src, prompts/f"{wid}.md")
data = json.loads(job_path.read_text())
data["inputs"][0]["path"] = "./input/source.txt"
data["workers"][0]["system_prompt_path"] = "./prompts/w1-link.md"
data["workers"][1]["system_prompt_path"] = "./prompts/w2.md"
data["synthesis"]["system_prompt_path"] = "./prompts/synth.md"
data["output"]["runs_dir"] = str(root/".bots5/runs")
job_path.write_text(json.dumps(data))
job = load_job(job_path)
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p11", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p11-run","stage_id":None,"attempt_number":None})
prov = FakeRoutedProvider()
res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p11-run", snapshot=snap, approval=ap))
out["baseline_ok"] = f"state={res.state.value} calls={len(prov.calls)}"
# attacker flips the symlink to an evil contract with DIFFERENT content
evil = root/"evil-w1.md"
evil.write_text(good.replace("worker task w1", "ATTACK CONTRACT"))
os.unlink(link); os.symlink(evil, link)
prov2 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov2}, run_id="p11b-run", snapshot=snap, approval=ap))
    out["symlink_swap_diff_content"] = f"DEFECT ran state={res.state.value}"
except ApprovalInvalidatedError as e:
    out["symlink_swap_diff_content"] = f"sound refused: {e}; calls={len(prov2.calls)}"
# attacker points the symlink at a byte-IDENTICAL file OUTSIDE the job tree
outside = TMP/"outside-contract.md"; outside.write_text(good)
os.unlink(link); os.symlink(outside, link)
prov3 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov3}, run_id="p11c-run", snapshot=snap, approval=ap))
    out["symlink_escape_identical_bytes"] = f"ran state={res.state.value} (binding is content-only; path escape accepted)"
except ApprovalInvalidatedError as e:
    out["symlink_escape_identical_bytes"] = f"refused: {e}"
print(json.dumps(out, indent=2))
