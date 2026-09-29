"""A3 cancellation-truth probes."""
import asyncio, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10"); TMP.mkdir(exist_ok=True); os.chdir(TMP)
from bots5.core.campaign import CampaignBridge
from bots5.errors import ApprovalInvalidatedError, StorageError
from bots5.models import ApprovalRecord
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job, build_preflight_snapshot
from bots5.manifest import load_job
from bots5.storage import reconstruct_run_state
from tests.helpers import make_job_tree

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self, delays=None, fail_models=()):
        super().__init__("offline-fake-api-key", base_url="https://openrouter.ai/api/v1")
        self.delays = dict(delays or {}); self.fail = set(fail_models)
        self.calls: list[CompletionRequest] = []
    async def complete(self, request):
        self.calls.append(request)
        d = self.delays.get(request.model, 0)
        if d: await asyncio.sleep(d)
        if request.model in self.fail:
            raise RuntimeError("boom: provider exploded")
        return CompletionResult(output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

def fresh(name, **kw):
    root = TMP / name
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True)
    job_path, _ = make_job_tree(root, **kw)
    return root, load_job(job_path)

out = {}

# C1: mid-run cancel via bridge -> run terminal CANCELLED, stage cause 'cancelled'
root, job = fresh("c1")
provider = FakeRoutedProvider(delays={"model-w1": 5.0, "model-w2": 5.0, "model-synth": 5.0})
bridge = CampaignBridge(root / ".bots5" / "runs", provider_factory=lambda j: {"openrouter": provider})
bridge.load_job(job_path := root / "job.json")
prepared = bridge.prepare_full_run("op")
async def drive_cancel():
    bridge.approve_and_start(prepared)
    await asyncio.sleep(0.4)
    await bridge.cancel()
try:
    asyncio.run(drive_cancel())
    out["C1_cancel_raise"] = "cancel() returned without error"
except StorageError as e:
    out["C1_cancel_raise"] = f"StorageError: {e}"
run_dir = root / ".bots5" / "runs" / prepared.run_id
if run_dir.is_dir():
    run_doc = json.loads((run_dir / "run.json").read_text())
    metas = {}
    for p in sorted((run_dir / "stages").glob("*.json")):
        m = json.loads(p.read_text()); metas[p.name] = (m.get("state"), (m.get("failure") or {}).get("type"))
    out["C1_run_state"] = run_doc.get("state")
    out["C1_stage_causes"] = metas
else:
    out["C1_run_state"] = "NO RUN DIR"

# C2: internal failure during run while outer task cancelled
# worker w1 raises immediately; synth slow; cancel the task after failure recorded?
root, job = fresh("c2")
provider = FakeRoutedProvider(delays={"model-w2": 5.0}, fail_models=("model-w1",))
bridge = CampaignBridge(root / ".bots5" / "runs", provider_factory=lambda j: {"openrouter": provider})
bridge.load_job(root / "job.json")
prepared = bridge.prepare_full_run("op")
async def drive():
    bridge.approve_and_start(prepared)
    await asyncio.sleep(0.3)
    await bridge.cancel()
try:
    asyncio.run(drive())
except Exception as e:
    out["C2_exc"] = f"{type(e).__name__}: {e}"
run_dir = root / ".bots5" / "runs" / prepared.run_id
run_doc = json.loads((run_dir / "run.json").read_text())
metas = {}
for p in sorted((run_dir / "stages").glob("*.json")):
    m = json.loads(p.read_text()); metas[p.name] = (m.get("state"), (m.get("failure") or {}).get("type"))
out["C2_run_state"] = run_doc.get("state")
out["C2_stages"] = metas

# C3: overall timeout must stay TIMED_OUT even when inner stages saw CancelledError
root, job = fresh("c3", run_timeout=0.5)
provider = FakeRoutedProvider(delays={"model-w1": 3.0, "model-w2": 3.0, "model-synth": 3.0})
res = asyncio.run(run_job(job, {"openrouter": provider}, run_id="c3-run",
                          snapshot=build_preflight_snapshot(job),
                          approval=ApprovalRecord(approval_id="ap-c3", approved_at="n", approved_by="op",
                              preflight_digest=build_preflight_snapshot(job).preflight_digest,
                              scope="full_run", target={"run_id":"c3-run","stage_id":None,"attempt_number":None})))
run_dir = res.run_dir
metas = {}
for p in sorted((run_dir / "stages").glob("*.json")):
    m = json.loads(p.read_text()); metas[p.name] = (m.get("state"), (m.get("failure") or {}).get("type"))
out["C3_run_state"] = res.state.value
out["C3_stages"] = metas

# C4: durable cancelled_pending read back — success/auto-retry?
root, job = fresh("c4")
provider = FakeRoutedProvider()
res = asyncio.run(run_job(job, {"openrouter": provider}, run_id="c4-run"))
cands = sorted((res.run_dir / "stages").glob("w1*.json"))
meta = Path(cands[0])
m = json.loads(meta.read_text())
m["state"] = "failed"
if not isinstance(m.get("failure"), dict): m["failure"] = {}
m["failure"]["type"] = "cancelled_pending"
m["started_at"] = m.get("started_at") or "2026-01-01T00:00:00+00:00"
meta.write_text(json.dumps(m))
try:
    st = reconstruct_run_state(res.run_dir)
    out["C4_reconstruct"] = json.dumps(st["stages"].get("w1"))
except Exception as e:
    out["C4_reconstruct"] = f"{type(e).__name__}: {e}"
from bots5.core.campaign import project_run
proj = project_run(res.run_dir)
w1 = [s for s in proj.stages if s.stage_id == "w1"][0]
out["C4_projection_w1"] = {"state": w1.state, "error_type": w1.error_type,
                           "unknown": w1.provider_side_outcome_unknown, "display": proj.display_state}

print(json.dumps(out, indent=2, default=str))
