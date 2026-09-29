"""M0.2 executed demonstration: typed errors, additive events, dual accounting.

Run from the repo root with the project venv:
    PYTHONPATH=src .venv/bin/python .m02-demo/demo_m02.py

Covers the required proof points:
  1. usage_document / aggregate_cost byte-identical vs `git show HEAD` (v1 stage set)
  2. w1.att1 + w1.att2, selection w1 -> 2: selected cost derives from att2 while
     cumulative includes both; unlisted selection defaults to att1
  3. derive_selected_spend never reads a cache (zero file opens, in-memory input)
  4. selected_cache_is_stale detects a deliberately corrupted stored summary
  5. D-9 provider-outcome classification matrix
  6. every new EVENT_TYPES member appendable; unknown type still raises StorageError
  7. persist_usage_v2 round-trip on a real run tree + typed ApprovalInvalidatedError
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from decimal import Decimal
from pathlib import Path

import bots5.errors as errors_mod
import bots5.storage as storage_mod
from bots5.errors import (
    ApprovalInvalidatedError,
    Bots5Error,
    ProviderError,
    ProviderHttpError,
    ProviderResponseError,
    ProviderTimeoutError,
    StorageError,
    provider_side_outcome_unknown,
)
from bots5.events import EVENT_TYPES, EventWriter
from bots5.models import StageRecord, StageState
from bots5.storage import consume_approval, create_run_tree, new_run_id, persist_usage_v2, read_json
from bots5.usage import (
    aggregate_cost,
    derive_selected_spend,
    selected_cache_is_stale,
    usage_document,
    usage_document_v2,
)

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def canon(doc) -> str:
    """Serialize exactly the way storage.atomic_write_json does."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


# --- 1. byte-identical legacy behaviour vs git HEAD --------------------------

section("1. usage_document / aggregate_cost byte-identical vs `git show HEAD` (v1 stage set)")
head_src = subprocess.run(
    ["git", "show", "HEAD:src/bots5/usage.py"], capture_output=True, text=True, check=True
).stdout
head_src = head_src.replace(
    "from .models import CostSummary, StageRecord",
    "from bots5.models import CostSummary, StageRecord",
)
head_mod = types.ModuleType("usage_at_head")
exec(compile(head_src, "HEAD:src/bots5/usage.py", "exec"), head_mod.__dict__)

v1_stages = [
    StageRecord(
        id="w1", provider="openrouter", requested_model="qwen/qwen3-8b",
        state=StageState.SUCCEEDED, prompt_tokens=120, completion_tokens=340,
        reasoning_tokens=0, total_tokens=460, known_cost_usd=Decimal("0.0012"),
    ),
    StageRecord(
        id="w2", provider="openrouter", requested_model="qwen/qwen3-8b",
        state=StageState.SUCCEEDED, prompt_tokens=100, completion_tokens=200,
        reasoning_tokens=10, total_tokens=310, known_cost_usd=Decimal("0.0021"),
    ),
    StageRecord(
        id="synthesis", provider="openrouter", requested_model="qwen/qwen3-8b",
        state=StageState.SUCCEEDED, prompt_tokens=500, completion_tokens=900,
        reasoning_tokens=20, total_tokens=1420, known_cost_usd=None,
    ),
]
for label, stages in [
    ("v1 stage set (known + unknown cost)", v1_stages),
    ("empty stage set", []),
    ("all-unknown stage set", [v1_stages[2]]),
]:
    check(f"usage_document dict-equal vs HEAD [{label}]", head_mod.usage_document(stages) == usage_document(stages))
    check(
        f"usage_document byte-identical vs HEAD [{label}]",
        canon(head_mod.usage_document(stages)) == canon(usage_document(stages)),
    )
    check(f"aggregate_cost equal vs HEAD [{label}]", head_mod.aggregate_cost(stages) == aggregate_cost(stages))
print("\n--- canonical serialization of the v1 stage set (byte-identical to HEAD) ---")
print(canon(usage_document(v1_stages)), end="")

# --- 2. dual accounting: two attempts, selection w1 -> 2 ---------------------

