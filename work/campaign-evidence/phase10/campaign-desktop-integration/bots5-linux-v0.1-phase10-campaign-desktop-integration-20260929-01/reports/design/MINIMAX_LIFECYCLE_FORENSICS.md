# Phase 10 lifecycle / failure forensics (MiniMax M2.7 Flash, parcel-v1 design-only)

Role: attack Phase 10 from concrete failure boundaries at the campaign engine seam.
Model routing: configured route `minimax/minimax-m2.7-flash`; effective route is the harness-provided identity.

Baseline verified live: `git rev-parse HEAD` → `0756904481ae884bb9e864e8e1e11fc4a27a72ff` (matches `parcel/BASELINE.json`); tracked tree clean.

Zero-provider tests run: `tests/test_runner.py tests/test_storage_events.py tests/test_cli_views.py tests/test_manifest.py` → **115 passed**, exit 0 (Python 3.14.7 venv).

Only file written: this report.

Legend: **[F]** = fact from pinned bytes at `0756904481ae884bb9e864e8e1e11fc4a27a72ff`. **[I]** = inference explicitly labelled. **[H]** = human semantic fork requiring Mick adjudication. **[B]** = blocking defect (safe convergence requires engine change outside Phase 10 scope).

---

## 0. Orientation

The campaign engine (`src/bots5/{runner,storage,events,models,usage}.py`) is a synchronous asyncio pipeline driven by `asyncio.run()` from `cli._cmd_run` (cli.py:110). The desktop has **no connection** to this pipeline today (Qwen archaeology §7.3, §14). Phase 10 creates that seam.

The core durable evidence is: `run.json` (state + stages), `stages/<id>.json` (per-stage records), `stages/<id>.md` (output text), `usage.json` (token/cost aggregates), `events.jsonl` (append-only narrative), `job.resolved.json` (frozen inputs), and `result.md` (synthesis output).

All failure analysis starts here: **the filesystem is the only authoritative record**. Any in-memory or UI-layer claim that contradicts filesystem evidence is wrong by definition.

---

## 1. The cancellation bug — exact mechanics

### 1.1 Grounded description

**[F]** `runner.run_job` (runner.py:253–517) has this terminal exception handling:

```
try:
    final_state = await asyncio.wait_for(pipeline(), timeout=...)
except TimeoutError:          # → Exception subclass → caught at line 465
    ...
except Exception as exc:      # line 465 — catches TimeoutError, ProviderError,
    ...                        # internal_error; does NOT catch CancelledError
```

**[F]** `asyncio.CancelledError` is a direct subclass of `BaseException` in Python ≥3.8 (PEP 479). It is **not** an `Exception`.

**[F]** `_execute_stage` (runner.py:127–215) does handle `CancelledError`:

```python
except asyncio.CancelledError:            # line 201
    if record.state not in (StageState.SUCCEEDED, StageState.FAILED):
        record.state = StageState.FAILED
        record.error_type = "run_timed_out"
        record.provider_side_outcome_unknown = started_monotonic is not None
        persist_stage(dirs, record)        # line 211
        events.write("stage_failed", ...)
    raise                                  # re-raises
```

So `CancelledError` caught inside `_execute_stage` produces a **terminal write** before re-raising.

**[F]** The `CancelledError` re-raise at line 215 propagates out of `_execute_stage` into the `async with` body, then into `asyncio.gather` (runner.py:330), then to `pipeline()`, then into `asyncio.wait_for` (runner.py:427), which **absorbs** the cancellation (because `wait_for` converts `CancelledError` into `TimeoutError` when the wait is cancelled). But `pipeline()` does **not** `await` the `asyncio.wait_for` call inside a `try` block that catches `BaseException`. Therefore:

- If the **run-level timeout** fires first: `TimeoutError` is raised, caught at runner.py:428–463, a terminal state is written, and the function returns normally.
- If **desktop-initiated cancellation** fires first (desktop window closes, desktop runtime cancels the campaign task): `CancelledError` propagates through the `asyncio.wait_for` call without being converted to `TimeoutError` (because the timeout hasn't fired), escapes the `except Exception` block, and becomes an **unhandled** `CancelledError` at the `asyncio.run()` call site in `cli._cmd_run` (cli.py:110).

**[F]** `cli._cmd_run` (cli.py:106–112) does not wrap `asyncio.run(run_job(...))` in any `try`/`except`. The `CancelledError` therefore propagates to the Python process level, which terminates the process with a non-zero exit and a traceback, but **leaves every `run.json` that was in `RUNNING` state at that exact moment still saying `RUNNING`** because the terminal `persist_run` call (runner.py:489–498) was never reached.

**[F]** The `persist_run` that would transition `RUNNING` → terminal lives at runner.py:489–498, called **only** at the bottom of `run_job`'s `try` block (after the `except TimeoutError` and `except Exception` branches have each already called it), and at runner.py:294 (in the initial-setup `except Exception` path). There is no `finally` block that guarantees terminal persistence.

**[I]** **The exact symptom**: a desktop-hosted campaign that is cancelled by UI shutdown leaves `run.json` with `"state": "running"`, and some stage JSON files with `state: "running"` for in-flight workers, with no terminal event in `events.jsonl` for those stages. The durable evidence claims the run is still in progress indefinitely.

### 1.2 When is this triggered?

**[F]** `run_job` is currently called only from `cli._cmd_run` via `asyncio.run()`. In the current codebase there is **no** desktop-hosted campaign runner, so the bug is latent. Phase 10 introduces the seam that makes this routine rather than exceptional.

**[F]** The desktop's own cancellation/reconciliation machinery (`application.py:1657–1714` `cancel_generation`, `ExecutionManager.shutdown`) uses task cancellation and reconciles `RUNNING → ABORTED` in the SQLite store. The campaign runner has **no equivalent reconciliation**. Once the Python process terminates with unhandled `CancelledError`, the filesystem is the only record.

### 1.3 The `provider_side_outcome_unknown` invariant

**[F]** `StageRecord.provider_side_outcome_unknown: bool = False` (models.py:110) is set to `True` when: a timeout occurs **after the stage has started** (runner.py:161, 206, 458); or `_best_effort_internal_failure` is called (which doesn't set it, only `run_timed_out` does). The field documents whether a provider may have done work that the harness cannot confirm.

**[I]** If a desktop cancellation lands while `asyncio.wait_for(provider.complete(request), timeout=...)` (runner.py:156) is pending or in-flight, the HTTP request is torn down by the OS at the TCP level. The provider **may** have processed it and billed for it; the harness **cannot** know. The invariant that the desktop must preserve: any stage whose last durable state is `RUNNING` and whose `started_at` is non-null **must** be treated as having `provider_side_outcome_unknown = True` on reconciliation, regardless of how the desktop discovers this.

---

## 2. Adversarial failure matrix

Each row: concrete crash/interruption point → durable evidence at that instant → required invariant → safe convergence / display behavior → fail-closed conditions.

---

### Matrix row 1: Desktop close during running campaign

**Crash point**: Desktop window/runtime closes while a campaign task is active in the same process or a child process.

**Durable evidence at crash point**:
- `run.json` `"state": "running"` (written at runner.py:281–289, never updated)
- Some `stages/<id>.json` with `"state": "running"` (written at runner.py:144 on `asyncio.Semaphore` acquisition)
- Some stages may have `"state": "queued"` (written at runner.py:292) if cancellation hit before semaphore acquisition
- No `run_timed_out` or `run_failed` or `run_succeeded` event in `events.jsonl` for this run
- `events.jsonl` ends with `stage_started` or `request_sent` for in-flight stages

**Required invariant**: No run directory claims to be in a terminal state when it is not. A run is either explicitly terminal (has a terminal `run.json` state) or it is in a non-terminal state and that claim is accurate.

**Safe convergence / display behavior**:
- Desktop shows the run as **state: unknown / interrupted** — not as succeeded, failed, or running
- The exact durable label must be distinct from `running` (which implies active progress) and from `failed` (which implies a recorded error)
- The UI may offer: (a) inspect the partial evidence as-is; (b) mark the run as abandoned (a bookkeeping state, not a new campaign state); (c) attempt restart only through explicit operator action that creates a new run id
- The UI **must not** silently transition this to `failed` because that fabricates an error type; the run may have succeeded server-side

**Fail-closed conditions**:
- If the desktop cannot confirm all stages are terminal → treat as uncertain / interrupted
- `provider_side_outcome_unknown` must be `True` for every stage with `started_at != null` and `state == running` at the moment of inspection
- Desktop **must not** infer provider-side completion or billing-stop certainty
- Desktop **must not** offer "resume" because `run_job` creates a new run id (storage.py:270 `run_id = run_id or new_run_id(...)`), so resuming into the same directory is not supported

---

### Matrix row 2: Provider request accepted but local outcome uncertain

**Crash point**: `await asyncio.wait_for(provider.complete(request), timeout=...)` (runner.py:156) is awaiting; the HTTP request was dispatched and the provider **may** have accepted it; the response has not been received.

**Durable evidence at crash point**:
- `run.json` `"state": "running"`
- `stages/<id>.json` with `"state": "running"`, `"started_at": "<timestamp>"`, all usage fields `null`, `provider_side_outcome_unknown: false` (not yet set — the flag is only set on error branches)
- `events.jsonl` has `request_sent` for this stage

**Required invariant**: If `started_at` is non-null and `state == running`, the outcome is unknown. `provider_side_outcome_unknown` must be `True` to reflect this honestly.

**Safe convergence / display behavior**:
- Display: **outcome unknown** — known cost = 0, unknown set includes this stage
- No fabricated cost, no fabricated tokens, no fabricated completion
- The run may be re-inspected by reloading from disk; the operator may start a new campaign job

**Fail-closed conditions**:
- `provider_side_outcome_unknown` defaults to `False` in the model (models.py:110) — the engine writes it to `True` only on error branches. For a crash at this point, the durable record has `False` by omission. The desktop **must not** assume `False` means confirmed-zero. It must treat a `running` stage with `started_at` as implicitly uncertain.
- Desktop **must not** assert the provider "did nothing" — the HTTP request was in flight
- Desktop **must not** auto-retry — acceptance means spend may have occurred (CONTRACT.md:83 "uncertain external acceptance cannot silently retry")

---

### Matrix row 3: Event/persist failure

**Crash point**: `persist_stage`, `persist_usage`, `persist_run`, or `events.write` raises `StorageError` after the data should have been committed but before the write completes.

**Durable evidence**:
- `stages/<id>.json` or `run.json` or `usage.json` may be missing, truncated, or in a partial-write state
- `_atomic_write` uses `os.replace` (storage.py:71) which is atomic on POSIX for a single file, but a multi-file transaction (e.g., `persist_stage` writes both `.json` and `.md`) is **not** atomic as a pair
- `events.jsonl` is append-only with per-line `fsync` (events.py:52–58); a failure during `write` raises `StorageError` and the event is lost, but prior events are preserved

**Required invariant**: If `run.json` says terminal state, all per-stage evidence is consistent with that state. If `run.json` says `running`, no stage JSON claims terminal state.

**Safe convergence / display behavior**:
- If `persist_stage` fails mid-write for `.md`: the `.json` record is updated (or not), the `.md` may be absent. This is survivable — output text is lost but the stage record is intact.
- If `persist_run` fails at runner.py:489: `run.json` still says `running`; `usage.json` and stage JSONs may be terminal. Display as interrupted / uncertain, same as Row 1.
- If `events.write` raises: the event is lost but the pipeline continues. Events are append-only narrative; final state is determined by the JSON documents. This is acceptable.

**Fail-closed conditions**:
- If `run.json` cannot be read as valid JSON → do not display fabricated state; show error
- If `stages/<id>.json` is missing for a stage listed in `run.json`'s `stage_order` → treat that stage as uncertain
- Multi-file `persist_stage` (`.json` + `.md`) failure half-way: the `.json` is authoritative; absent `.md` is displayed as "no output text"

---

### Matrix row 4: Crash between attempt creation and terminal settlement

**Crash point**: `create_run_tree` succeeded (runner.py:271, `run.json` written with `RUNNING`), initial `persist_stage` calls for queued stages succeeded (runner.py:291–293), but the first `asyncio.Semaphore` worker has not yet been awaited.

**Durable evidence**:
- `run.json` `"state": "running"`, `"stage_order": ["w1", "w2", ...]`
- All `stages/<id>.json` with `"state": "queued"`
- `events.jsonl` has `run_started` + per-stage `stage_queued`
- No `stage_started`, `request_sent`, `stage_succeeded`, or `stage_failed` events

**Required invariant**: `"state": "running"` with all stages `"state": "queued"` means no stage has started work.

**Safe convergence / display behavior**:
- This is a genuinely early-stage interruption. The operator can start a new campaign with the same job file; the old run directory is an abandoned partial.
- Display: **interrupted (not started)** — not `running` (which implies active work), not `failed` (which implies a recorded error).
- The old run directory is inspectable but cannot be continued.

**Fail-closed conditions**:
- If any stage has `started_at != null` → reclassify as Row 1 or Row 2
- No stage can be upgraded from `queued` to any terminal state without evidence

---

### Matrix row 5: Partial worker set (some stages complete, some not)

**Crash point**: Some workers completed (their `persist_stage` calls succeeded at runner.py:197), synthesis has not started, cancellation lands.

**Durable evidence**:
- Some `stages/<id>.json` with `"state": "succeeded"`, `completion_complete: true`, `known_cost_usd` non-null
- Some `stages/<id>.json` with `"state": "running"` and `started_at != null` (in-flight at crash)
- `run.json` `"state": "running"`
- `usage.json` with partial aggregate cost (some stages known, some unknown)
- `events.jsonl` partial: some `stage_succeeded`, then cancellation before further events

**Required invariant**: A stage's `state` is accurate to what the harness actually persisted. Completed stages are correct; in-flight stages reflect uncertainty.

**Safe convergence / display behavior**:
- Display: **partial — some workers succeeded, N worker(s) uncertain** with explicit list of uncertain stage ids
- Known cost is truthful (sum of stages with `known_cost_usd != null`). Unknown stages are in the `unknown_cost_stage_ids` set.
- Synthesis is blocked because `run.json` is not terminal; display: synthesis not reached.
- The operator may start a new campaign job. The partial run directory is preserved as evidence.

**Fail-closed conditions**:
- Do not show a sum that includes only the known stages as if it were total cost — the `usage.json` aggregate correctly separates known and unknown
- Do not show synthesis as `failed` — synthesis was never attempted, so `skipped` is the accurate label for any future inspection of this run
- In-flight stages must display `provider_side_outcome_unknown: true` (by inference from `started_at != null` and `state == running`)

---

### Matrix row 6: Synthesis gate/rerun crash

**Crash point**: Worker phase succeeded; synthesis is being prepared or executed; cancellation lands.

**Durable evidence**:
- All worker `stages/<id>.json` with `"state": "succeeded"`
- Synthesis `stages/<id>.json` with `"state": "running"` (if `persist_stage` called at runner.py:144 for synthesis) or still `"state": "queued"` (if synthesis task not yet created)
- `run.json` `"state": "running"`
- `events.jsonl`: all `stage_succeeded` events for workers, then cancellation

**Required invariant**: Synthesis `state == running` with no output means synthesis was interrupted. It is not a failure — it is an uncertain outcome.

**Safe convergence / display behavior**:
- Display: **synthesis uncertain** alongside worker completion
- `known_cost_usd` reflects worker cost only; synthesis cost is unknown
- The synthesis dependency chain is intact in `job.resolved.json`; any future synthesis run would need to re-read worker outputs (which are preserved)

**Fail-closed conditions**:
- Synthesis `state` must not be displayed as `failed` or `skipped` — both imply a recorded decision. The correct display is "not completed / uncertain".
- If the synthesis stage record has `started_at != null` → `provider_side_outcome_unknown = True` by inference

---

### Matrix row 7: UI observer lag (stale filesystem projection)

**Crash point**: Not a crash — a timing gap where the UI is showing a projection that is older than the current filesystem state.

**Durable evidence**: Any combination from rows 1–6, depending on when the last poll/refresh occurred.

**Required invariant**: The filesystem is authoritative; UI is always a projection. A UI that hasn't refreshed does not create new evidence.

**Safe convergence / display behavior**:
- UI should display the last known filesystem state with an explicit staleness indicator (e.g., "last refreshed N seconds ago; refresh to update")
- No automatic refresh should mutate evidence
- After a visible refresh, the new filesystem state supersedes the old projection

**Fail-closed conditions**:
- If a poll returns `run.json` with `state: running` and a later poll returns `state: succeeded`, the UI must not animate a transition from `running → succeeded` without confirming the new filesystem state is stable
- UI **must not** display `running` for a run whose `run.json` has been terminal for longer than the poll interval (a `run.json` in terminal state does not spontaneously change)

---

### Matrix row 8: Stale filesystem state

**Crash point**: The desktop has not yet observed a terminal write that has already occurred on disk (e.g., a long-running stage just completed but the UI hasn't polled).

**Durable evidence**: Same as the terminal state that was just written — `run.json` may now say `succeeded` or `failed`, but the UI is still showing `running`.

**Required invariant**: The terminal `run.json` is definitive evidence. The UI projection is stale.

**Safe convergence / display behavior**:
- On next poll/refresh: show the terminal state with a clear indicator that this is the current state, not a transition
- Do not animate a fake "transition" — just update to the current truth

**Fail-closed conditions**:
- If the filesystem says terminal and the UI says `running`, the UI is wrong. Correct it silently on next refresh without claiming a transition occurred.

---

### Matrix row 9: Restart with run marked running

**Crash point**: Desktop restarts after a crash (or deliberate close) left `run.json` in `state: running`. On restart, the desktop loads and must decide what to show.

**Durable evidence at restart moment**:
- `run.json` `"state": "running"`
- Some or all `stages/<id>.json` in `running` or `queued` state
- `events.jsonl` ends mid-narrative
- `job.resolved.json` is intact (written at runner.py:279, before any stage ran)
- `usage.json` is in initial all-unknown state (written at runner.py:280)

**Required invariant**: `run.json` claiming `running` after a process exit is a **factual contradiction**. A process that exits without a terminal `persist_run` call cannot be "still running." The run is interrupted.

**Safe convergence / display behavior**:
- Desktop detects `run.json state: running` on load and classifies as **interrupted / abandoned** (a stable non-terminal state label, distinct from both `running` and any terminal state)
- The desktop **must not** re-open the run for "resumption" — `run_job` always creates a new `run_id` (storage.py:270); the existing directory cannot be continued
- The desktop **must not** fabricate a terminal outcome — the evidence does not support it
- The operator may inspect the partial evidence; may start a new campaign job

**Fail-closed conditions**:
- If any stage has `started_at != null` and `state == running` → `provider_side_outcome_unknown = True` by inference (the provider may have processed the request)
- No `persist_run` with a terminal state is ever called for this run directory after this detection
- The desktop **must not** call `persist_run` itself to "fix" the state — that would be fabricating evidence

---

### Matrix row 10: Regeneration cancellation (Phase 10 specific)

**Crash point**: Operator initiates a selected-worker regeneration (creates a sibling attempt); the new attempt's provider request is in flight or accepted; the operator then cancels.

**Durable evidence**:
- Original attempt's `stages/<id>.json` is preserved (append-only sibling — design requirement, but current storage layer does `os.replace` over the same filename, so this requires the Phase 10 storage redesign)
- New attempt's `stages/<id>.json` may be in `running` state at crash
- `events.jsonl` may have an appended event for the new attempt (if Phase 10 extends the event vocabulary)
- `run.json` may or may not reflect the new attempt depending on when the crash occurred

**Required invariant**: Original attempt evidence is never overwritten. New attempt evidence is either terminal or uncertain, never both.

**Safe convergence / display behavior**:
- Display: the original attempt is intact. The new attempt is shown as **uncertain / interrupted** if it was in flight.
- The original attempt's state is unchanged.
- The desktop **must not** merge or choose between sibling attempts — selection is operatorexplicit.

**Fail-closed conditions**:
- If the storage redesign (append-only sibling per §12.1 of Qwen archaeology) is not implemented, the original attempt **will be overwritten** by `persist_stage` using the same `stage.id` filename. This is a **design requirement that cannot be satisfied by the current storage layer**.
- Provider cancellation for the new attempt must follow the same uncertainty rules as rows 1–2 for that attempt's stage record.

---

### Matrix row 11: New Job while activity exists

**Crash point**: Operator clicks "New Job" while a campaign is active (either in the desktop's campaign task or in a child process).

**Durable evidence at click moment**: Any state from rows 1–10 depending on current progress.

**Required invariant**: `New Job` is a UI working-context operation. It never deletes or modifies filesystem evidence. Active work is not cancelled by `New Job`.

**Safe convergence / display behavior**:
- If active work is in progress: prompt the operator — "A campaign is currently running. Switching to New Job will not stop it. Confirm?"
- `New Job` clears the desktop's selected/loaded state, removes the current job from the UI context, but leaves the filesystem evidence and any child process untouched.
- The running campaign continues; when it completes, its evidence is at the filesystem path and can be inspected separately.

**Fail-closed conditions**:
- `New Job` must not call `run_job` or mutate any `run.json`
- If a campaign task is running in the desktop process and the operator confirms `New Job`, the desktop should offer to cancel the active task first — not silently abandon it leaving durable `running` state

---

## 3. The desktop-must-NEVER rules (absolute, derived from the matrix)

These rules are not implementation preferences. They are failure-closed invariants that any Phase 10 design must satisfy to avoid fabricating campaign outcomes.

### Rule D-1: Never fabricate a terminal state

The desktop **must never** write `run.json` or any stage JSON to change a state that was not written by the campaign engine. The filesystem is authoritative. The desktop is a reader and projector.

*Derivation*: Matrix rows 1, 9. The `persist_run` call at runner.py:489 is the only code that transitions `run.json` to a terminal state. The desktop has no equivalent call.

### Rule D-2: Never fabricate completion or success

If the desktop cannot read a terminal `run.json` state from the filesystem, it **must not** display the run as succeeded, failed, timed-out, or complete. It must display it as interrupted / uncertain.

*Derivation*: Matrix rows 1, 9. `run.json state: running` after process exit is an interrupt, not a failure.

### Rule D-3: Never fabricate recovery or resume

A campaign job creates a new `run_id` on each execution (storage.py:270). There is no resume path in the current engine. The desktop **must not** offer a "resume" operation that would overwrite or continue an existing run directory.

*Derivation*: Matrix row 1, CONTRACT.md:81–82. `create_run_tree` raises `StorageError` on duplicate `run_id` (storage.py:109–110). Reusing a `run_id` is impossible.

### Rule D-4: Never fabricate provider-stop certainty

For any stage with `started_at != null` and `state == running` (or any stage that was in that state at the last filesystem read), the desktop **must** treat `provider_side_outcome_unknown` as `True` even if the durable record says `False` by omission. The desktop **must not** assert that the provider stopped work, billed zero, or produced no output.

*Derivation*: Matrix rows 2, 5, 6. The `provider_side_outcome_unknown` flag is written by the engine only on error branches. By omission it is `False`. For a crashed in-flight stage, this `False` is factually incorrect — the provider may have processed the request.

### Rule D-5: Never fabricate live cost progress

Cost evidence appears in the filesystem only at terminal completion (runner.py:192 `_apply_result` assigns `known_cost_usd`). The desktop **must not** display a running cost projection derived from token counting or rate math. It may display: known subtotal + explicit unknown set.

*Derivation*: Matrix row 2, CONTRACT.md:63–68, Qwen archaeology §5.1–5.2. `stream()` is unused by the campaign runner (Qwen archaeology §5.1, confirmed by grep).

### Rule D-6: Never auto-retry after uncertain acceptance

If the desktop cannot confirm whether a provider request was accepted (state `running`, `started_at != null`), it **must not** automatically initiate a retry or re-run. Any re-run requires explicit operator approval after full disclosure of the uncertainty.

*Derivation*: Matrix rows 1, 2, CONTRACT.md:83.

### Rule D-7: Never leave cancellation without a terminal record

If the desktop initiates a cancellation (window close, explicit cancel button), it **must** ensure a terminal `run.json` state is eventually written. This requires either: (a) the campaign engine is modified to treat `CancelledError` as a terminal-writing path; or (b) the desktop serializes cancellation through a wrapper that confirms terminal persistence before considering the campaign done.

*Derivation*: Matrix row 1, §1 above. The current engine's `except Exception` (runner.py:465) does not catch `CancelledError`. This is the **primary blocking defect** for Phase 10 lifecycle integrity.

### Rule D-8: New Job never deletes evidence

`New Job` clears the UI working context. It **must not** delete, rename, or modify any filesystem evidence. A campaign run directory that exists continues to exist after `New Job`.

*Derivation*: Matrix row 11, CONTRACT.md:85–86, F-12.

---

## 4. The cancellation defect: exact required fix specification

### 4.1 The defect is in the campaign engine

**[B]** The `CancelledError` non-handling in `run_job` (runner.py:465: `except Exception` — does not cover `BaseException`) is a **pre-existing engine defect** that Phase 10 exposes but cannot fix by adding a desktop layer. Any Phase 10 design that does not address this will produce routine durable `running` states on every normal desktop close.

### 4.2 The design must require one of two paths

**Path A — Engine treats `CancelledError` as terminal (preferred, requires engine mutation)**:

`run_job`'s outer `try`/`except` is changed to:
```python
except Exception as exc:
    ...
except asyncio.CancelledError:
    # Cancel all worker tasks gracefully
    for task in worker_tasks:
        if not task.done():
            task.cancel()
    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)
    # Write terminal state for all non-terminal stages
    for record in records:
        if record.state not in (StageState.SUCCEEDED, StageState.FAILED):
            record.state = StageState.FAILED
            record.error_type = "cancelled"
            record.error_message = "run cancelled by operator"
            record.provider_side_outcome_unknown = True
            record.ended_at = now_iso()
            persist_stage(dirs, record)
    events.write("run_cancelled")  # requires EVENT_TYPES addition
    persist_run(..., state=RunState.CANCELLED, ...)  # requires RunState.CANCELLED
```

This requires: (a) `RunState.CANCELLED` added to `RunState` enum (models.py:10–14); (b) `"run_cancelled"` added to `EVENT_TYPES` (events.py:13–25); (c) `_execute_stage`'s own `CancelledError` handler remains unchanged — it still writes per-stage terminal state and re-raises; (d) the outer handler catches `CancelledError` separately and writes the aggregate terminal state.

**Path B — Desktop wrapper guarantees terminal state before exit**:

The desktop wraps the campaign runner in a process or task that:
1. Drives the campaign to completion or cancellation
2. **Before the desktop process exits**, confirms `run.json` shows a terminal state by reading it from disk
3. If `run.json` still shows `running` after the campaign task settles, treats this as an interrupted run and explicitly displays it as such without modifying the filesystem

Path B does **not** require engine changes but requires the desktop to own the campaign task lifecycle completely. Path B is what the archaeology report (QWEN §7.3) calls "either the desktop must drive `run_job` to a terminal persist before allowing close, or the engine must treat cancellation as a terminal-writing path."

### 4.3 What the design must require (not suggest)

CONTRACT.md:81–82 states: "A crash may leave a durable run that says `running`; do not silently call that resumable/succeeded." This tolerance is for **crash** scenarios (OS-level kill, power loss) where the engine physically could not write. A desktop **initiated** close is not a crash — it is a deliberate operator/system action that the engine and desktop **can** coordinate to produce a terminal record.

**The Phase 10 design must require**: either Path A or Path B. A design that does not require either leaves the cancellation defect unaddressed and makes durable `running` states a routine Phase 10 artifact, violating CONTRACT.md:81–82 in the common case (not just the crash edge case).

This is a **HUMAN_SEMANTIC_FORK**: the choice between engine mutation (Path A) and desktop-lifecycle ownership (Path B) is a product architecture decision not settled by current authority. Both are technically feasible. The supervisor must adjudicate.

---

## 5. Synthesis staleness and the append-only obligation

### 5.1 Current storage is overwrite-only for stage evidence

**[F]** `persist_stage` (storage.py:119–125) writes `stages/<id>.json` and `stages/<id>.md` using filenames derived **only** from `stage.id`. A second write for the same id **overwrites** the first (via `os.replace`, storage.py:71). `persist_run` (storage.py:156) aggregates into a dict keyed by stage id — duplicate ids would silently collapse.

**[F]** `result.md` is written at a fixed path (storage.py:162–163) — no attempt discriminator.

**[I]** For Phase 10 sibling regeneration to preserve the original attempt (CONTRACT.md:28–29), the storage layer **must** change to support append-only sibling evidence. This is not a Phase 10 desktop feature — it is an engine extension that Phase 10 requires.

### 5.2 Synthesis staleness mechanics

**[F]** Synthesis gating resolves by `record_by_id[dep].state` and `record_by_id[dep].completion_complete` (runner.py:344, 363). The dependency is by stage id equality only. Today there is exactly one record per id, so this is unambiguous.

**[F]** After Phase 10 sibling regeneration: if `w1` now has two attempt records (e.g., `w1` and `w1__a2`), the synthesis dependency by id alone is ambiguous. The desktop must track which attempt was used by which synthesis run.

**[I]** The safe design: each synthesis run records the exact attempt identities it consumed. When a worker sibling is created, all synthesis runs that used the old sibling become stale. Staleness is a label, not an automatic action. The operator must explicitly trigger a synthesis rerun.

### 5.3 The containment check blocking attempt paths

**[F]** `load_stage_view` (storage.py:196–198): `candidate.parent != (run_dir / "stages").resolve(strict=False)` — this **exactly one level** check rejects the obvious nested attempt layout (`stages/w1/attempts/a2.md`). Any attempt path design that nests files deeper than `stages/<id>.<ext>` will be rejected by this reader.

**[I]** Resolution options: (a) keep all attempt outputs in `stages/` with attempt-discriminated filenames (`stages/<id>__<attempt>.md`); (b) change the containment predicate to allow one level of nesting (`stages/<id>/<attempt>.md`); (c) use a separate `attempts/` directory at the run-root level. Each has different reader/writer compatibility implications. This is a **HUMAN_SEMANTIC_FORK** — the design must choose one explicitly.

---

## 6. UI observer lag and the polling contract

### 6.1 Bounded polling is the sanctioned pattern

**[F]** Phase 9 uses bounded polling for live progress: `POLL_INTERVAL_MS=250`, `POLL_MAX_INTERVAL_MS=1000`, poll only while visible (phase9_queue_dock.py:136–201). Stale token markers produce "Queue changed — refresh." not auto-retry (phase9.py:41–51).

**[I]** For Phase 10 campaign projection: the same discipline applies. Poll the filesystem (read `run.json` + relevant stage JSONs) at a bounded interval; display staleness explicitly; never auto-retry on staleness.

### 6.2 No event bus for campaign transitions

**[F]** `EventWriter` writes `events.jsonl` but **no code reads it** (Qwen archaeology §6: "there is no reader"). The campaign runner has no event bus. Any "live progress" UI must poll the JSON documents, not subscribe to events.

**[I]** Adding a campaign event bus would make the desktop "a second campaign engine" (CONTRACT.md:78–79). The filesystem polling approach is the only one consistent with the current engine's design.

### 6.3 The cwd asymmetry is a live trap

**[F]** CLI `status/inspect --runs-dir` resolves relative to `Path.cwd()` (paths.py:54); `manifest.resolve_runs_dir` resolves relative to the **job file's directory** (paths.py:33–39). A desktop process has a different cwd than the operator's shell.

**[I]** Phase 10 must handle this explicitly: either require absolute paths for campaign runs, or resolve paths relative to the job file's directory (consistent with how the engine itself resolves them).

---

## 7. Cost truth across partial states

### 7.1 Known cost is additive and truthful

**[F]** `usage.json` is written at three moments: initial all-unknown snapshot (runner.py:280), after internal failure best-effort (runner.py:83), and at terminal completion (runner.py:484). Each write replaces the previous `usage.json`.

**[F]** `aggregate_cost` (usage.py:9–33) correctly separates `known_sum_usd`, `status ∈ {zero, known, partial, unknown}`, and `unknown_cost_stage_ids`. A partial run has `status: partial` and lists the uncertain stage ids.

**[I]** Truthful live cost display: show the `usage.json` aggregate as-is. Do not interpolate or project. Show `cost_status: partial` and the list of stages with unknown cost. Do not show a running dollar total.

### 7.2 Provider-reported cost is the only authoritative cost

**[F]** `known_cost_usd` is assigned once at `_apply_result` (runner.py:48) from the provider's response. There is no pricing registry or rate lookup in the campaign engine.

**[I]** The "live cost progress" that Phase 10 can show is: known subtotal from completed stages + the set of stages with unknown cost. This is the maximum truthful projection.

---

## 8. HUMAN_SEMANTIC_FORK register

| # | Fork | Options | Authority gap |
|---|---|---|---|
| H-1 | Cancellation handling path | Engine mutation (Path A) vs desktop-lifecycle ownership (Path B) | §4.3 above; CONTRACT.md:81–82 does not specify engine-change vs desktop-wrapper |
| H-2 | Attempt path layout | `stages/<id>__<attempt>.md` flat (breaks `load_stage_view` containment) vs one-level nesting `stages/<id>/<attempt>.md` (changes containment predicate) vs separate `attempts/` root | Qwen archaeology §12.1; CONTRACT.md:43–47; `storage.py:196–198` |
| H-3 | Run discovery | Browse past campaigns (needs enumeration seam) vs operator-supplied path/id only | Qwen archaeology §2.3, §8; CONTRACT.md:19 does not specify |
| H-4 | Preflight pricing authority | Operator-entered rates vs provider lookup vs cached metadata vs unknown+approval | CONTRACT.md:71–74; Qwen archaeology §4.1; F-06 |
| H-5 | Restart persistence of loaded campaign | Desktop app DB (existing seam) vs filesystem-only vs explicit operator action | CONTRACT.md:85–86; F-15 |
| H-6 | Approval binding substrate | Content digest / snapshot vs re-validation at execution vs human-only binding | CONTRACT.md:51–60; Qwen archaeology §3.3 |

---

## 9. BLOCKED conditions

| Condition | Blocking reason | Required action |
|---|---|---|
| Cancellation leaves durable `running` (no terminal write) | Violates CONTRACT.md:81–82 in the routine case; desktop fabricates recovery if it displays as anything other than interrupted | Phase 10 design must require Path A or Path B from §4.3 — cannot be left unaddressed |
| Append-only sibling storage not specified | Regeneration requirement (CONTRACT.md:28–29, F-01) cannot be satisfied without storage redesign; current `persist_stage` overwrites original | Design must include exact storage mutation fence for append-only sibling evidence; without this, sibling regeneration is not implementable |
| Attempt path containment not resolved | `load_stage_view` containment check (storage.py:196–198) will reject the obvious nested layout; a design that ignores this will fail at runtime | H-2 must be adjudicated before implementation |

---

## 10. Summary of failure classes and safe behaviors

| Failure class | Durable evidence at failure | Safe UI behavior | Must never |
|---|---|---|---|
| Desktop close during run | `run.json: running`; stages `running`/`queued` | Show as **interrupted / uncertain** | Fabricate terminal state, offer resume, assert provider-stop |
| Provider accepted, outcome unknown | Stage `running`, `started_at != null`, `known_cost_usd = null` | Show as **uncertain** with `provider_side_outcome_unknown = true` | Assert zero cost, assert no billing, auto-retry |
| Persist failure (partial write) | Some files updated, some not | Show best-effort; treat incomplete as uncertain | Fabricate complete state from partial writes |
| Crash after creation, before start | All stages `queued` | Show as **interrupted (not started)** | Fabricate running or failed |
| Partial worker completion | Some stages `succeeded`, some `running` | Show partial with explicit unknown list | Fabricate total cost, fabricate synthesis result |
| Synthesis crash | Workers `succeeded`, synthesis `running` or `queued` | Show workers done + **synthesis uncertain** | Fabricate synthesis `failed` or `skipped` |
| UI observer lag | Stale projection | Show staleness indicator; refresh to update | Animate fake transitions, auto-refresh to "fix" |
| Restart with `running` state | `run.json: running` after process exit | Classify as **abandoned** on load | Reopen for resume, write terminal state to "fix" |
| Regeneration cancellation | Original attempt preserved (if storage redesigned); new attempt uncertain | Show original intact + new attempt uncertain | Overwrite original, auto-complete new attempt |
| New Job during activity | Any state; filesystem untouched | Prompt if active work exists; clear UI context only | Cancel the campaign, delete evidence |

---

## 11. Findings for supervisor integration

1. **Cancellation defect (B)**: `run_job`'s `except Exception` (runner.py:465) does not catch `asyncio.CancelledError` (Python ≥3.8: `BaseException`). Desktop-initiated close leaves durable `run.json: running`. Phase 10 design must require Path A (engine change) or Path B (desktop wrapper) — this is a HUMAN_SEMANTIC_FORK that the supervisor must adjudicate before implementation proceeds.

2. **Storage overwrite (B)**: `persist_stage` uses `stage.id` as the sole filename discriminator; sibling regeneration overwrites the original attempt. The Phase 10 regeneration requirement (CONTRACT.md:28–29) cannot be implemented without a storage redesign. This is a **blocking dependency** for the regeneration feature.

3. **Attempt path containment (B)**: `load_stage_view`'s containment check (storage.py:196–198) rejects any output path nested more than one level below `stages/`. Attempt path design (H-2) must resolve this before implementation.

4. **No auto-retry after uncertain acceptance (D-6)**: For any stage with `started_at != null` and `state == running`, the desktop must treat the provider outcome as unknown. This is the primary counterfactual to any "it probably didn't bill" assumption. No automatic retry is ever safe after an uncertain acceptance.

5. **Cost truth boundary**: Live cost progress means known subtotal plus explicit unknown set. The `usage.json` aggregate already provides this. No token-dollar projection is safe without a pricing authority the campaign engine does not have (H-4).

6. **Event bus absence**: The campaign runner writes `events.jsonl` but nothing reads it. "Live progress" must poll `run.json` and stage JSONs — filesystem polling, not event subscription. Adding a campaign event bus would make the desktop a second campaign engine (CONTRACT.md:78–79).

7. **cwd asymmetry**: Desktop process cwd differs from operator shell cwd; CLI paths resolve differently. Phase 10 must use absolute paths or resolve relative to the job file directory.

8. **HUMAN_SEMANTIC_FORK H-1** (cancellation path) is the most consequential open question. Both Path A and Path B are feasible but have different mutation footprints. The supervisor must adjudicate before the design seal is finalized.
