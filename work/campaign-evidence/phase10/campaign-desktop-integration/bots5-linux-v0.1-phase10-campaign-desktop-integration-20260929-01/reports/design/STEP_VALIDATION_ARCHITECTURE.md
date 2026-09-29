# Phase 10 T0–T4 Validation Architecture

## Role and scope

This document is the Phase 10 proof-sufficiency architecture for the design-only
parcel-v1. It defines the test-surface plan, gate semantics, collection-count
discipline, serial/concurrency constraints, and candidate-seal governance. It
does not prescribe implementation; it constrains validation so that, after Mick
accepts the design and grants implementation authority, the implementation
campaign cannot repeat the vacuous-selector failure seen in a prior campaign
(a supposedly required `-k/marker` selector that collected zero tests).

Authoritative sources consulted for this plan:
- `parcel/CONTRACT.md` (locked obligations and exclusions)
- `parcel/SCOPE.json`, `parcel/BASELINE.json`, `parcel/FINDINGS.md`,
  `parcel/VALIDATION.md`, `parcel/SOURCES.md`
- `MODEL_ROUTING_EVIDENCE.md`
- `reports/design/QWEN_CAMPAIGN_ARCHAEOLOGY.md` (the authoritative
  reconstruction of the live engine)
- The pinned source tree at `0756904481ae884bb9e864e8e1e11fc4a27a72ff`

## 0. Anti-vacuity rules (non-negotiable)

1. **No empty marker assumptions.** Every pytest gate must use a selector that
   demonstrably collects **>0** tests at the moment the gate is run. A selector
   is empty if `--collect-only -q` returns zero lines.
2. **No full-suite-as-default at every milestone.** T0–T3 use focused
   changed-contract selection. T4 is the only gate that runs the full
   repository test surface, and it runs exactly once on the exact final sealed
   candidate.
3. **Serial when specified.** When a gate is declared `serial`, only one pytest
   process may run. Parallel pytest processes are forbidden for that gate.
4. **Counts are authoritative.** Every gate records the exact collected count,
   exact exit code, and exact selector. A passing suite is not rerun solely to
   scrape a count.
5. **Fake providers only.** Automated product tests must not contact OpenRouter,
   local external endpoints, or any provider service. Deterministic
   `FakeProvider` / `FakeStreamingBackend` / `ControlledBackend` are the
   standard surfaces. Socket-blocking fixture (`tests/conftest.py:9-18`) is
   autouse for ordinary tests.

## 1. Existing test inventory (ground truth for collection claims)

### 1.1 Campaign-engine tests (zero-provider, Qt-free)

| File | Line-range evidence | What it proves today |
|---|---|---|
| `tests/test_runner.py` | `:49-62` | worker + synthesis success persists to `stages/*.md` and `result.md` |
| `tests/test_runner.py` | `:127-137` | completion metadata survives disk reconstruction (`load_run_view` + `load_stage_view`) |
| `tests/test_runner.py` | `:140-177` | incomplete/empty output persists metadata and fails run |
| `tests/test_runner.py` | `:205-217` | invalid contract ⇒ no provider call, no run directory |
| `tests/test_runner.py` | `:243-282` | worker failure preserves sibling, blocks synthesis with typed reason |
| `tests/test_runner.py` | `:284-301` | known-cost threshold blocks synthesis before provider call |
| `tests/test_runner.py` | `:303-319` | unknown/partial cost aggregation |
| `tests/test_runner.py` | `:321-342` | per-stage timeout and overall timeout persist terminal state |
| `tests/test_runner.py` | `:344-371` | workers actually overlap (concurrency) |
| `tests/test_runner.py` | `:373-507` | v2 routing, secret non-persistence, missing mapping fails before run creation |
| `tests/test_cli_views.py` | `:14-23` | `validate` makes no API call and creates no run dir |
| `tests/test_cli_views.py` | `:25-29` | missing API key ⇒ exit 1, no run dir |
| `tests/test_cli_views.py` | `:31-43` | run summary exposes incomplete completion |
| `tests/test_cli_views.py` | `:59-84` | local-only CLI does not require OpenRouter key |
| `tests/test_cli_views.py` | `:86-103` | status/inspect from disk only (same-process round-trip) |
| `tests/test_manifest.py` | full file | v1/v2 acceptance, closed objects, unsafe stage id, missing files |
| `tests/test_storage_events.py` | `:9-22` | run-id uniqueness and JSONL validity |
| `tests/test_provider.py` | full file | OpenRouter normalization, finish-reason mapping |
| `tests/test_local_provider.py` | full file | local non-streaming request shape, unknown cost stays unknown |
| `tests/test_audit_blockers.py` | `:35-54` | reread failure creates no run tree |
| `tests/conftest.py` | `:9-18` | autouse socket block + `OPENROUTER_API_KEY` deletion |
| `tests/helpers.py` | `:31-116` | `FakeProvider`, `make_job_tree` (schema v1), `worker_contract` |