section("2. att1 + att2 executed, selection w1 -> 2 (selected derives from att2; cumulative = both)")
att1 = StageRecord(
    id="w1", provider="openrouter", requested_model="qwen/qwen3-8b",
    state=StageState.SUCCEEDED, prompt_tokens=100, completion_tokens=200,
    reasoning_tokens=0, total_tokens=300, known_cost_usd=Decimal("0.0010"), attempt_number=1,
)
att2 = StageRecord(
    id="w1", provider="openrouter", requested_model="qwen/qwen3-8b",
    state=StageState.SUCCEEDED, prompt_tokens=150, completion_tokens=260,
    reasoning_tokens=5, total_tokens=415, known_cost_usd=Decimal("0.0025"), attempt_number=2,
)
doc = usage_document_v2([att1, att2], selected_attempts={"w1": 2}, stage_ids=["w1"], run_id="demo-run")
per_attempt = doc["per_attempt"]
derived = derive_selected_spend(per_attempt, {"w1": 2}, ["w1"])
check("per_attempt keyed 'w1.att1' and 'w1.att2'", set(per_attempt) == {"w1.att1", "w1.att2"})
check("selected_spend cost == att2 cost ('0.0025')", doc["selected_spend"]["cost_usd_known_sum"] == "0.0025")
check("derive_selected_spend cost == '0.0025'", derived["cost_usd_known_sum"] == "0.0025")
check("cumulative_spend cost includes BOTH attempts ('0.0035')", doc["cumulative_spend"]["cost_usd_known_sum"] == "0.0035")
check("selected_spend total_tokens == att2 (415)", doc["selected_spend"]["total_tokens_known_sum"] == 415)
check("cumulative_spend total_tokens == 715", doc["cumulative_spend"]["total_tokens_known_sum"] == 715)
check("aggregate mirrors the derived selected_spend", doc["aggregate"] == derived)
check(
    "stages['w1'] is the SELECTED attempt (att2) in the legacy per-stage shape",
    doc["stages"]["w1"]
    == {
        "prompt_tokens": 150,
        "completion_tokens": 260,
        "reasoning_tokens": 5,
        "total_tokens": 415,
        "cost_usd": "0.0025",
        "cost_known": True,
    },
)
check("evidence_version == 2", doc["evidence_version"] == 2)
check("unlisted stage defaults to attempt 1 ('0.0010')", derive_selected_spend(per_attempt, {}, ["w1"])["cost_usd_known_sum"] == "0.0010")

# --- 3. purity: derivation never reads a cache (zero file opens) --------------

section("3. derive_selected_spend never reads a cache (audit-hook proof: zero file opens)")
opens: list[str] = []


def _audit_hook(event, args):
    if event == "open":
        opens.append(str(args[0]))


sys.addaudithook(_audit_hook)
before = len(opens)
derived_pure = derive_selected_spend(
    {"w1.att2": {"total_tokens": 1, "cost_known": True, "cost_usd": "9.99"}},
    {"w1": 2},
    ["w1"],
)
after = len(opens)
check(f"zero 'open' audit events during derivation ({after - before} observed)", after == before)
check("derived purely from the in-memory map ('9.99')", derived_pure["cost_usd_known_sum"] == "9.99")

# --- 4. staleness detection ---------------------------------------------------

section("4. selected_cache_is_stale detects a deliberately corrupted stored summary")
check("honest cache (copy of derived) is NOT stale", selected_cache_is_stale(dict(derived), derived) is False)
corrupted_sum = dict(derived)
corrupted_sum["cost_usd_known_sum"] = "0.0000"
check("corrupted cost_usd_known_sum -> stale", selected_cache_is_stale(corrupted_sum, derived) is True)
corrupted_status = dict(derived)
corrupted_status["cost_status"] = "unknown"
check("corrupted cost_status -> stale", selected_cache_is_stale(corrupted_status, derived) is True)
corrupted_complete = dict(derived)
corrupted_complete["cost_complete"] = False
check("corrupted cost_complete -> stale", selected_cache_is_stale(corrupted_complete, derived) is True)
corrupted_ids = dict(derived)
corrupted_ids["unknown_cost_stage_ids"] = ["w1.att1"]
check("corrupted unknown_cost_stage_ids -> stale", selected_cache_is_stale(corrupted_ids, derived) is True)
check("non-dict stored cache -> stale (fail-safe)", selected_cache_is_stale(None, derived) is True)

derived_unknown = derive_selected_spend({"w1.att1": {"cost_known": False, "cost_usd": None}}, {"w1": 1}, ["w1"])
check(
    "unknown cost derives 'unknown'/incomplete and never interpolates ('0')",
    derived_unknown["cost_status"] == "unknown"
    and derived_unknown["cost_usd_known_sum"] == "0"
    and derived_unknown["cost_complete"] is False,
)
invented = dict(derived_unknown)
invented.update({"cost_usd_known_sum": "0.01", "cost_status": "known", "cost_complete": True})
check("interpolated ('invented') cache detected as stale", selected_cache_is_stale(invented, derived_unknown) is True)

# --- 5. D-9 provider-outcome classification -----------------------------------

