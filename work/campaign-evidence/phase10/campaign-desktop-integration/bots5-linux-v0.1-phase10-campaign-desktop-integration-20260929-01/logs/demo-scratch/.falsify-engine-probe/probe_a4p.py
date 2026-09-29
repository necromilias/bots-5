"""A4: persist_usage_v2 with a NaN cost record -> does it write 'NaN'?"""
import sys, json
from decimal import Decimal
from pathlib import Path
REPO="/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0,REPO+"/src")
from bots5.models import StageRecord
from bots5.storage import persist_usage_v2, RunDirs
rd = Path("/tmp/falsify_phase10/a4nanwrite")
import shutil; shutil.rmtree(rd, ignore_errors=True); rd.mkdir(parents=True)
dirs = RunDirs(root=rd, stages=rd/"stages", events=rd/"events.jsonl")
rec = StageRecord(id="w1", provider="openrouter", requested_model="m")
rec.attempt_number = 1
rec.known_cost_usd = Decimal("NaN")   # e.g. provider telemetry corruption
try:
    doc = persist_usage_v2(dirs, [rec], selected_attempts={"w1":1}, stage_ids=["w1"])
    written = json.loads((rd/"usage.json").read_text())
    print("WROTE OK:", written["per_attempt"], "| cumulative:", written["cumulative_spend"]["cost_usd_known_sum"])
except Exception as e:
    print(f"refused: {type(e).__name__}: {e}")
