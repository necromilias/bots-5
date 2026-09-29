"""A4: does the gate accept a NaN per_attempt entry (Decimal('NaN') parse)?"""
import asyncio, json, sys, os, shutil
from decimal import Decimal
from pathlib import Path
REPO = "/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1"
sys.path.insert(0, REPO + "/src"); sys.path.insert(0, REPO)
TMP = Path("/tmp/falsify_phase10")
from bots5.usage import derive_selected_spend, _entry_known_cost_usd
out={}
# direct unit check of the derivation with a NaN string cost
per={"w1.att1":{"cost_usd":"NaN","cost_known":True,"total_tokens":1},
     "w2.att1":{"cost_usd":"7.00","cost_known":True,"total_tokens":1}}
d=derive_selected_spend(per,{"w1":1,"w2":1},["w1","w2"])
out["nan_derivation"]=d
print(json.dumps(out,indent=2,default=str))
