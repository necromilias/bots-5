# IMPL_LIFECYCLE_REVIEW.md — Phase 10 Independent Lifecycle Review

## A. Seal Verification

**STATUS: VERIFIED — ALL 17 FILES MATCH**

All 17 file digests from `PHASE10_CANDIDATE_SEAL_v2.json` recomputed from the live worktree using Python `hashlib.sha256` — **0 mismatches**:

```
✓ src/bots5/bootstrap/desktop.py       1f7cd690...436e6b
✓ src/bots5/cli.py                     dd0fe6a0...684b06
✓ src/bots5/core/campaign.py           0e7c2785...9d36f
✓ src/bots5/desktop/campaign_dock.py   ec1a8455...f230b4
✓ src/bots5/desktop/window.py          588f4c3f...45cb5d
✓ src/bots5/errors.py                  1a5ec370...3aaa8e
✓ src/bots5/events.py                  59d51f54...367529
✓ src/bots5/models.py                  71366297...0162c43
✓ src/bots5/runner.py                  c0ae3300...812a47c
✓ src/bots5/storage.py                 8decde78...a0ee86
✓ src/bots5/usage.py                   542e886c...70e60a
✓ tests/test_phase10_backward_compat.py    4e2f4f47...59acee5
✓ tests/test_phase10_cross_cutting.py      fbb8891d...c98060
✓ tests/test_phase10_desktop_lifecycle.py  e739f3f6...3314298
✓ tests/test_phase10_desktop_preflight.py  a7dd1c56...542c18
✓ tests/test_phase10_desktop_projection.py  3b8eac75...b8b3b9
✓ tests/test_phase10_evidence_regeneration.py 1b1a67b2...862283
```

- `file_count: 17` matches the 17 sealed paths.
- `tracked_modified_in_fence: 9` files; `tracked_modified_out_of_fence: []`; `fence_violations: []`.
- Seal `candidate_id` and the identifying hash (`6db93b97...`) recorded in the seal file are self-consistent.
- Head commit `0756904481ae884bb9e864e8e1e11fc4a27a72ff` matches live git.

## B. Findings

### F-L1 — INFO — F-A2-1 CLOSED: full-run approval target binding now enforced

**Evidence:** `runner.py:272–277` — `_require_approval_binding` constructs the exact expected full-run target dict `{"run_id": run_id, "stage_id": None, "attempt_number": None}` and compares with `!=`, not `!=` on individual fields. The falsification engine's minimal reproduction (wrong `stage_id` or non-null `attempt_number` with correct `run_id`) now raises `ApprovalInvalidatedError` before any dispatch.

```python
expected_target = {"run_id": run_id, "stage_id": None, "attempt_number": None}
if not isinstance(approval.target, dict) or approval.target != expected_target:
    target = approval.target if isinstance(approval.target, dict) else None
    raise refuse(
        f"approval target {target!r} does not match the full-run target {expected_target!r}"
    )
```

- Inside-fence: yes (`runner.py`).

---

### F-L2 — INFO — F-A3-1 CLOSED: operator cancellation of regeneration/rerun writes `cancelled`, not `cancelled_pending`

**Evidence:** `runner.py:1856–1866` (regenerate_worker) and `runner.py:2079–2088` (rerun_synthesis) each have `except asyncio.CancelledError:` that calls `_terminalize_operation_cancellation`. That function (`runner.py:1679–1705`) writes `error_type = "cancelled"` and `record.error_message = "stage cancelled by operator"`, persists the stage, calls `_persist_operation_usage`, and writes the finished event — all before re-raising. The transient `cancelled_pending` written by `_execute_stage:796` is overwritten before any other code can observe it.

```python
# runner.py:1679
def _terminalize_operation_cancellation(...):
    record.state = StageState.FAILED
    record.error_type = "cancelled"
    record.error_message = "stage cancelled by operator"
    persist_stage_attempt(dirs, record, attempt_number=attempt_number)
    events.write("stage_failed", record.id, error_type="cancelled")
    _persist_operation_usage(dirs, stage_order)   # per-attempt usage updated
    events.write(operation_finished_event, ...)
```

