"""A5 freshness-order probes: v1 FRESH/STALE leak; v2 failed-synthesis edge."""
import hashlib, json, sys, os, shutil
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.core.campaign import project_run
from bots5.storage import reconstruct_run_state

def base(run_dir, run_json_extra=None, version_marker=True):
    run_dir.mkdir(parents=True)
    job_resolved = {
      "schema_version":1,"name":"t",
      "inputs":[],"execution":{"max_parallelism":1,"run_timeout_seconds":5.0,
        "stop_before_synthesis_if_known_cost_exceeds_usd":None},
      "workers":[{"id":"w1","provider":"openrouter","model":"m1","system_prompt_path":"p","temperature":0.1,"max_output_tokens":10,"timeout_seconds":1.0}],
      "synthesis":{"id":"synth","provider":"openrouter","model":"ms","system_prompt_path":"p","temperature":0.1,"max_output_tokens":10,"timeout_seconds":1.0,"depends_on":["w1"]},
      "output":{"runs_dir":str(run_dir.parent)},
    }
    (run_dir/"job.resolved.json").write_text(json.dumps(job_resolved))
    run={"run_id":run_dir.name,"state":"succeeded","started_at":"x","ended_at":"y",
         "stage_order":["w1","synth"],"stages":{},"usage":{}}
    if version_marker: run["evidence_version"]=2
    if run_json_extra: run.update(run_json_extra)
    (run_dir/"run.json").write_text(json.dumps(run))
    (run_dir/"usage.json").write_text("{}")
    (run_dir/"events.jsonl").write_text("")
    (run_dir/"stages").mkdir()

out={}
# T1: v1 run whose synth record CARRIES provenance matching current outputs -> must not be FRESH
rd = TMP/"a5-t1"; shutil.rmtree(rd, ignore_errors=True)
base(rd, version_marker=False)
digest = hashlib.sha256(b"w1 output").hexdigest()
(rd/"stages"/"w1.json").write_text(json.dumps({"stage_id":"w1","provider":"openrouter","requested_model":"m","state":"succeeded","output_path":"stages/w1.md"}))
(rd/"stages"/"w1.md").write_text("w1 output")
(rd/"stages"/"synth.json").write_text(json.dumps({
    "stage_id":"synth","provider":"openrouter","requested_model":"ms","state":"succeeded","output_path":"stages/synth.md",
    "consumed_dependencies":{"w1":1},"dependency_digests":{"w1":digest}}))
try:
    st=reconstruct_run_state(rd); fr=st["stages"]["synth"].get("synthesis_freshness",{})
    out["T1_v1_with_provenance"]=fr.get("classification")
except Exception as e:
    out["T1_v1_with_provenance"]=f"{type(e).__name__}: {e}"

# T2: v1 run with provenance that would MECHANICALLY be STALE -> must not be STALE
rd = TMP/"a5-t2"; shutil.rmtree(rd, ignore_errors=True)
base(rd, version_marker=False)
(rd/"stages"/"w1.json").write_text(json.dumps({"stage_id":"w1","provider":"openrouter","requested_model":"m","state":"succeeded","output_path":"stages/w1.md"}))
(rd/"stages"/"w1.md").write_text("DIFFERENT BYTES NOW")
(rd/"stages"/"synth.json").write_text(json.dumps({
    "stage_id":"synth","provider":"openrouter","requested_model":"ms","state":"succeeded","output_path":"stages/synth.md",
    "consumed_dependencies":{"w1":1},"dependency_digests":{"w1":"0"*64}}))
try:
    st=reconstruct_run_state(rd); fr=st["stages"]["synth"].get("synthesis_freshness",{})
    out["T2_v1_mechanically_stale"]=fr.get("classification")
except Exception as e:
    out["T2_v1_mechanically_stale"]=f"{type(e).__name__}: {e}"

# T3: v2 synthesis FAILED after dispatch, provenance present & fresh -> classification?
rd = TMP/"a5-t3"; shutil.rmtree(rd, ignore_errors=True)
base(rd)
digest = hashlib.sha256(b"w1 output").hexdigest()
(rd/"selection.json").write_text(json.dumps({"schema_version":1,"run_id":rd.name,"updated_at":"x","selected_attempts":{}}))
(rd/"stages"/"w1.att1.json").write_text(json.dumps({"stage_id":"w1","provider":"openrouter","requested_model":"m","state":"succeeded","attempt_number":1}))
(rd/"stages"/"w1.att1.md").write_text("w1 output")
(rd/"stages"/"synth.att1.json").write_text(json.dumps({
    "stage_id":"synth","provider":"openrouter","requested_model":"ms","state":"failed","attempt_number":1,
    "failure":{"type":"request_timeout","message":"x","provider_side_outcome_unknown":True},
    "consumed_dependencies":{"w1":1},"dependency_digests":{"w1":digest}}))
try:
    st=reconstruct_run_state(rd); fr=st["stages"]["synth"].get("synthesis_freshness",{})
    out["T3_v2_failed_dispatched"]=fr.get("classification")
except Exception as e:
    out["T3_v2_failed_dispatched"]=f"{type(e).__name__}: {e}"

# T4: v2 synthesis RUNNING (crashed), provenance present -> classification?
rd = TMP/"a5-t4"; shutil.rmtree(rd, ignore_errors=True)
base(rd)
(rd/"selection.json").write_text(json.dumps({"schema_version":1,"run_id":rd.name,"updated_at":"x","selected_attempts":{}}))
(rd/"stages"/"w1.att1.json").write_text(json.dumps({"stage_id":"w1","provider":"openrouter","requested_model":"m","state":"succeeded","attempt_number":1}))
(rd/"stages"/"w1.att1.md").write_text("w1 output")
(rd/"stages"/"synth.att1.json").write_text(json.dumps({
    "stage_id":"synth","provider":"openrouter","requested_model":"ms","state":"running","attempt_number":1,
    "started_at":"2026-01-01T00:00:00Z",
    "consumed_dependencies":{"w1":1},"dependency_digests":{"w1":digest}}))
(rd/"run.json").write_text(json.dumps({"run_id":rd.name,"state":"running","started_at":"x","ended_at":None,
    "stage_order":["w1","synth"],"stages":{},"usage":{},"evidence_version":2}))
try:
    st=reconstruct_run_state(rd); fr=st["stages"]["synth"].get("synthesis_freshness",{})
    p=project_run(rd, hosted=False)
    out["T4_v2_running_crashed"]={"classification":fr.get("classification"),"display":p.display_state}
except Exception as e:
    out["T4_v2_running_crashed"]=f"{type(e).__name__}: {e}"

print(json.dumps(out, indent=2))
