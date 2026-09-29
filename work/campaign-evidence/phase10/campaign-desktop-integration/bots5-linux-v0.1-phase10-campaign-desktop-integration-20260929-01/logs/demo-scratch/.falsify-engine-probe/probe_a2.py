"""A2 adversarial probes — falsification engine (scratch only, outside src/tests/evidence)."""
import asyncio, hashlib, json, sys, os
from decimal import Decimal
from pathlib import Path

REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src")
sys.path.insert(0, REPO)

TMP = Path("/tmp/falsify_phase10"); TMP.mkdir(exist_ok=True)
os.chdir(TMP)

from bots5.core.campaign import CampaignBridge
from bots5.errors import ApprovalInvalidatedError
from bots5.models import ApprovalRecord
from bots5.providers.base import CompletionRequest
from bots5.providers.openrouter import OpenRouterProvider
from bots5.runner import run_job, build_preflight_snapshot
from bots5.manifest import load_job
from tests.helpers import make_job_tree

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

class FakeRoutedProvider(OpenRouterProvider):
    def __init__(self, results=None, delays=None, base_url=OPENROUTER_BASE_URL):
        super().__init__("offline-fake-api-key", base_url=base_url)
        self.results = dict(results or {}); self.delays = dict(delays or {})
        self.calls: list[CompletionRequest] = []
    async def complete(self, request):
        self.calls.append(request)
        delay = self.delays.get(request.model, 0)
        if delay: await asyncio.sleep(delay)
        from bots5.providers.base import CompletionResult
        return CompletionResult(
            output_text=self.results.get(request.model, f"output:{request.model}"),
            requested_model=request.model, finish_reason="stop", returned_model=request.model,
            request_id=f"req-{request.model}", prompt_tokens=10, completion_tokens=20,
            total_tokens=30, known_cost_usd=Decimal("0.01"), duration_seconds=0.001)

def tree(name):
    root = TMP / name
    import shutil
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True)
    job_path, _raw = make_job_tree(root)
    job = load_job(job_path)
    return root, job_path, job

results = {}

# ---- P1: replay of a consumed approval via direct run_job -------------------
try:
    root, job_path, job = tree("p1")
    provider = FakeRoutedProvider()
    bridge = CampaignBridge(root / ".bots5" / "runs", provider_factory=lambda j: {"openrouter": provider})
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("op")
    async def drive():
        bridge.approve_and_start(prepared)
        return await bridge.run_to_completion()
    r1 = asyncio.run(drive())
    calls_after_first = len(provider.calls)
    try:
        r2 = asyncio.run(run_job(job, {"openrouter": provider}, run_id=prepared.run_id,
                                snapshot=prepared.snapshot, approval=prepared.approval))
        results["P1_replay"] = f"DEFECT: replay succeeded state={r2.state}"
    except ApprovalInvalidatedError as e:
        results["P1_replay"] = f"sound: refused ({e}); provider calls {calls_after_first}->{len(provider.calls)}"
    except Exception as e:
        results["P1_replay"] = f"refused untyped {type(e).__name__}: {e}; calls {calls_after_first}->{len(provider.calls)}"
except Exception as e:
    results["P1_replay"] = f"probe error {type(e).__name__}: {e}"

# ---- P2: tampered input bytes after approval, honest digest recompute -------
try:
    root, job_path, job = tree("p2")
    inp = next(p for p in root.rglob("source.txt"))
    original = inp.read_bytes()
    provider = FakeRoutedProvider()
    inp.write_bytes(b"EVIL\n")
    evil_snap = build_preflight_snapshot(job)   # attacker honestly recomputes digest over evil bytes
    ap = ApprovalRecord(approval_id="ap-p2", approved_at="now", approved_by="attacker",
                        preflight_digest=evil_snap.preflight_digest, scope="full_run",
                        target={"run_id": "p2-run", "stage_id": None, "attempt_number": None})
    try:
        res = asyncio.run(run_job(job, {"openrouter": provider}, run_id="p2-run",
                                  snapshot=evil_snap, approval=ap))
        dispatched_user = provider.calls[0].user if provider.calls else ""
        results["P2_tampered"] = (f"DISPATCHED state={res.state.value}; provider_calls={len(provider.calls)}; "
                                  f"user_msg_contains_EVIL={'EVIL' in dispatched_user}")
    except ApprovalInvalidatedError as e:
        results["P2_tampered"] = f"sound: refused ({e}); provider_calls={len(provider.calls)}"
    finally:
        inp.write_bytes(original)
except Exception as e:
    results["P2_tampered"] = f"probe error {type(e).__name__}: {e}"

# ---- P3: wrong-target approval ----------------------------------------------
try:
    root, job_path, job = tree("p3")
    provider = FakeRoutedProvider()
    snap = build_preflight_snapshot(job)
    ap = ApprovalRecord(approval_id="ap-p3", approved_at="now", approved_by="op",
                        preflight_digest=snap.preflight_digest, scope="full_run",
                        target={"run_id": "OTHER-RUN", "stage_id": None, "attempt_number": None})
    try:
        res = asyncio.run(run_job(job, {"openrouter": provider}, run_id="p3-run", snapshot=snap, approval=ap))
        results["P3_wrongtarget"] = f"DEFECT: ran with wrong target state={res.state.value}"
    except ApprovalInvalidatedError as e:
        results["P3_wrongtarget"] = f"sound: refused ({e}); provider_calls={len(provider.calls)}"
    # omitted run_id resolves from the approval target itself
    try:
        res = asyncio.run(run_job(job, {"openrouter": provider}, snapshot=snap, approval=ap))
        results["P3_omit_runid"] = f"ran -> run_dir={res.run_dir} state={res.state.value} calls={len(provider.calls)}"
    except Exception as e:
        results["P3_omit_runid"] = f"{type(e).__name__}: {e}"
except Exception as e:
    results["P3_wrongtarget"] = f"probe error {type(e).__name__}: {e}"

print(json.dumps(results, indent=2))