- Inside-fence: yes (`runner.py`).

---

### F-L3 — INFO — F-A4-1 CLOSED: per-attempt usage persisted at each attempt completion

**Evidence:** `runner.py:957–961` sets `terminal_usage_callback` to `_persist_operation_usage` when `v2_mode` is True. This callback is invoked by `persist_terminal_usage()` (`runner.py:716–718`) inside `_execute_stage` at every terminal path: success (line 786), timeout (line 752), ProviderError (line 764), generic Exception (line 776), and the CancelledError path (after stage record is updated). `_persist_operation_usage` (`runner.py:1661–1676`) reloads all attempt records from disk and calls `persist_usage_v2` to refresh per-attempt and cumulative summaries. The CancelledError path in `regenerate_worker`/`rerun_synthesis` also calls `_terminalize_operation_cancellation → _persist_operation_usage` before re-raising, so cancellation also refreshes usage.

```python
# runner.py:716
def persist_terminal_usage() -> None:
    if usage_callback is not None:
        usage_callback()   # called on every terminal path
```

- Inside-fence: yes (`runner.py`).

---

### F-L4 — INFO — F-A5-1 CLOSED: missing declared dependency in provenance returns UNVERIFIABLE

**Evidence:** `storage.py:946–961` checks `missing_consumed = [dep for dep in depends_on if dep not in consumed_dependencies]` and similarly for `dependency_digests`. If either list is non-empty, the classification is set to `SYNTHESIS_FRESHNESS_UNVERIFIABLE`, `integrity_warning` is set `True`, and a descriptive warning listing the missing keys is appended. This is distinct from the mechanical-staleness path (lines 969–1014) which only runs when both maps contain every declared dependency.

```python
# storage.py:946
missing_consumed = [dep for dep in depends_on if dep not in consumed_dependencies]
missing_digests = [dep for dep in depends_on if dep not in dependency_digests]
if missing_consumed or missing_digests:
    report["classification"] = SYNTHESIS_FRESHNESS_UNVERIFIABLE
    report["integrity_warning"] = True
    report["warnings"].append(
        "dispatched synthesis provenance is missing declared dependency binding(s) ("
        + "; ".join(missing_parts) + ")"
    )
    return report
```

- Inside-fence: yes (`storage.py`).

---

### F-L5 — INFO — F-A2-2 CLOSED: paid approval pricing enforcement with HSF-1 Branch A evidence

**Evidence:** Three-layer enforcement:

1. **Runner binding** (`runner.py:400–409`): `_require_approval_binding` checks `has_paid_route and approval.pricing_evidence is None` → raises `ApprovalInvalidatedError`; then compares `canonical_json(expected_pricing) != canonical_json(approval.pricing_evidence)` → raises `ApprovalInvalidatedError`. No dispatch proceeds.

2. **Bridge validation** (`core/campaign.py:817–821`): `_validate_prepared_pricing` calls `build_pricing_evidence(stages, routes, prepared.approval.pricing_evidence)` and raises if `expected != approval.pricing_evidence`. Called in `approve_and_start`, `approve_and_regenerate`, `approve_and_rerun_synthesis` before any provider is constructed.

3. **build_pricing_evidence** (`runner.py:421–509`): validates operator entries have `input_usd_per_1m`, `output_usd_per_1m`, `rate_source`, `observed_at`, checks all paid routes are covered (lines 448–450), validates `input_rate.is_finite()`, `output_rate >= 0` (lines 459–460), and binds the route/model identity (lines 467–475). Returns the conservative upper bound per stage.

4. **Desktop dock** (`desktop/campaign_dock.py:589–591`): the Approve button is enabled only when `not paid or prepared.approval.pricing_evidence is not None`.

5. **Local-only exemption** (`runner.py:432`): `paid = [s for s in stages if routes.get(s.get("provider"), {}).get("kind") != "local_openai"]` — `local_openai` routes never require evidence.

