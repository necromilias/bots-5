# Desktop surface and lifecycle

Scope: the desktop surface, its projection/authority boundary, the bounded evidence
polling contract, failure/uncertainty display truth, cancellation terminalization, shutdown
participation and `New Job` semantics.
Sources: Qwen §7–§8, §13; Gemini §7–§8; MiniMax §1–§6, §8–§11; Command §4; GLM §2/§5/§6.

> **Revision history.** v1 → v2: F-04/D-9 (provider-error uncertainty classification) and
> F-05 (cancellation labelling). v2 → v3: M-4 (generic-failure path reclassifies
> `cancelled_pending`), §5.2. v3 → v4: N-3 (skipped synthesis is NOT_APPLICABLE, not
> UNVERIFIABLE) and N-4 (honest hard-kill limit; sibling keeps its own cancellation cause).
> See `reports/design/DESIGN_REPAIR_RECORD_v3_to_v4.md`.

## 1. Authority boundary

The desktop is a **projection and command surface**, never a second engine.

| Concern | Owner | Not the desktop |
|---|---|---|
| Run-directory creation | engine (`storage.create_run_tree`) | — |
| Provider dispatch | engine (`runner._execute_stage` → `provider.complete`) | — |
| Durable stage/run/usage/selection evidence | engine (`storage.persist_*`) | — |
| "Current attempt" truth | filesystem (`selection.json`) | UI memory, app DB |
| Cost/token truth | filesystem (`usage.json`, stage docs) | UI arithmetic, interpolation |
| Staleness truth | filesystem predicate (`REGENERATION_AND_STALE_SYNTHESIS.md` §4) | UI flag |
| Result truth | filesystem (`run.json` state + selected synthesis) | widget state |

If UI state ever disagrees with the filesystem, the filesystem wins and the UI is corrected
on the next poll without claiming a transition occurred.

## 2. Components

### 2.1 `src/bots5/core/campaign.py` (new, Qt-free)

`CampaignBridge`:

- `validate(job_path)` — zero-spend validation via `manifest.load_job` +
  `manifest.validate_referenced_files`; creates nothing.
- `prepare_preflight(job_path)` — builds the frozen `PreflightSnapshot` (digests, compiled
  messages) without writing anything.
- `approve(snapshot, actor)` — returns an `ApprovalRecord` bound to the digest.
- `start_run(snapshot, approval)` — hosts `runner.run_job(...)` as a task on the desktop
  event loop through the existing single-flight task discipline; returns a handle.
- `regenerate_worker(...)`, `rerun_synthesis(...)` — the corresponding engine entry points
  under their own preflight/approval.
- `make_current(run_dir, stage_id, attempt_number)` — explicit selection write + event.
- `project(run_dir)` — reads the run directory and returns an immutable
  `CampaignProjection`.
- `close()` — single-flight, loop-pinned cancel-and-drain (see §5).

`CampaignProjection` (immutable): run identity/state/display classification, per-stage
selected attempt, available attempts, state, completion, output path, tokens/cost,
uncertainty, staleness, selected-synthesis digest status, and the dual cost summaries.
No Qt types; directly unit-testable with plain `asyncio.run`.

### 2.2 `src/bots5/desktop/campaign_dock.py` (new, Qt)

`CampaignDock` + `CampaignViewModel`:

- **Load**: explicit job path (HSF-3); shows job identity after load.
- **Validate**: "Validating… (zero spend)"; on success shows validated identity and a
  content digest; on failure shows the typed refusal verbatim.
- **Preflight/approve**: modal showing exact stages, requested models, provider route
  names, token ceilings/timeouts, the preflight digest, and the pricing-evidence branch
  (HSF-1). One-shot explicit approval; no auto-confirm.
- **Progress**: bounded poll of the projection; per worker/stage/synthesis state; cost as
  known subtotal + explicit unknown set; no fabricated dollar accrual.
