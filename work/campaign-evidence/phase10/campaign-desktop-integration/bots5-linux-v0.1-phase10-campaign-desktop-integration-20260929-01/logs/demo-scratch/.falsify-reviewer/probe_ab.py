"""Probe A: does a mutated job.json (only inputs[].label changed) still dispatch under the
pre-mutation approval?  -> already shown YES. Now check whether the same trick works for
the operation paths (regeneration / synthesis rerun).

Probe B: can an approval be consumed and then the run tree creation fails, leaving the
durable one-shot marker spent with NO run directory (approval burned + nothing ran)?
"""
import asyncio, json, sys, shutil
from pathlib import Path
sys.path.insert(0, "src"); sys.path.insert(0, ".")
from bots5.core.campaign import CampaignBridge
from bots5.manifest import load_job
from bots5.runner import run_job, regenerate_worker
from bots5.errors import ApprovalInvalidatedError, Bots5Error, ValidationError
from bots5.storage import approval_consumed
from tests.helpers import make_job_tree
from tests.test_phase10_desktop_preflight import FakeRoutedProvider

base = Path(__file__).resolve().parent


def setup(name):
    root = base / name
    shutil.rmtree(root, ignore_errors=True); root.mkdir(parents=True)
    job_path, _ = make_job_tree(root)
    provider = FakeRoutedProvider(results={"model-w1": "A", "model-w2": "B", "model-synth": "S"})
    bridge = CampaignBridge(root / ".bots5" / "runs", provider_factory=lambda j: {"openrouter": provider})
    bridge.load_job(job_path)
    prepared = bridge.prepare_full_run("op")
    asyncio.run(run_job(prepared.job, {"openrouter": provider}, run_id=prepared.run_id,
                        snapshot=prepared.snapshot, approval=prepared.approval))
    bridge.adopt_run(prepared.run_dir)
    return root, job_path, bridge, provider, prepared


# --- Probe A: label tamper on regeneration ------------------------------------
root, job_path, bridge, provider, prepared = setup("regen_label")
prepped_regen = bridge.prepare_regeneration("w1", "model-w1-alt", "op")
d = json.loads(job_path.read_text())
d["inputs"][0]["label"] = "TAMPERED"
job_path.write_text(json.dumps(d), encoding="utf-8")
mutated_job = load_job(job_path)
calls_before = len(provider.calls)
try:
    rec = asyncio.run(regenerate_worker(
        mutated_job, {"openrouter": provider}, run_dir=prepped_regen.run_dir,
        run_id=prepped_regen.run_id, stage_id=prepped_regen.stage_id,
        model=prepped_regen.model, snapshot=prepped_regen.snapshot,
        approval=prepped_regen.approval))
    print("PROBE A: REGEN DISPATCHED state=", rec.state.value, "calls+", len(provider.calls) - calls_before)
    print("   user message sent:", provider.calls[-1].user[:80].replace("\n", "\\n"))
except (ApprovalInvalidatedError, ValidationError, Bots5Error) as e:
    print("PROBE A refused:", type(e).__name__, str(e)[:120])

# --- Probe B: approval burned but run tree creation fails ---------------------
root2 = base / "burn"; shutil.rmtree(root2, ignore_errors=True); root2.mkdir(parents=True)
jp2, _ = make_job_tree(root2)
prov2 = FakeRoutedProvider()
b2 = CampaignBridge(root2 / ".bots5" / "runs", provider_factory=lambda j: {"openrouter": prov2})
b2.load_job(jp2)
prep2 = b2.prepare_full_run("op")
# Make create_run_tree fail AFTER the marker is written: pre-create the run dir read-only
rd = prep2.run_dir
rd.mkdir(parents=True)          # run dir exists -> os.mkdir in create_run_tree raises FileExistsError
rd2 = rd / "stages"             # not needed; mkdir of run_dir itself fails
try:
    asyncio.run(run_job(prep2.job, {"openrouter": prov2}, run_id=prep2.run_id,
                        snapshot=prep2.snapshot, approval=prep2.approval))
    print("PROBE B: unexpected success")
except Bots5Error as e:
    print("PROBE B error:", type(e).__name__, str(e)[:120])
print("PROBE B: marker present?", approval_consumed(rd, prep2.approval.approval_id),
      "| provider calls:", len(prov2.calls))