6. **Legacy plain run** (`cli.py:188–194`): `_cmd_run` calls `run_job(job, providers)` with no snapshot/approval, so `v2_mode = False` and pricing checks are not reached.

No bypass found. Inside-fence: yes (`runner.py`, `core/campaign.py`, `models.py`).

---

### F-L6 — INFO — Cancellation matrix: all cells honest

Truth table verified against code paths:

| Cell | Trigger | Durable run state | Stage error_type | provider_unknown | Code path |
|---|---|---|---|---|---|
| Overall timeout | `TimeoutError` in `pipeline()` | `TIMED_OUT` | `run_timed_out` (relabelled from `cancelled_pending` at `runner.py:1143–1153`) | preserved | `runner.py:1103–1156` |
| Explicit full-run cancel | `CancelledError` caught | `CANCELLED` (HSF-4) | `cancelled` (relabelled at `runner.py:1172–1178`) | preserved | `runner.py:1157–1209` |
| Internal failure (generic) | `Exception` | `FAILED, internal_error` | `cancelled` for sibling `cancelled_pending` (O-2/O-3 at `runner.py:628–641`); `internal_error` for QUEUED/RUNNING | preserved for cancelled sibling | `runner.py:1210–1225` |
| Internal failure with cancelled sibling | same as above | same as above | sibling: `cancelled` (explicit O-2 sweep); run: `FAILED internal_error` | preserved | `runner.py:628–641` |
| Operator cancel regeneration | `CancelledError` in `regenerate_worker` | N/A (parent run unchanged) | `cancelled` (via `_terminalize_operation_cancellation runner.py:1688–1691`) | `record.started_at is not None` | `runner.py:1857–1866` |
| Operator cancel synthesis rerun | `CancelledError` in `rerun_synthesis` | N/A (parent run unchanged) | `cancelled` (via `_terminalize_operation_cancellation`) | `record.started_at is not None` | `runner.py:2080–2088` |
| Hard kill (SIGKILL/power loss) | OS termination | `running` or `FAILED+cancelled_pending` (depends on timing) | `cancelled_pending` (N-4 honest limit) | set if stage had started | `runner.py:789–807`; reader applies N-4 rule |

No cell reports success for an uncertain outcome. No auto-retry path exists. The `cancelled_pending` durable outcome is possible only from a hard-kill and is honestly displayed as `interrupted_uncertain`.

---

### F-L7 — INFO — V3 SHUTDOWN: bounded cancel-and-drain before authority release

**Evidence:** `bootstrap/desktop.py:246–264` — `_close_campaigns` iterates all tracked bridges, calling:
```python
await asyncio.wait_for(bridge.cancel(), _CAMPAIGN_CLOSE_TIMEOUT_SECONDS)
await asyncio.wait_for(bridge.close(), _CAMPAIGN_CLOSE_TIMEOUT_SECONDS)
```
with `_CAMPAIGN_CLOSE_TIMEOUT_SECONDS = 30.0`. The timeout is per-bridge; exceptions are caught and appended to close errors, and the finally block discards the bridge. No retry of provider work occurs.

**Precedence:** `_close_campaigns` runs at rank 2 (`"campaign": 2`), before `authority.release()` at rank 1 (`"outer_authority": 1`) and `"application": 1` (`bootstrap/desktop.py:366–378`). Authority is released last.

**Desktop window teardown** (`desktop/window.py:1944–1989`): `_finish_close → stop_bridge → _campaign_dock.drain → bridge.close()`. The dock's own `drain()` awaits `bridge.close()` which, when combined with the close driver's outer `wait_for`, gives a total effective shutdown budget of 60s for the cancel+close of each bridge.

**No unbounded stage:** `CampaignBridge.cancel()` has no internal timeout, but the caller's `wait_for(bridge.cancel(), 30s)` enforces it. `CampaignBridge.close()` similarly has no timeout but is called inside `wait_for(bridge.close(), 30s)`. If both time out, the bridge is discarded and close proceeds.