**Baseline count (Qwen archaeology, §11):** 115 passed in 0.48s across the
selection `test_storage_events.py test_manifest.py test_runner.py
test_cli_views.py test_audit_blockers.py test_provider.py test_local_provider.py
test_prompts.py test_rendering.py`.

### 1.2 Desktop/lifecycle tests (Qt-dependent, used for T2/T3 neighbors)

| File | Line-range evidence | Reusable precedent |
|---|---|---|
| `tests/test_desktop_draft1.py` | `:203-243` | close/reopen durable state |
| `tests/test_desktop_draft1.py` | `:374` | busy state blocks navigation; stop persists abort |
| `tests/test_phase4.py` | `:453` | restart reconciles several interrupted attempts |
| `tests/test_phase4.py` | `:645-707` | qasync runtime close survives waiter cancellation |
| `tests/test_phase9_desktop_slice_e.py` | `:1030` | window close during import drains cutoff past work on GUI thread |
| `tests/test_phase9_desktop_slice_e.py` | `:2676` | declined active generation close prompt aborts handoff without child thread |
| `tests/test_phase9_desktop_slice_e.py` | `:3009` | failed runtime close drops restore request and launches no child |
| `tests/test_phase9_desktop_slice_e.py` | `:3266` | cancelled restore handoff awaiter keeps request pending thread |
| `tests/test_phase2_core.py` | `:192` | restart reconciles a persisted running generation |
| `tests/test_phase1_core.py` | `:84` | application close closes subscribers before cancelling generation |
| `tests/test_phase1_core.py` | `:116` | immediate application close terminalizes a scheduled generation |

## 2. Gate grammar

Every gate below states:

- **What it proves**
- **Collection mechanism** — exact selector (or new test path) and why it
  collects **>0** today, or a note that a new test is required
- **Serial / concurrency constraint**
- **Broader-validation trigger**

## 3. T0 — exact behavior gates (design contract)

T0 is focused on the new seams Phase 10 introduces. These tests must be new
tests added under `tests/` (or `tests/test_phase10_*.py`) because the desktop
currently has **zero** campaign surface (Qwen archaeology §0, confirmed by grep
over `src/bots5/desktop`, `src/bots5/core`, `src/bots5/bootstrap`).

### 3.1 T0.1 Zero-spend validation and no run-directory creation

**What it proves:** Loading a campaign job for desktop preview does not contact
any provider and does not create a run directory.

**Collection mechanism:**
- New test file `tests/test_phase10_desktop_preflight.py`
- New function `test_desktop_load_job_preview_creates_no_run_dir_and_no_provider_call`
- This is a **new test** (does not exist today). It will be added during
  implementation and is included here because zero-spend validation is a locked
  obligation (CONTRACT.md §2, VALIDATION.md T0).

**Why the new test will collect >0:** It is a function-level selector targeting
`test_phase10_desktop_preflight.py::test_desktop_load_job_preview_creates_no_run_dir_and_no_provider_call`.
Once the file exists, `pytest -k
test_desktop_load_job_preview_creates_no_run_dir_and_no_provider_call` collects
exactly one test. Before the file exists, this selector is empty; the gate must
not be run until the implementation adds the file.

**Serial/concurrency:** Serial. No pytest parallelism.

**Broader-validation trigger:** If the desktop preview path introduces any new
filesystem polling or background task, T1.1 must also run.

### 3.2 T0.2 Exact preflight and approval binding

**What it proves:** Approval is bound to the exact job bytes, model/provider
selection, token ceilings, execution limits, dependencies, and output target
that will execute. Changing any of these after preflight invalidates the prior
approval.

