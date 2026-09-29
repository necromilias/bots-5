"""A5 decisive: v1 run, dependency output swapped to attacker bytes matching the recorded digest."""
import hashlib, json, sys, os, shutil
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.storage import reconstruct_run_state

rd = TMP/"a5-t5"; shutil.rmtree(rd, ignore_errors=True); rd.mkdir(parents=True)
job_resolved = {
  "schema_version":1,"name":"t","inputs":[],
  "execution":{"max_parallelism":1,"run_timeout_seconds":5.0,"stop_before_synthesis_if_known_cost_exceeds_usd":None},
  "workers":[{"id":"w1","provider":"openrouter","model":"m","system_prompt_path":"p","temperature":0.1,"max_output_tokens":10,"timeout_seconds":1.0}],
  "synthesis":{"id":"synth","provider":"openrouter","model":"ms","system_prompt_path":"p","temperature":0.1,"max_output_tokens":10,"timeout_seconds":1.0,"depends_on":["w1"]},
  "output":{"runs_dir":str(rd.parent)},
}
(rd/"job.resolved.json").write_text(json.dumps(job_resolved))
# NO evidence_version marker -> v1
(rd/"run.json").write_text(json.dumps({"run_id":rd.name,"state":"succeeded","started_at":"x","ended_at":"y",
    "stage_order":["w1","synth"],"stages":{},"usage":{}}))
(rd/"usage.json").write_text("{}"); (rd/"events.jsonl").write_text(""); (rd/"stages").mkdir()
evil = b"ATTACKER CONTROLLED TEXT"
(rd/"stages"/"w1.json").write_text(json.dumps({"stage_id":"w1","provider":"openrouter","requested_model":"m","state":"succeeded","output_path":"stages/w1.md"}))
(rd/"stages"/"w1.md").write_bytes(evil)
(rd/"stages"/"synth.json").write_text(json.dumps({
    "stage_id":"synth","provider":"openrouter","requested_model":"ms","state":"succeeded","output_path":"stages/synth.md",
    "consumed_dependencies":{"w1":1},"dependency_digests":{"w1":hashlib.sha256(evil).hexdigest()}}))
st = reconstruct_run_state(rd)
fr = st["stages"]["synth"].get("synthesis_freshness",{})
print(json.dumps({"classification": fr.get("classification"),
                  "deps": fr.get("dependencies"),
                  "integrity_warning": fr.get("integrity_warning")}, indent=2))