- **Result**: durable outcome until `New Job`; expandable persisted output per attempt;
  typed `successful / failed / timed_out / partial / interrupted / cancelled`.
- **Attempts**: list per stage, mark current, explicit "Make current", explicit
  "Regenerate worker" (model changeable, provider route shown and locked), explicit
  "Rerun synthesis" with stale/fresh indication.
- **New Job**: clears working context only.

Reuse discipline (from Phase 9): stable `objectName`s for theming/tests; `_disabled_button`
inert-affordance pattern for unavailable actions; typed refusals shown verbatim, never
collapsed; view-model derived from a sealed projection only.

### 2.3 `src/bots5/desktop/window.py` (modify, narrow)

Attach the campaign dock at the existing `MainWindow` dock composition seam, mirroring how
the Phase 9 import-queue dock is attached. No Phase 9 dock/dialog/bridge behavior changes.

### 2.4 `src/bots5/bootstrap/desktop.py` (modify, narrow)

Compose `CampaignBridge` into `build_runtime`/`serve`; register campaign task cancellation
in the close driver **before** authority release, and await terminal persistence before the
close driver proceeds. Authority acquisition order and the existing `_close_driver`
precedence table are preserved; the campaign step is inserted as one more bounded stage with
its own precedence slot.

`src/bots5/core/application.py` remains **zero-diff**: campaign composition happens in
bootstrap, not inside the 2775-line Phase 1–9 authority machinery.

## 3. Bounded evidence polling contract

- `POLL_INTERVAL_MS = 250`, `POLL_MAX_INTERVAL_MS = 1000`, matching the Phase 9
  import-queue discipline.
- Poll only while the dock is visible and the run is non-terminal.
- Each poll reads `run.json`, `stages/*` for the selected attempts, `usage.json` and (for
  display only) tails of `events.jsonl`.
- A stale read triggers an explicit "evidence changed — refresh" affordance; **never**
  auto-retry, never auto-mutate.
- Polling is read-only: it can never create or alter evidence.
- There is **no** campaign event bus and no SQLite shadow state. `EventWriter` writes the
  append-only log; it is a narrative for display, never the correctness source.

## 4. Failure / uncertainty truth (desktop-must-NEVER rules)

Derived from MiniMax's failure matrix; these are invariants, not preferences.

- **D-1** Never write a campaign state the engine did not write. The desktop never calls
  `persist_run`/`persist_stage` to "fix" a run.
- **D-2** If no terminal `run.json` state is readable, never display succeeded/failed/
  timed-out/complete. Display `interrupted / uncertain`.
- **D-3** Never offer resume. A run id is a one-shot namespace; there is no resume path.
- **D-4** For any stage whose durable state is `running` with `started_at != null`, treat
  `provider_side_outcome_unknown` as **true** even if the stored flag is false by omission
  (the flag is only written on error branches). Never assert the provider stopped, billed
  zero, or produced nothing.
- **D-5** Never fabricate live cost. Provider cost lands only at terminal completion.
  Display known subtotal + explicit unknown set.
- **D-6** Never auto-retry after uncertain acceptance. Any re-run is a new explicit
  approval after full disclosure.
- **D-7** Desktop-initiated cancellation must produce a terminal durable record (see §5).
- **D-8** `New Job` never deletes or modifies evidence.
- **D-9 (F-04 repair)** A stage that was dispatched (`request_sent` emitted) and then failed
  is `unknown` unless the failure is a **definitive non-acceptance**. The engine classifies:
  - **definitive non-acceptance** ⇒ `provider_side_outcome_unknown = False`: HTTP 4xx
    excluding 408/429 (`ProviderHttpError.status_code`), credential/config rejection raised
    before any network write, and other errors explicitly marked
    `ProviderError.definitive_rejection = True`;
  - **ambiguous** ⇒ `provider_side_outcome_unknown = True`: transport errors
    (`provider_transport_error`), connection reset/refused after send, read failures,
    `ProviderTimeoutError`, HTTP 5xx/408/429, and any *unclassified* `ProviderError` or
    unexpected exception raised after `request_sent`.
  The default for an unclassified post-dispatch failure is **ambiguous** (fail-safe to
  "unknown"), and it is never auto-retried. `ProviderResponseError` (a response was
  received but unusable) is a definitive-reception case and is displayed as
  failed-but-received, distinct from ambiguous transport failure.
  The `ProviderError.definitive_rejection` class attribute is added in `src/bots5/errors.py`
  (in fence); no provider adapter change is required, and existing adapters raising plain
  `ProviderError` remain conservatively ambiguous.