**Collection mechanism:**
- New test file `tests/test_phase10_desktop_preflight.py`
- New function `test_preflight_approval_binding_invalidates_after_job_or_input_or_model_or_limits_change`
- New function `test_preflight_approval_binding_does_not_silently_apply_to_swapped_input_bytes`

**Why the new tests will collect >0:** Exact function-name selectors. The test
file will be created during implementation.

**Serial/concurrency:** Serial. Approval-state tests are sequential state
machines.

**Broader-validation trigger:** If the accepted design introduces an engine-side
immutable execution snapshot (digest pin), T1.1 must cover the new snapshot
writer/reader.

**HUMAN_SEMANTIC_FORK:** If the engine cannot produce an immutable execution
snapshot without a product choice, classify this as `HUMAN_SEMANTIC_FORK`
per CONTRACT.md §Preflight/approval integrity and FINDINGS F-03.

### 3.3 T0.3 Fake-provider spend gate

**What it proves:** No provider request is issued before explicit operator
approval, and the fake-provider test surface proves the gate without any live
network.

**Collection mechanism:**
- Reuse existing `tests/test_runner.py::test_invalid_contract_fails_before_provider_call_and_run_directory`
  (`:205-217`) — proves engine-side zero-spend before provider call.
- Reuse `tests/test_cli_views.py::test_validate_makes_no_api_call_or_run_dir`
  (`:14-23`) — proves CLI validate path.
- New test `tests/test_phase10_desktop_preflight.py::test_desktop_requires_explicit_approval_before_any_provider_construction`
  — proves desktop seam does not construct a provider before approval.

**Why these collect >0 today:** The two existing tests already pass at baseline
(115-pass run). The new test is added during implementation and will be
selectable by exact function name.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If the desktop introduces a background preflight
task, T3.1 (asyncio/shutdown) becomes relevant.

### 3.4 T0.4 Attempt preservation and sibling regeneration

**What it proves:** Regenerating a selected worker preserves the original
attempt, creates a sibling attempt, permits explicit model change, and does not
silently change provider route.

**Collection mechanism:**
- New test file `tests/test_phase10_evidence_regeneration.py`
- New function `test_worker_regeneration_preserves_original_attempt_and_creates_sibling`
- New function `test_worker_regeneration_explicit_model_change_does_not_silently_change_provider_route`
- New function `test_regeneration_is_explicit_not_automatic_retry`

**Why the new tests will collect >0:** Exact function-name selectors.

**Serial/concurrency:** Serial. Attempt identity and sibling layout are
sequential assertions.

**Broader-validation trigger:** If the storage layer gains append-only attempt
semantics, T1.2 (storage) and T2.1 (CLI compatibility) become affected.

**HUMAN_SEMANTIC_FORK:** Provider change on regeneration is not explicitly
granted by product authority (FINDINGS F-04). If the operator wants provider
change, present explicit options rather than guessing.

### 3.5 T0.5 Stale synthesis and explicit rerun

**What it proves:** After selecting a regenerated worker sibling, dependent
synthesis becomes mechanically stale. Explicit synthesis rerun creates a new
synthesis attempt and preserves old synthesis evidence.

**Collection mechanism:**
- New test `tests/test_phase10_evidence_regeneration.py::test_dependent_synthesis_becomes_stale_after_worker_regeneration`
- New test `tests/test_phase10_evidence_regeneration.py::test_explicit_synthesis_rerun_creates_new_attempt_and_preserves_old`

**Why the new tests will collect >0:** Exact function-name selectors.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If the event vocabulary is extended (closed set
in `events.py:13-25`), T1.3 (events) must cover the new event types.

### 3.6 T0.6 Crash/partial display truth

**What it proves:** A crash or desktop-initiated cancellation that leaves a
durable run claiming `running` does not silently appear as resumable or
succeeded. The UI displays truthful partial/uncertain state.

**Collection mechanism:**
- New test `tests/test_phase10_desktop_lifecycle.py::test_crash_leave_running_durable_state_does_not_appear_as_succeeded_or_resumable`
- New test `tests/test_phase10_desktop_lifecycle.py::test_desktop_cancellation_persists_aborted_or_running_with_truthful_uncertainty`

