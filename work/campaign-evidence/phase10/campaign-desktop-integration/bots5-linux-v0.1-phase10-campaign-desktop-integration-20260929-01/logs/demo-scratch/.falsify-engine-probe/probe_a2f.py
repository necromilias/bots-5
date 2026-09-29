"""A2 part 6: TOCTOU on contract path identity (symlink flip between preflight and dispatch)."""
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

root = TMP/"p13"
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
out = {}

# Attack 1: honest approval over GOOD bytes; attacker flips symlink to EVIL content
# BEFORE run_job's re-verification read. Expect refusal (digest mismatch).
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p13a", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p13a","stage_id":None,"attempt_number":None})
evil = root/"evil-w1.md"; evil.write_text(good.replace("worker task w1", "ATTACK CONTRACT"))
os.unlink(link); os.symlink(evil, link)
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p13a", snapshot=snap, approval=ap))
    out["flip_to_evil_before_dispatch"] = f"DEFECT ran state={res.state.value}; system_msg_sent={[c.system[:60] for c in prov.calls][:1]}"
except ApprovalInvalidatedError as e:
    out["flip_to_evil_before_dispatch"] = f"sound refused: {e}; calls={len(prov.calls)}"
finally:
    os.unlink(link); os.symlink(real, link)

# Attack 2: attacker points the symlink at BYTE-IDENTICAL content OUTSIDE the job tree.
outside = root/"outside-contract.md"; outside.write_text(good)
os.unlink(link); os.symlink(outside, link)
snap2 = build_preflight_snapshot(job)
ap2 = ApprovalRecord(approval_id="ap-p13b", approved_at="n", approved_by="op",
                     preflight_digest=snap2.preflight_digest, scope="full_run",
                     target={"run_id":"p13b","stage_id":None,"attempt_number":None})
prov2 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov2}, run_id="p13b", snapshot=snap2, approval=ap2))
    out["escape_identical_bytes"] = f"ran state={res.state.value} (binding is content-only; path outside job tree accepted)"
except ApprovalInvalidatedError as e:
    out["escape_identical_bytes"] = f"refused: {e}"
finally:
    os.unlink(link); os.symlink(real, link)

# Attack 3: replay the SAME approval object against a second run (same approval id, new run id).
snap3 = build_preflight_snapshot(job)
ap3 = ApprovalRecord(approval_id="ap-p13c", approved_at="n", approved_by="op",
                     preflight_digest=snap3.preflight_digest, scope="full_run",
                     target={"run_id":"p13c","stage_id":None,"attempt_number":None})
prov3 = FakeRoutedProvider()
r1 = asyncio.run(run_job(job, {"openrouter": prov3}, run_id="p13c", snapshot=snap3, approval=ap3))
calls1 = len(prov3.calls)
try:
    r2 = asyncio.run(run_job(job, {"openrouter": prov3}, run_id="p13d", snapshot=snap3, approval=ap3))
    out["replay_new_runid"] = f"DEFECT: second run {r2.run_dir} state={r2.state.value}"
except ApprovalInvalidatedError as e:
    out["replay_new_runid"] = f"sound refused: {e}; extra_calls={len(prov3.calls)-calls1}"
except Exception as e:
    out["replay_new_runid"] = f"{type(e).__name__}: {e}; extra_calls={len(prov3.calls)-calls1}"

print(json.dumps(out, indent=2))
