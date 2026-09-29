"""A4: NaN per_attempt entry -> selected_spend 'NaN' with status known/complete."""
import json, sys
sys.path.insert(0,"/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1/src")
from bots5.usage import derive_selected_spend, selected_cache_is_stale
per = {"w1.att1": {"cost_usd": "NaN", "cost_known": True, "total_tokens": 10},
       "w2.att1": {"cost_usd": "0.01", "cost_known": True, "total_tokens": 10}}
sel = {"w1": 1, "w2": 1}
d = derive_selected_spend(per, sel, ["w1","w2"])
print("derived:", d)
stored = {"cost_usd_known_sum": "0.02", "cost_status":"known","cost_complete":True,"unknown_cost_stage_ids":[]}
print("cache stale vs derived:", selected_cache_is_stale(stored, d))