**Why the new tests will collect >0:** Exact function-name selectors.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If the engine is changed to treat
`CancelledError` as a terminal-writing path (see §7.3 of archaeology), T1.4
(runner lifecycle) and T3.2 (asyncio/shutdown) must run.

**BLOCKED/Risk:** The archaeology found that `runner.run_job` does not treat
`CancelledError` as a terminal-writing path (`runner.py:465` catches
`Exception`, not `BaseException`; `:201` re-raises). Desktop-initiated
cancellation would leave `run.json` saying `running`. This is a concrete
code-grounded defect that must be resolved before implementation.

### 3.7 T0.7 Live projection (known + unknown cost truth)

**What it proves:** Live progress projection shows known subtotal + explicit
unknown set truthfully. It does not fabricate per-token dollar accrual.

**Collection mechanism:**
- Reuse `tests/test_runner.py::test_unknown_partial_cost_aggregation`
  (`:303-319`) — proves engine-level unknown/partial cost.
- Reuse `tests/test_runner.py::test_incomplete_empty_output_persists_metadata_and_fails_run`
  (`:140-177`) — proves partial metadata.
- New test `tests/test_phase10_desktop_projection.py::test_live_projection_shows_known_subtotal_and_explicit_unknown_set`

**Why these collect >0 today:** The two existing tests already pass. The new
test is added during implementation.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If the desktop introduces a polling loop for
projection, T3.2 (asyncio/shutdown) must verify the poller stops truthfully.

### 3.8 T0.8 Shutdown with active campaign

**What it proves:** Desktop shutdown while a campaign is active does not
silently lose or retry uncertain provider work. The durable state remains
truthful.

**Collection mechanism:**
- New test `tests/test_phase10_desktop_lifecycle.py::test_active_campaign_shutdown_persists_truthful_state_without_retry`
- Reuse `tests/test_phase9_desktop_slice_e.py::test_window_close_during_import_drains_cutoff_past_work_on_gui_thread`
  (`:1030`) — proves GUI-thread drain precedent.
- Reuse `tests/test_phase1_core.py::test_application_close_closes_subscribers_before_cancelling_generation`
  (`:84`) — proves close ordering precedent.

**Why these collect >0 today:** The two desktop tests already exist and pass.
The new test is added during implementation.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If the desktop drives `run_job` to terminal
persist before close, T1.4 (runner lifecycle) must cover the new cancellation
path.

### 3.9 T0.9 Headless CLI preservation

**What it proves:** The existing `bots5` CLI verbs (`validate`, `run`,
`status`, `inspect`) remain usable without the desktop, and old run directories
remain inspectable.

**Collection mechanism:**
- Reuse `tests/test_cli_views.py::test_validate_makes_no_api_call_or_run_dir`
  (`:14-23`)
- Reuse `tests/test_cli_views.py::test_status_and_inspect_from_disk_only`
  (`:86-103`)
- Reuse `tests/test_runner.py::test_missing_provider_mapping_fails_before_run_creation`
  (`:439-445`)
- Reuse `tests/test_runner.py::test_generic_single_provider_runner_form_is_removed`
  (`:448-454`)

**Why these collect >0 today:** All four tests pass at baseline.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If the evidence schema is extended, T2.2
(old-run compatibility) becomes mandatory.

### 3.10 T0.10 New Job presentation state

**What it proves:** `New Job` changes UI working context without deleting
historical run evidence.

**Collection mechanism:**
- New test `tests/test_phase10_desktop_lifecycle.py::test_new_job_clears_ui_working_context_without_deleting_historical_run_evidence`

**Why the new test will collect >0:** Exact function-name selector.

**Serial/concurrency:** Serial.

## 4. T1 — subsystem gates

T1 covers the subsystems directly touched by the accepted design. All T1 tests
must be new or modified tests; existing tests remain as regression anchors.

### 4.1 T1.1 Desktop/controller seam

**What it proves:** The desktop surface operates the existing campaign engine
through explicit seams without replacing it.

**Collection mechanism:**
- New file `tests/test_phase10_desktop_preflight.py`
- New file `tests/test_phase10_desktop_projection.py`
- New file `tests/test_phase10_desktop_lifecycle.py`

**Serial/concurrency:** Serial. Controller tests are sequential state machines.

