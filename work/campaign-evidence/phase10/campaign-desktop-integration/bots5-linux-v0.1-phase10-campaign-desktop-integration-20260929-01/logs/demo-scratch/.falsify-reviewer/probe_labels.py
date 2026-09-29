"""Probe: can a stale PreflightSnapshot (built from the ORIGINAL job) be used to
dispatch work described by a MUTATED job.json, when the mutation is invisible to
the digest? Candidate blind spot: input LABELS are not in the digest ("the approval
binds the bytes, not the labels").

Attack: prepare -> rewrite job.json changing only inputs[].label -> reload job.json
-> run_job(mutated job, original snapshot+approval). Expect per contract "changed
job ... invalidates or refuses approval". Observe what actually happens and what
preflight.json records.
"""
import asyncio, json, sys, shutil
from pathlib import Path
sys.path.insert(0, "src"); sys.path.insert(0, ".")
from bots5.core.campaign import CampaignBridge
from bots5.manifest import load_job
from bots5.runner import run_job
from bots5.errors import ApprovalInvalidatedError, Bots5Error
from bots5.storage import load_preflight
from tests.helpers import make_job_tree
from tests.test_phase10_desktop_preflight import FakeRoutedProvider

base = Path(__file__).resolve().parent
root = base / "labels"; shutil.rmtree(root, ignore_errors=True); root.mkdir(parents=True)
job_path, job_dict = make_job_tree(root)
provider = FakeRoutedProvider()
bridge = CampaignBridge(root / ".bots5" / "runs", provider_factory=lambda j: {"openrouter": provider})
bridge.load_job(job_path)
prepared = bridge.prepare_full_run("op")

# Mutate ONLY the input label in job.json (bytes of referenced files unchanged).
d = json.loads(job_path.read_text())
d["inputs"][0]["label"] = "TAMPERED-LABEL"
job_path.write_text(json.dumps(d), encoding="utf-8")
mutated = load_job(job_path)

try:
    res = asyncio.run(run_job(mutated, {"openrouter": provider}, run_id=prepared.run_id,
                              snapshot=prepared.snapshot, approval=prepared.approval))
    print("RESULT:", res.state.value, "| provider calls:", len(provider.calls))
    pf = load_preflight(res.run_dir)
    print("preflight inputs:", pf["inputs"])
    rd = json.loads((res.run_dir / "job.resolved.json").read_text())
    print("persisted resolved label:", rd["inputs"])
    # What did the provider actually receive as the user message?
    print("dispatch user message head:", provider.calls[0].user[:120].replace("\n", "\\n"))
except ApprovalInvalidatedError as e:
    print("REFUSED:", str(e)[:160], "| calls:", len(provider.calls))
except Bots5Error as e:
    print("BOTS5 ERROR:", type(e).__name__, str(e)[:160])
