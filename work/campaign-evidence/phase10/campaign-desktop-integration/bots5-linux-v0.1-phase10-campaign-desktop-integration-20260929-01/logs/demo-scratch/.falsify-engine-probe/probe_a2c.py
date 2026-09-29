"""A2: JSON-canonicalization drift between approved snapshot and dispatched bytes."""
import asyncio, hashlib, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10"); TMP.mkdir(exist_ok=True); os.chdir(TMP)
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

out = {}
root = TMP / "p9"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
# rewrite worker timeout as an integer literal (no dot) — canonicalizes to int
data = json.loads(job_path.read_text())
data["workers"][0]["timeout_seconds"] = 1          # int, not float
data["synthesis"]["temperature"] = 0               # int zero, not 0.0
job_path.write_text(json.dumps(data))
job = load_job(job_path)
print("loaded types:", type(job.workers[0].timeout_seconds), type(job.synthesis.temperature))
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p9", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p9-run","stage_id":None,"attempt_number":None})
prov = FakeRoutedProvider()
res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p9-run", snapshot=snap, approval=ap))
out["P9_int_float_drift"] = f"ran state={res.state.value} calls={len(prov.calls)}"

# P10: symlinked contract path identity — snapshot stores unresolved path string
root = TMP / "p10"
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
job_path, _ = make_job_tree(root)
prompts = root/"prompts"; real = prompts/"w1.md"
real_bytes = real.read_bytes()
link = prompts/"w1-link.md"
os.symlink(real, link)
data = json.loads(job_path.read_text())
data["workers"][0]["system_prompt_path"] = "./prompts/w1-link.md"
job_path.write_text(json.dumps(data))
job = load_job(job_path)
snap = build_preflight_snapshot(job)
ap = ApprovalRecord(approval_id="ap-p10", approved_at="n", approved_by="op",
                    preflight_digest=snap.preflight_digest, scope="full_run",
                    target={"run_id":"p10-run","stage_id":None,"attempt_number":None})
prov = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov}, run_id="p10-run", snapshot=snap, approval=ap))
    out["P10_symlink_contract"] = f"ran state={res.state.value} (symlink followed; digest over target content)"
except ApprovalInvalidatedError as e:
    out["P10_symlink_contract"] = f"refused: {e}"
# now flip the symlink to a DIFFERENT contract with same size? different digest -> must refuse
alt = prompts/"alt.md"; alt.write_bytes(real_bytes.replace(b"task w1", b"task XX"))
os.unlink(link); os.symlink(alt, link)
prov2 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov2}, run_id="p10b-run", snapshot=snap, approval=ap))
    out["P10b_symlink_swapped_after_approval"] = f"DEFECT ran state={res.state.value}"
except ApprovalInvalidatedError as e:
    out["P10b_symlink_swapped_after_approval"] = f"sound refused: {e}; calls={len(prov2.calls)}"
# P10c: swap symlink AFTER approval to a byte-identical-content file at another path (path escape)
evil_target = TMP/"outside.md"; evil_target.write_bytes(real_bytes)
os.unlink(link); os.symlink(evil_target, link)
prov3 = FakeRoutedProvider()
try:
    res = asyncio.run(run_job(job, {"openrouter": prov3}, run_id="p10c-run", snapshot=snap, approval=ap))
    out["P10c_symlink_escape_same_bytes"] = f"ran state={res.state.value} (content-bound only; path escape accepted)"
except ApprovalInvalidatedError as e:
    out["P10c_symlink_escape_same_bytes"] = f"refused: {e}"

print(json.dumps(out, indent=2))