**Broader-validation trigger:** If the desktop adds a new bus or event source,
T3.4 (desktop/core authority boundaries) must run.

### 4.2 T1.2 Storage / evidence evolution

**What it proves:** Append-only attempt semantics preserve old evidence, old
run-directory readability, and make current attempt selection mechanically
reconstructable.

**Collection mechanism:**
- New file `tests/test_phase10_evidence_regeneration.py`
- New function `test_storage_append_only_attempt_layout_preserves_old_evidence`
- New function `test_load_run_view_returns_correct_current_attempt_for_each_stage`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If `load_stage_view` containment check is
changed (currently rejects nested paths at `storage.py:196-198`), T2.1 (CLI
compatibility) must run because `cli._cmd_inspect` uses the same reader.

### 4.3 T1.3 Events

**What it proves:** New regeneration/staleness events are append-only and
versioned. The closed event vocabulary is extended deliberately.

**Collection mechanism:**
- New function `test_events_regeneration_and_staleness_are_appended_without_rewriting_history`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If a new events reader is introduced, T2.1
(CLI compatibility) must cover it.

### 4.4 T1.4 Runner lifecycle / cancellation

**What it proves:** Cancellation (desktop-initiated or timeout) writes a
terminal durable state. The runner does not silently leave `running` as
resumable/succeeded.

**Collection mechanism:**
- New function `test_runner_cancellation_writes_terminal_persisted_state`
- Reuse `tests/test_runner.py::test_overall_timeout_persists_terminal_state`
  (`:334-342`) — regression anchor.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** If engine-side terminal-on-cancel is
implemented, T3.2 (asyncio/shutdown) must run.

## 5. T2 — adjacent subsystem gates

T2 is run when evidence shows cross-cutting impact to adjacent systems. It is
**not** run by default.

### 5.1 T2.1 CLI compatibility

**What it proves:** Existing CLI verbs (`validate`, `run`, `status`, `inspect`)
remain usable, and old run directories (V0/V0.2 schema) remain readable.

**Collection mechanism:**
- Reuse `tests/test_cli_views.py` (all 5 functions) — exact selector:
  `pytest tests/test_cli_views.py -k "validate_makes_no_api_call_or_run_dir or missing_api_key_makes_no_run_dir or run_summary_exposes_incomplete_completion or local_only_cli_does_not_require_openrouter_key or status_and_inspect_from_disk_only"`
  — **collects 5 tests today** (verified by Qwen archaeology, file total 103
  lines).
- Reuse `tests/test_runner.py::test_completion_metadata_survives_disk_reconstruction`
  (`:127-137`) — proves round-trip within one process.
- New test `tests/test_phase10_backward_compat.py::test_old_run_directory_v0_schema_remains_inspectable` —
  reads a golden fixture run directory produced by a prior revision.

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Always run when storage schema changes. Skip if
evidence is purely additive and no reader is modified.

**Critical gap:** Old-run compatibility currently has **no automated test**
(Qwen archaeology §11). The golden-fixture test above is mandatory before
implementation proceeds.

### 5.2 T2.2 Provider mapping and unknown-cost semantics

**What it proves:** Provider mapping and unknown-cost behavior are unchanged.

**Collection mechanism:**
- Reuse `tests/test_runner.py::test_runner_routes_local_only_job_to_local_mapping`
  (`:402-409`)
- Reuse `tests/test_runner.py::test_runner_routes_mixed_job_by_declared_provider`
  (`:412-436`)
- Reuse `tests/test_runner.py::test_unknown_partial_cost_aggregation`
  (`:303-319`)
- Reuse `tests/test_local_provider.py` (full file or selected functions)

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Run if provider routing or cost normalization
code is touched.

### 5.3 T2.3 Desktop session/window shutdown (Phase 9 shared surfaces)

**What it proves:** Shared desktop lifecycle surfaces (session, window,
workspace state) are not regressed.

**Collection mechanism:**
- Reuse `tests/test_desktop_draft1.py::test_draft1_qasync_boundary_preserves_durable_state_across_close_reopen`
  (`:203-243`)
- Reuse `tests/test_phase4.py::test_a5_qasync_runtime_close_survives_waiter_cancellation_and_closes_once`
  (`:645-707`)