section("5. D-9 provider-outcome classification")
check("ProviderHttpError(404).definitive_rejection is True", ProviderHttpError(404, "x").definitive_rejection is True)
check("404 -> provider_side_outcome_unknown False", provider_side_outcome_unknown(ProviderHttpError(404, "x")) is False)
check("400 -> False (definitive)", provider_side_outcome_unknown(ProviderHttpError(400, "x")) is False)
check("429 -> True", provider_side_outcome_unknown(ProviderHttpError(429, "x")) is True)
check("408 -> True", provider_side_outcome_unknown(ProviderHttpError(408, "x")) is True)
check("503 -> True", provider_side_outcome_unknown(ProviderHttpError(503, "x")) is True)
check("399 -> True (not a 4xx)", provider_side_outcome_unknown(ProviderHttpError(399, "x")) is True)
check("ProviderTimeoutError -> True", provider_side_outcome_unknown(ProviderTimeoutError("late")) is True)
check("ProviderTimeoutError.definitive_rejection is False", ProviderTimeoutError("late").definitive_rejection is False)
check("ProviderResponseError -> False", provider_side_outcome_unknown(ProviderResponseError("unusable")) is False)
check("ProviderResponseError.definitive_rejection is True", ProviderResponseError("unusable").definitive_rejection is True)
check("bare ProviderError -> True (fail-safe)", provider_side_outcome_unknown(ProviderError("mystery")) is True)
check("ProviderError.definitive_rejection is False", ProviderError("mystery").definitive_rejection is False)
check("unclassified RuntimeError -> True (fail-safe)", provider_side_outcome_unknown(RuntimeError("surprise")) is True)
check("hierarchy untouched (ProviderHttpError -> ProviderError -> Bots5Error)",
      issubclass(ProviderHttpError, ProviderError) and issubclass(ProviderError, Bots5Error))
check("message behaviour untouched (str(ProviderHttpError(404, 'nope')) == 'nope')", str(ProviderHttpError(404, "nope")) == "nope")

# --- 6. additive EVENT_TYPES; writer stays fail-closed ------------------------

section("6. additive EVENT_TYPES; writer stays fail-closed")
legacy_types = {
    "run_started", "stage_queued", "stage_started", "request_sent", "stage_succeeded",
    "stage_failed", "stage_skipped", "synthesis_blocked", "run_timed_out", "run_succeeded", "run_failed",
}
new_types = [
    "attempt_selected", "synthesis_stale", "run_cancelled",
    "worker_regeneration_started", "worker_regeneration_finished",
    "synthesis_rerun_started", "synthesis_rerun_finished",
]
check("all 11 legacy kinds still present", legacy_types <= EVENT_TYPES)
check("all 7 new kinds present", set(new_types) <= EVENT_TYPES)
check(f"EVENT_TYPES has exactly {len(legacy_types) + len(new_types)} members", len(EVENT_TYPES) == 18)
with tempfile.TemporaryDirectory() as td:
    log = Path(td) / "events.jsonl"
    writer = EventWriter(log, "demo-run")
    for kind in new_types:
        writer.write(kind, stage_id=None if kind == "run_cancelled" else "w1", attempt_number=2, reason="demo")
    parsed = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    check("every new kind appended, in order", [p["event"] for p in parsed] == new_types)
    check("lines remain parseable line-by-line with ts/run_id/event",
          all({"ts", "run_id", "event"} <= set(p) for p in parsed))
    raised = False
    try:
        writer.write("not_a_real_event_type")
    except StorageError:
        raised = True
    check("unknown event type still raises StorageError (fail-closed)", raised)

# --- 7. persist_usage_v2 round-trip + typed approval refusal ------------------

section("7. persist_usage_v2 round-trip + typed ApprovalInvalidatedError from errors.py")
check("storage.ApprovalInvalidatedError IS errors.ApprovalInvalidatedError (fallback removed)",
      storage_mod.ApprovalInvalidatedError is errors_mod.ApprovalInvalidatedError)
with tempfile.TemporaryDirectory() as td:
    dirs = create_run_tree(Path(td) / "runs", new_run_id("demo"))
    written = persist_usage_v2(dirs, [att1, att2], selected_attempts={"w1": 2}, stage_ids=["w1"])
    on_disk = read_json(dirs.root / "usage.json")
    check("persist_usage_v2 returns exactly the document it wrote", on_disk == written)
    check("written document carries evidence_version 2", on_disk["evidence_version"] == 2)
    # F-07: selection moves to att1 -> derivation (authority) follows selection.json
    # while the stored selected_spend cache still describes att2.
    moved = derive_selected_spend(on_disk["per_attempt"], {"w1": 1}, ["w1"])
    check(
        "after selection moves to att1 the derivation reports '0.0010' while the stored cache says '0.0025'",
        moved["cost_usd_known_sum"] == "0.0010"
        and on_disk["selected_spend"]["cost_usd_known_sum"] == "0.0025"
        and selected_cache_is_stale(on_disk["selected_spend"], moved) is True,
    )
    consume_approval(dirs.root, "approval-demo", {"approval_id": "approval-demo", "scope": "full_run"})
    refused = False
    try:
        consume_approval(dirs.root, "approval-demo", {"approval_id": "approval-demo", "scope": "full_run"})
    except ApprovalInvalidatedError as exc:
        refused = True
        print(f"       typed refusal: ApprovalInvalidatedError: {exc}")
    check("spent approval raises the typed ApprovalInvalidatedError", refused)

print()
if failures:
    print(f"DEMONSTRATION FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("DEMONSTRATION PASSED: all checks green")