- Inside-fence: yes (`bootstrap/desktop.py`, `core/campaign.py`).

---

### F-L8 — INFO — V5 HONEST REPORTING SURFACES: no fabricated cost, no bare running as success

**Desktop dock:**

- `live_cost_line` (`desktop/campaign_dock.py:167–191`): returns `"cost: {known_str}{unknown_text} ({status})"` with `known_str = "?"` when unknown, and `unknown_text` lists all unknown stage IDs explicitly. No accrual, no dollar interpolation.
- Projection `live_cost.basis` (`core/campaign.py:509–512`): `"known subtotal plus explicit unknown set over the selected attempts; provider cost lands only at terminal completion; no accrual is fabricated"`.
- Synthesis freshness (`desktop/campaign_dock.py:727–731`): shown verbatim from `projection.synthesis_freshness` (value from `_synthesis_freshness` classification), with integrity warnings from `projection.integrity_warnings` shown verbatim in a red label.
- Per-stage display (`desktop/campaign_dock.py:691–721`): shows `error_type` verbatim, `"Unknown outcome"` suffix when `provider_side_outcome_unknown=True`, amber background for unknown-outcome failures.
- `_sync_polling` (`desktop/campaign_dock.py:932–943`): stops polling when `display_state` is terminal; never auto-retries.

**CLI:**

- `bots5 status` (`cli.py:197–230`): shows `state` verbatim from `run.json`, shows `aggregate_cost` with explicit `status=` and `complete=` fields, appends freshness markers from `reconstruct_run_state`.
- `bots5 inspect` (`cli.py:257–281`): shows full metadata verbatim including `failure.type`, `failure.message`.

**Display classification** (`core/campaign.py:367–374`): `_display_state` returns `"running"` only when `hosted=True`; otherwise `"interrupted_uncertain"`. A durable `running` without a hosted task is never success/failure/timed_out/cancelled — always `interrupted_uncertain`.

- Inside-fence: yes (all files above).

## C. V1..V5 Verdicts

### V1 — REOPENED FINDINGS CHECK

**F-A2-1 (full-run approval target binding): CLOSED**
Code: `runner.py:272–277`. Evidence: `expected_target = {"run_id": run_id, "stage_id": None, "attempt_number": None}; approval.target != expected_target` raises `ApprovalInvalidatedError`. No dispatch proceeds with a mismatched target.

**F-A3-1 (ordinary cancellation of regeneration/rerun leaves `cancelled_pending`): CLOSED**
Code: `runner.py:1856–1866`, `runner.py:2079–2088`, `runner.py:1679–1705`. Both `regenerate_worker` and `rerun_synthesis` call `_terminalize_operation_cancellation` in their `CancelledError` handler, which writes `error_type = "cancelled"`, persists the stage, refreshes usage, and writes the finished event before re-raising. The `cancelled_pending` written by `_execute_stage:796` is a transient marker overwritten before any observation.

**F-A4-1 (completed attempt usage not written until pipeline ends): CLOSED**
Code: `runner.py:957–961` (callback registration), `runner.py:716–718` (callback invocation on every terminal path), `runner.py:1661–1676` (`_persist_operation_usage` that refreshes per-attempt and cumulative summaries from disk). Every terminal path in `_execute_stage` calls `persist_terminal_usage()`, including cancellation. Cancellation paths additionally call it via `_terminalize_operation_cancellation`.

**F-A5-1 (incomplete provenance mislabeled STALE, not UNVERIFIABLE): CLOSED**
Code: `storage.py:946–961`. The missing-declared-dependency check returns `SYNTHESIS_FRESHNESS_UNVERIFIABLE` with `integrity_warning = True` and a descriptive message listing the absent bindings. This is unreachable for the mechanical-staleness predicate, which only runs when both maps contain every declared dependency.