- Reuse `tests/test_phase9_desktop_slice_e.py::test_window_close_during_import_drains_cutoff_past_work_on_gui_thread`
  (`:1030`)

**Serial/concurrency:** Serial (Qt tests require single-process execution).

**Broader-validation trigger:** Run if `window.py`, `session.py`, or
`bootstrap/desktop.py` are modified.

### 5.4 T2.4 Historical run-directory readers

**What it proves:** Golden V0/V0.2 run directories remain readable by the
current engine and CLI.

**Collection mechanism:**
- New test `tests/test_phase10_backward_compat.py::test_golden_v0_run_directory_loads_through_load_run_view`
- New test `tests/test_phase10_backward_compat.py::test_golden_v0_stage_directory_loads_through_load_stage_view`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Mandatory on any storage schema change.

## 6. T3 — cross-cutting gates

T3 is run **only when evidence justifies it** (VALIDATION.md §T3). It is not a
routine milestone gate.

### 6.1 T3.1 Filesystem evidence durability/reconstruction

**What it proves:** Append-only sibling evidence is durable across process
interruption and reconstructable from filesystem artifacts alone.

**Collection mechanism:**
- New test `tests/test_phase10_cross_cutting.py::test_sibling_evidence_survives_mid_run_process_kill`
- New test `tests/test_phase10_cross_cutting.py::test_evidence_reconstruction_from_filesystem_artifacts_alone`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Run if evidence model changes or if crash-test
evidence shows non-terminal state leakage.

### 6.2 T3.2 Asyncio / concurrency / shutdown

**What it proves:** Active campaign tasks participate truthfully in application
shutdown without silent retry or data loss.

**Collection mechanism:**
- Reuse `tests/test_phase9_desktop_slice_e.py::test_declined_active_generation_close_prompt_aborts_handoff_without_child_thread`
  (`:2676`)
- Reuse `tests/test_phase9_desktop_slice_e.py::test_failed_runtime_close_drops_restore_request_and_launches_no_child`
  (`:3009`)
- New test `tests/test_phase10_cross_cutting.py::test_desktop_hosted_campaign_task_cancellation_writes_terminal_state`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Run if the desktop integrates campaign execution
into its event loop.

### 6.3 T3.3 Provider-side outcome uncertainty

**What it proves:** Uncertain external acceptance is displayed truthfully and
never silently retried.

**Collection mechanism:**
- Reuse `tests/test_runner.py::test_worker_failure_preserves_sibling_and_blocks_synthesis`
  (`:243-256`) — `provider_side_outcome_unknown` is persisted.
- Reuse `tests/test_runner.py::test_per_stage_request_timeout`
  (`:321-332`) — timeout persists failed state.
- New test `tests/test_phase10_cross_cutting.py::test_uncertain_provider_outcome_is_never_auto_retried`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Run if provider integration or cancellation
semantics change.

### 6.4 T3.4 Desktop/core authority boundaries

**What it proves:** The desktop observes/operates the existing engine through
explicit seams without becoming a second campaign engine or creating a
competing SQLite truth.

**Collection mechanism:**
- New test `tests/test_phase10_cross_cutting.py::test_desktop_does_not_create_second_campaign_engine`
- New test `tests/test_phase10_cross_cutting.py::test_desktop_state_does_not_become_second_cost_truth`

**Serial/concurrency:** Serial.

**Broader-validation trigger:** Run if desktop introduces new persistence for
campaign selection state.

**HUMAN_SEMANTIC_FORK:** If the accepted design adds durable desktop selection
state to the app DB, migration authority becomes affected (VALIDATION.md:62-65)
and must be tested honestly.

## 7. T4 — complete repository final gate

### 7.1 Default rule

T4 is required exactly because Phase 10 is a Linux v0.1 milestone boundary and
the campaign engine is shared substrate (VALIDATION.md §T4). The default rule
is:

> **One final T4 on the exact final sealed candidate.**

T4 is **not** a routine repair loop. Focused T0–T3 gates are used during
repair. An intermediate full-suite run is justified only by concrete
cross-cutting evidence and must not be misrepresented as the final gate.

### 7.2 Collection mechanism

The final T4 runs the complete repository test surface. At baseline the
repository has **1409 tests** (Phase 9 final T4, `parcel/BASELINE.json:12-18`).
The exact count must be recorded at T4 time.

