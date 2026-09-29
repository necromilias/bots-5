"""A3: durable cancelled_pending under a run.json that says succeeded."""
import asyncio, hashlib, json, sys, os, shutil
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10"); TMP.mkdir(exist_ok=True); os.chdir(TMP)
from bots5.core.campaign import project_run

root = TMP / "c5"
if root.exists(): shutil.rmtree(root)
run_dir = root / ".bots5" / "runs" / "kill-window"; run_dir.mkdir(parents=True)
(run_dir.parent).mkdir(exist_ok=True)
inp = root/"input"; pr = root/"prompts"; inp.mkdir(); pr.mkdir()
(inp/"source.txt").write_text("hello\n")
(pr/"w1.md").write_text(open(REPO+"/tests/fixtures/sample_contract.md").read() if (REPO and Path(REPO,"tests/fixtures/sample_contract.md").exists()) else "")
# use the real contract format from tests.helpers
sys.path.insert(0, REPO)
from tests.helpers import worker_contract
pr.joinpath("w1.md").write_text(worker_contract("Perform worker task w1."))
job_resolved = {
  "schema_version":1,"name":"test-job",
  "inputs":[{"label":"source","path":str(inp/"source.txt")}],
  "execution":{"max_parallelism":1,"run_timeout_seconds":5.0,"stop_before_synthesis_if_known_cost_exceeds_usd":2.0},
  "workers":[{"id":"w1","provider":"openrouter","model":"model-w1","system_prompt_path":str(pr/"w1.md"),
              "temperature":0.1,"max_output_tokens":100,"timeout_seconds":1.0}],
  "synthesis":None,
  "output":{"runs_dir":str(run_dir.parent)},
}
(run_dir/"job.resolved.json").write_text(json.dumps(job_resolved))
(run_dir/"run.json").write_text(json.dumps({
  "run_id":"kill-window","state":"succeeded","started_at":"2026-01-01T00:00:00Z",
  "ended_at":"2026-01-01T00:00:05Z","stage_order":["w1"],"stages":{},"usage":{},
  "evidence_version":2}))
(run_dir/"usage.json").write_text("{}")
(run_dir/"events.jsonl").write_text("")
stages=run_dir/"stages"; stages.mkdir()
(stages/"w1.att1.json").write_text(json.dumps({
  "stage_id":"w1","provider":"openrouter","requested_model":"model-w1","state":"failed",
  "failure":{"type":"cancelled_pending","message":"stage cancelled before terminal classification",
             "provider_side_outcome_unknown":True},
  "started_at":"2026-01-01T00:00:01Z","ended_at":"2026-01-01T00:00:02Z",
  "usage":{"total_tokens":10},"attempt_number":1}))
proj = project_run(run_dir, hosted=False)
w1 = next(s for s in proj.stages if s.stage_id=="w1")
print(json.dumps({
  "run_state":proj.run_state,"display_state":proj.display_state,
  "w1_state":w1.state,"w1_error_type":w1.error_type,"w1_unknown":w1.provider_side_outcome_unknown,
  "integrity_warnings":list(proj.integrity_warnings),
}, indent=2))