### 4.1 Display classification

| Durable evidence | Display |
|---|---|
| `run.json` terminal `succeeded`/`failed`/`timed_out` | exact typed outcome |
| `run.json` `running`, dock hosts the task | running, live projection |
| `run.json` `running`, no hosted task (loaded from disk / after restart) | **interrupted / uncertain** (not resumable, not success/failure) |
| all stages `queued`, run `running`, no task | interrupted (not started) |
| some stages terminal, some `running` with `started_at` | partial + explicit uncertain stage list |
| terminal `failed` with `provider_side_outcome_unknown=True` | failed — provider outcome unknown; never auto-retried |
| terminal `failed` with a definitive non-acceptance | failed — not accepted by provider |
| terminal `failed` with a received-but-unusable response | failed — request received, response unusable |
| synthesis `running`/`queued` with workers done | synthesis not completed / uncertain |
| selected synthesis provenance present and mismatched | mechanically stale (+ integrity warning if digests mismatch) |
| selected synthesis provenance present but output bytes missing | stale + integrity warning |
| selected synthesis **skipped / never dispatched** (`dependency_failed`, `dependency_incomplete`, cost threshold, not reached) | show the recorded skip reason; provenance not applicable |
| selected synthesis provenance absent/malformed on a v2 run **and dispatched** | **unverifiable** (integrity warning) |
| selected synthesis provenance absent on a v1 run | current, provenance not recorded — pre-v2 evidence |
| durable stage `error_type = "cancelled_pending"` (only possible after a hard process kill in the outer-terminalization window) | interrupted / uncertain; never a normal outcome |

## 5. Cancellation and shutdown

### 5.1 The defect

`runner.run_job`'s outer handler is `except Exception` (`runner.py:465`);
`asyncio.CancelledError` derives from `BaseException`, so a desktop-initiated cancellation
escapes without a terminal `persist_run`. `_execute_stage` does handle cancellation
(`runner.py:201-215`) and re-raises. Result today: a normal UI close can leave `run.json`
durably `running` (Qwen [X]1, MiniMax H-1/B, Step §11).

### 5.2 Adopted mechanism (fixed): engine-side terminalization

**Stage-level marker (F-05 repair).** `_execute_stage`'s `except asyncio.CancelledError`
block (`runner.py:201-215`) currently writes `error_type="run_timed_out"`, which falsely
labels a user-requested cancellation as a timeout. Repair: it writes a **neutral** terminal
marker instead — `state=FAILED`, `error_type="cancelled_pending"`,
`provider_side_outcome_unknown = (started_monotonic is not None)`, `ended_at`, duration —
then re-raises. The cause is determined by the outer terminalization, which is the only
place that knows whether the overall run timeout fired or an operator cancelled:

- `run_job`'s `except TimeoutError` branch (`runner.py:428-464`) reclassifies records with
  `error_type == "cancelled_pending"` to `run_timed_out` (the true cause), preserving
  `provider_side_outcome_unknown`;
- `run_job`'s new `except asyncio.CancelledError` branch (below) reclassifies them to the
  HSF-4 cancellation error type, preserving `provider_side_outcome_unknown`.

**Outer branch.** `run_job` gains an explicit `except asyncio.CancelledError:` branch that:

1. cancels and awaits all outstanding worker tasks with `return_exceptions=True`;
2. sweeps every non-terminal record to a terminal state with
   `provider_side_outcome_unknown = (record.started_at is not None)`;