Selector: **full repository default** — no `-k` filter, no marker filter.
Command:
```bash
pytest
```
This collects all tests under `tests/`. It collects **>0** because the
repository currently contains 1409 tests.

### 7.3 Serial/concurrency constraint

T4 must run **serial** — one pytest process. The contract explicitly forbids
launching multiple pytest processes concurrently (CONTRACT.md §Anti-vacuity).

### 7.4 Candidate seal binding

The following rules are non-negotiable (CONTRACT.md §Implementation phase,
VALIDATION.md §Implementation, FINDINGS F-18):

1. **Materialize the implementation candidate seal before any independent
   implementation review.**
2. **Run one final T4 on the exact final sealed candidate.**
3. **Verify the candidate is byte-identical after final T4.** If any product
   bytes change after final T4, that T4 is superseded; reseal and rerun the
   applicable final validation.
4. The seal is a manifest+hash over the exact implementation candidate tree.
   It is written before independent review, not after.

### 7.5 Independent review sequence (implementation phase)

1. Focused milestone validation (T0–T3).
2. Freeze exact implementation candidate.
3. **Materialize candidate manifest+seal before independent review.**
4. Fresh Qwen3.8 broad implementation falsification.
5. Targeted MiniMax lifecycle review if material.
6. Targeted Step proof-sufficiency review if material.
7. Classify findings, repair, focused revalidate, reseal.
8. Run final T4 on exact final seal.
9. Verify seal unchanged after T4.
10. Optional Jamba final reviewer only if justified (max one).
11. Fresh MiMo final implementation oracle against exact seal + final validation.
12. Reconcile evidence and stop at pre-commit boundary.

Any review performed against unsealed product bytes is historical evidence only
and does not satisfy the post-seal gate.

## 8. New test surfaces required (summary)

The following new test files are required. None exist at baseline.

| New file | Purpose |
|---|---|
| `tests/test_phase10_desktop_preflight.py` | Zero-spend preview, approval binding, spend gate |
| `tests/test_phase10_desktop_projection.py` | Live projection truth (known + unknown) |
| `tests/test_phase10_desktop_lifecycle.py` | Crash/partial display, shutdown, New Job |
| `tests/test_phase10_evidence_regeneration.py` | Sibling attempts, stale synthesis, explicit rerun |
| `tests/test_phase10_backward_compat.py` | Old-run directory golden fixtures |
| `tests/test_phase10_cross_cutting.py` | Filesystem durability, asyncio shutdown, provider uncertainty, authority boundaries |

## 9. HUMAN_SEMANTIC_FORK register

These are genuine product-semantic choices not settled by current authority.
They must be presented to Mick, not guessed.

1. **Preflight pricing authority (F-06).** OPv1 requires conservative preflight
   pricing, but no campaign pricing registry/lookup exists. Options:
   - operator-entered pricing at preflight time
   - external provider pricing lookup (requires separate Mick authority for
     external paid product canary)
   - cached pricing metadata
   - unknown cost + approval (show conservative bound as unknown)
   - another path

   Source: archaeology §4.1, CONTRACT.md §Cost truth.

2. **Approval binding substrate (F-03, X-3).** The engine has no digest/hash of
   referenced bytes. Options:
   - engine-side immutable execution snapshot (requires engine mutation)
   - desktop-side digest + reverification at execution
   - operator-entered content pin
   - another mechanism

   Source: archaeology §3.3, CONTRACT.md §Preflight/approval integrity.

3. **Provider change on regeneration (F-04).** Product authority permits explicit
   model change. Provider change is not explicitly granted. Options:
   - preserve original provider route by default; explicit provider change is a
     separate operator action
   - treat provider change as equivalent to model change (requires authority)
   - another explicit fork

   Source: FINDINGS F-04, CONTRACT.md §Locked product obligations item 9.

4. **Durable result view across restart (F-15).** Architecture allows desktop
   state in app DB while campaign evidence stays filesystem-authoritative.
   Options:
   - desktop selection state is purely in-memory; restart reloads from
     filesystem evidence
   - desktop persists selected campaign/result in app DB (requires migration
     testing)
   - another explicit choice

   Source: FINDINGS F-15, VALIDATION.md:62-65.