**F-A2-2 (paid approval without Mick-adjudicated pricing evidence): CLOSED**
Code: `runner.py:400–409` (binding enforcement), `core/campaign.py:817–821` (bridge validation), `runner.py:421–509` (evidence construction with conservative bound), `desktop/campaign_dock.py:589–591` (dock gate). The enforcement is at three layers, no dispatch proceeds for a paid route without complete evidence covering all paid providers, rates, source, observation time, and bound against live routes/models/ceilings.

### V2 — CANCELLATION MATRIX: SOUND

All seven cells verified above (F-L6). No cell fabricates success or auto-retries. `cancelled_pending` as a durable marker is confined to the hard-kill window and honestly displayed as `interrupted_uncertain` (N-4). Every cell distinguishes the run state, stage error type, and provider-side uncertainty correctly.

### V3 — SHUTDOWN: SOUND

`_close_campaigns` (`bootstrap/desktop.py:246–264`) is bounded by `wait_for(..., 30s)` per bridge. It runs before `application.close()` and before `authority.release()` (precedence rank 2 vs rank 1). No retry of provider work occurs. The `except BaseException` in `_close_driver` records the error but continues. If `cancel()` or `close()` times out, the bridge is discarded and close proceeds; the orphaned task terminates when the process exits.

The window teardown path (`desktop/window.py:1944–1989`) drains the campaign dock before the window closes, providing the same guarantee for the per-window close.

### V4 — PRICING ENFORCEMENT: SOUND

Verified: a paid approval truly cannot reach dispatch without complete HSF-1 Branch A evidence (three enforcement layers). The evidence is recomputed via `build_pricing_evidence` and bound against live routes, models, and output ceilings. A local-only operation returns `None` from `build_pricing_evidence` and is exempt. The legacy `bots5 run` verb uses the `v2_mode=False` path with no snapshot/approval and no pricing check — unaffected. No bypass path found in any code that constructs providers or dispatches for a paid route.

### V5 — HONEST REPORTING SURFACES: SOUND

Dock presents `live_cost_line` with explicit unknown set (no accrual), verbatim `synthesis_freshness` classification, verbatim `integrity_warnings`, verbatim `error_type` per stage, and amber/red state-color coding. Projection `live_cost.basis` explicitly says "no accrual is fabricated". CLI shows `state` verbatim with explicit `status=` and `complete=` fields. Display state rule (`_display_state`) is honest: durable `running` without a hosted task is always `interrupted_uncertain`. No fabricated cost, no bare running state as success.

## D. Overall

**YES — the repaired candidate is honest and internally consistent.**

The repair wave correctly closed all five findings from the falsification engine:
- F-A2-1: full target dict comparison enforced.
- F-A3-1: `_terminalize_operation_cancellation` writes `cancelled` (not `cancelled_pending`) for handled cancellations.
- F-A4-1: `usage_callback` fires on every terminal path in `_execute_stage`, including cancellation, refreshing per-attempt usage.
- F-A5-1: missing declared dependency in provenance returns `UNVERIFIABLE` with an integrity warning.
- F-A2-2: three-layer paid-approval pricing enforcement with conservative upper bound.

The cancellation matrix, shutdown bounds, and honest reporting surfaces are all sound.

**The one thing a human acceptor should look at first:**

The **`approve_and_start` gate in `core/campaign.py:1244`** — it calls `_validate_prepared_pricing` which recomputes `build_pricing_evidence` and raises if the result doesn't match the already-recorded `approval.pricing_evidence`. The question to verify is whether the `approval.pricing_evidence` written into the `PreparedOperation` by `prepare_full_run` (which computed it at preflight time, `core/campaign.py:959`) is already the correct conservative-bound form — which it is, because `prepare_full_run` calls `build_pricing_evidence` with the same stage/route arguments that `_validate_prepared_pricing` uses. The two calls compute identical results; the comparison at line 820 (`expected != prepared.approval.pricing_evidence`) would only fire if the operator-supplied evidence was empty, which the dock's Approve-button gate (`campaign_dock.py:590`) already prevents. Confirm this invariant holds if any future change touches `prepare_full_run`'s pricing computation or the dock's approval-enable logic.