3. reclassifies every `cancelled_pending` record to the HSF-4 cancellation error type
   (never `run_timed_out`);
4. persists affected stages and appends `stage_failed`/`run_cancelled` events;
5. persists the terminal aggregate run state;
6. re-raises `CancelledError` **after** durable terminalization.

Because the cancellation branch sits ahead of the generic `except Exception` handler and
re-persists the affected records, the final durable record carries a truthful cause. A
genuine overall timeout continues to be labelled `run_timed_out`.

**Generic-failure path (M-4 / N-4 repair).** The third outer path — the existing
`except Exception` / `_best_effort_internal_failure` (`runner.py:465-480`, `runner.py:56-98`)
— sweeps only `QUEUED`/`RUNNING` records, so a sibling stage already written as
`FAILED` + `cancelled_pending` could survive as a transitional label. That path is extended
to also reclassify any `cancelled_pending` record to `error_type = "cancelled"` with the
message "stage was cancelled while the run failed with an internal error", preserving
`provider_side_outcome_unknown` when `started_at` is set, and to persist it. The sibling
keeps **its own** cause (cancellation) rather than inheriting the run-level cause; the run
itself remains `FAILED` with `internal_error`. The same sweep is applied by the timeout and
operator-cancellation branches (relabelled `run_timed_out` and the HSF-4 cancellation type
respectively).

**Honest limit (N-4).** This guarantees that `cancelled_pending` never persists **in-process**;
it does **not** guarantee it can never be durable. A hard process kill (SIGKILL, power loss)
inside the short window between the stage handler's write and the outer terminalization can
leave `error_type = "cancelled_pending"` on disk. The reader therefore treats a durable
`cancelled_pending` as **interrupted / uncertain** — never as a normal outcome, never as
success, never auto-retried — and the design does not claim crash-immunity it does not have.
No startup repair rewrites it; it is preserved and displayed truthfully.

Rationale for engine-side rather than desktop-owned: the terminal record must be written by
the process that owns the run, before the event loop tears down; a desktop wrapper that
"drives to terminal before close" would put campaign lifecycle authority in the UI and still
fail on non-close cancellation.

The exact terminal **vocabulary** is HSF-4 (add `RunState.CANCELLED`, or reuse
`FAILED` + `error_type="cancelled"`). Either branch is inside the fence.

### 5.3 Shutdown sequence

`bootstrap/desktop.py` close driver, in order:

1. stop the campaign poller (no new projections);
2. if a campaign task is active, prompt the operator with the exact consequence
   ("in-flight requests may still incur provider charges whose outcome is unknown");
3. on confirm: cancel the hosting task and `await` it; the engine branch in §5.2 writes the
   terminal record;
4. verify from disk that the run is terminal; if the terminal write failed, display the run
   as `interrupted / uncertain` — never fabricate;
5. continue the existing close order (application, workspace, authority last).

An OS-level kill or power loss may still leave `running`; that remains truthfully displayed
as interrupted, never resumable/succeeded (CONTRACT §Lifecycle truth; F-11).

`New Job` while work is active prompts; it never cancels silently and never deletes
evidence. Cancellation remains a separate explicit operator action.

## 6. Path discipline

`manifest.output.runs_dir` resolves relative to the **job file**; CLI `--runs-dir` resolves
relative to the **current working directory** (`paths.py:33-54`;
`docs/OPERATING_PROCEDURE_V1.md:121-127`). A GUI process has a different cwd. The bridge
therefore:

- resolves and stores absolute paths for the loaded job and run directory;
- never relies on process cwd for campaign artifacts;
- displays the exact absolute run directory it is projecting.

## 7. What the desktop must not add

- no campaign event bus;
- no SQLite campaign table / second cost truth;
- no campaign authoring/editing surface;
- no automatic retry/resume/reuse;
- no provider streaming merely to animate progress;
- no daemon/remote client;
- no behaviour change to Phase 9 surfaces.