5. **Run discovery seam (X-6).** `locate_run_dir` requires operator-supplied id.
   Options:
   - desktop accepts explicit path/id only
   - desktop browses a designated runs directory (introduces enumeration)
   - another explicit choice

   Source: archaeology §2.3, §8.

6. **Engine-side terminal-on-cancel (X-1).** `run_job` does not treat
   `CancelledError` as terminal. Options:
   - engine treats cancellation as terminal-writing path
   - desktop must drive `run_job` to terminal persist before allowing close
   - another explicit choice

   Source: archaeology §7.3, FINDINGS F-11, CONTRACT.md §Lifecycle truth.

## 10. Old-run compatibility gap (critical)

**Finding:** Old-run compatibility currently has **no automated test** (Qwen
archaeology §11).

The nearest existing proxies are:
- `tests/test_runner.py:127-137` (disk reconstruction within one process)
- `tests/test_cli_views.py:86-103` (status/inspect from disk within one process)
- Checked-in `evidence/**` trees (76 tracked files) — human-reviewed artifacts,
  not test inputs.

**Required action:** Add golden-fixture reader tests under
`tests/test_phase10_backward_compat.py` that load retained historical V0/V0.2
run directories and verify:
- `load_run_view` succeeds
- `load_stage_view` succeeds for each stage id
- `locate_run_dir` resolves correctly
- CLI `status` and `inspect` produce expected output shape

These tests must be added **before** any storage schema mutation.

## 11. Crash/partial display defect (critical)

**Defect:** `runner.run_job` outer exception handler (`runner.py:465`) catches
`Exception`, not `BaseException`. `asyncio.CancelledError` derives from
`BaseException` in Python ≥3.8. Desktop-driven cancellation would leave
`run.json` durably stating `state: "running"` with possibly-running stage
records.

**Impact:** This makes the F-11/CONTRACT "crash may leave a durable run that
says running" case reachable through normal UI shutdown, not only a crash.

**Required action:** The accepted design must either:
- change the engine to treat cancellation as a terminal-writing path, or
- require the desktop to drive `run_job` to terminal persist before allowing
  close.

Until this is resolved, T0.6 and T0.8 cannot be fully validated.

## 12. Top 5 defects / risks the supervisor must integrate

1. **Vacuous-selector anti-pattern.** A prior campaign used a supposedly
   required selector that collected zero tests. This architecture mandates
   collection-count verification before every gate.

2. **No automated old-run compatibility test.** V0/V0.2 run directories have
   zero regression harness. Add golden-fixture tests before any schema change.

3. **Cancellation does not write terminal state.** Desktop-initiated cancellation
   leaves `running` durable state. Resolve engine-side or UI-side before
   implementation.

4. **Approval has no digest substrate.** No hash/digest of referenced bytes
   exists. Approval binding requires an explicit product fork or engine mutation.

5. **Preflight pricing authority is absent.** OPv1 requires conservative
   preflight pricing, but no registry/lookup exists. This is a HUMAN_SEMANTIC_FORK
   that must be adjudicated by Mick.

## 13. Approval binding and candidate seal rules (final)

### 13.1 Approval binding

- Preflight must bind to the exact bytes/configuration that execute.
- If inputs, model/provider selection, token ceilings, execution limits,
  dependencies, or output target change after preflight, the prior approval is
  invalidated.
- The design must establish an immutable or mechanically reverified execution
  snapshot before the first provider request.
- If this cannot be done without a product choice, classify a
  HUMAN_SEMANTIC_FORK (CONTRACT.md §Preflight/approval integrity).

### 13.2 Candidate seal governance

- **Materialize the implementation candidate seal before any independent
  implementation review.**
- The seal covers the exact implementation candidate tree (file hashes).
- One final T4 runs against the exact final sealed candidate.
- After final T4, verify the candidate is byte-identical. If bytes change,
  reseal and rerun final validation.
- Any review against unsealed bytes is historical evidence only.

---

*Report path:* `reports/design/STEP_VALIDATION_ARCHITECTURE.md`
*Parcel:* `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01`
*Baseline HEAD:* `0756904481ae884bb9e864e8e1e11fc4a27a72ff`
*Model:* Step 3.7 Flash (validation/proof architecture role)
