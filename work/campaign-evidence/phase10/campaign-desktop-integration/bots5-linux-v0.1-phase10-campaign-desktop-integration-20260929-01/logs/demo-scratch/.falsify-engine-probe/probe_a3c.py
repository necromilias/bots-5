"""A3: run.json 'cancelled' but stage records still carry cancelled_pending (partial terminalization)."""
import json, sys, os, shutil, hashlib
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.core.campaign import project_run
rd = TMP/"a3-cancel-partial"; shutil.rmtree(rd, ignore_errors=True); rd.mkdir(parents=True)
job_resolved={"schema_version":1,"name":"t","inputs":[],
 "execution":{"max_parallelism":1,"run_timeout_seconds":5.0,"stop_before_synthesis_if_known_cost_exceeds_usd":None},
 "workers":[{"id":"w1","provider":"openrouter","model":"m","system_prompt_path":"p","temperature":0.1,"max_output_tokens":10,"timeout_seconds":1.0}],
 "synthesis":None,"output":{"runs_dir":str(rd.parent)}}
(rd/"job.resolved.json").write_text(json.dumps(job_resolved))
(rd/"run.json").write_text(json.dumps({"run_id":rd.name,"state":"cancelled","started_at":"x","ended_at":"y",
  "stage_order":["w1"],"stages":{},"usage":{},"evidence_version":2}))
(rd/"usage.json").write_text("{}"); (rd/"events.jsonl").write_text(""); (rd/"stages").mkdir()
(rd/"selection.json").write_text(json.dumps({"schema_version":1,"run_id":rd.name,"updated_at":"x","selected_attempts":{}}))
# hard kill INSIDE the CancelledError handler: stage record durably cancelled_pending,
# but run.json was already written as cancelled by a previous partial pass? Simulate:
(rd/"stages"/"w1.att1.json").write_text(json.dumps({
  "stage_id":"w1","provider":"openrouter","requested_model":"m","state":"failed","attempt_number":1,
  "failure":{"type":"cancelled_pending","message":"stage cancelled before terminal classification",
             "provider_side_outcome_unknown":True},
  "started_at":"2026-01-01T00:00:01Z","ended_at":"2026-01-01T00:00:02Z"}))
p=project_run(rd, hosted=False)
w1=p.stages[0]
print(json.dumps({"display_state":p.display_state,"run_state":p.run_state,
  "w1_error_type":w1.error_type,"w1_unknown":w1.provider_side_outcome_unknown,
  "warnings":list(p.integrity_warnings)},indent=2))
