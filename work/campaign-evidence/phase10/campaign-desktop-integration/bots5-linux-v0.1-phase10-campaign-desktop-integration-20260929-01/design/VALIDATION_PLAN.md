# Phase 10 validation plan (T0–T4)

Constitution: `parcel/VALIDATION.md`. Anti-vacuity: `parcel/CONTRACT.md` §"Anti-vacuity /
anti-loop rules".

This plan integrates Step 3.7 Flash's proof-sufficiency architecture and
verifies its selectors against the live tree.

> **Revision history.** v1 → v2: F-08 (terminal-cancellation assertion, bidirectional
> staleness, provenance cases, approval invalidators). v2 → v3: M-2 (initial-run FRESH→STALE
> and v1-refusal selectors), M-4 (`cancelled_pending` never durable), M-5 (headless consent),
> M-3 (provider-route-object swap). v3 → v4: N-3 (skipped-synthesis NOT_APPLICABLE) and N-4
> (durable `cancelled_pending` after hard kill reads as interrupted/uncertain). See
> `reports/design/DESIGN_REPAIR_RECORD_v3_to_v4.md`.

## 0. Non-negotiable gate rules

1. Every gate records: exact command, exact selector, **collected count**, exit code, and
   candidate/seal identity. A selector whose `--collect-only -q` yields zero lines is
   invalid and the gate must not be claimed.
2. New test files do not exist yet. A selector naming a new test is only valid **after**
   the implementation creates that test; until then it collects zero and must not be used.
3. Never rerun a passing suite merely to obtain its count.
4. Serial gates run one pytest process. No concurrent pytest invocations when serial is
   specified (T4 and Qt tests).
5. Provider-facing tests use deterministic fakes only (`tests/helpers.py` `FakeProvider`,
   `conftest.py` autouse socket block + `OPENROUTER_API_KEY` deletion). No OpenRouter /
   local-endpoint / provider contact.
6. T4 runs exactly once, on the exact final sealed candidate.

## 1. Verified existing selectors (non-vacuous today)

Every existing selector reused below was checked against the live tree: the named function
exists in the named file (22/22 verified).

- `tests/test_runner.py`: `test_completion_metadata_survives_disk_reconstruction`,
  `test_incomplete_empty_output_persists_metadata_and_fails_run`,
  `test_worker_failure_preserves_sibling_and_blocks_synthesis`,
  `test_unknown_partial_cost_aggregation`, `test_per_stage_request_timeout`,
  `test_overall_timeout_persists_terminal_state`,
  `test_invalid_contract_fails_before_provider_call_and_run_directory`,
  `test_missing_provider_mapping_fails_before_run_creation`,
  `test_generic_single_provider_runner_form_is_removed`,
  `test_runner_routes_local_only_job_to_local_mapping`,
  `test_runner_routes_mixed_job_by_declared_provider`
- `tests/test_cli_views.py`: `test_validate_makes_no_api_call_or_run_dir`,
  `test_missing_api_key_makes_no_run_dir`, `test_run_summary_exposes_incomplete_completion`,
  `test_local_only_cli_does_not_require_openrouter_key`,
  `test_status_and_inspect_from_disk_only`
- `tests/test_desktop_draft1.py`:
  `test_draft1_qasync_boundary_preserves_durable_state_across_close_reopen`
- `tests/test_phase4.py`:
  `test_a5_qasync_runtime_close_survives_waiter_cancellation_and_closes_once`
- `tests/test_phase9_desktop_slice_e.py`:
  `test_window_close_during_import_drains_cutoff_past_work_on_gui_thread`,
  `test_declined_active_generation_close_prompt_aborts_handoff_without_child_thread`,
  `test_failed_runtime_close_drops_restore_request_and_launches_no_child`
- `tests/test_phase1_core.py`:
  `test_application_close_closes_subscribers_before_cancelling_generation`

Baseline campaign-engine selection (115 passed, socket-blocked):
`tests/test_storage_events.py tests/test_manifest.py tests/test_runner.py
tests/test_cli_views.py tests/test_audit_blockers.py tests/test_provider.py
tests/test_local_provider.py tests/test_prompts.py tests/test_rendering.py`.

## 2. T0 — exact behavior gates (new + reused)

New test files: `test_phase10_desktop_preflight.py`, `test_phase10_desktop_projection.py`,
`test_phase10_desktop_lifecycle.py`, `test_phase10_evidence_regeneration.py`.
For each new gate, the selector is an exact `file::function`; validity begins only when the
implementation adds that function (rule 0.2).

| Gate | Proves | Selector (new unless marked reused) |
|---|---|---|
| T0.1 | load/preview creates no run dir, no provider call | `test_phase10_desktop_preflight.py::test_desktop_load_job_preview_creates_no_run_dir_and_no_provider_call` |
| T0.2 | approval binds to exact bytes/model/limits/prompt/route/deps/output target; each change invalidates | `test_phase10_desktop_preflight.py::test_preflight_approval_binding_invalidates_after_job_or_input_or_model_or_limits_change`; `::test_preflight_approval_binding_invalidates_after_prompt_contract_bytes_change`; `::test_preflight_approval_binding_invalidates_after_provider_route_change`; `::test_preflight_approval_binding_invalidates_after_dependency_selection_or_output_target_change`; `::test_preflight_approval_binding_does_not_silently_apply_to_swapped_input_bytes` |
| T0.3 | no provider before explicit approval; refused approval creates no run dir; approval is one-shot | new `test_phase10_desktop_preflight.py::test_desktop_requires_explicit_approval_before_any_provider_construction`; `::test_replayed_consumed_approval_makes_no_second_provider_request`; reused `test_runner.py::test_invalid_contract_fails_before_provider_call_and_run_directory` + `test_cli_views.py::test_validate_makes_no_api_call_or_run_dir` |
| T0.4 | regeneration preserves original, creates sibling, explicit model change, provider route unchanged, never automatic, v1 runs refused | `test_phase10_evidence_regeneration.py::test_worker_regeneration_preserves_original_attempt_and_creates_sibling`; `::test_worker_regeneration_explicit_model_change_does_not_silently_change_provider_route`; `::test_regeneration_is_explicit_not_automatic_retry`; `::test_worker_regeneration_attempt_number_is_bound_by_approval_and_never_renumbered`; `::test_regeneration_refused_for_evidence_version_1_run_writing_nothing`; `::test_provider_route_object_swap_is_refused_before_dispatch` |
| T0.5 | dependent synthesis mechanically stale (bidirectional); initial-run provenance; explicit rerun preserves evidence | `test_phase10_evidence_regeneration.py::test_initial_run_synthesis_is_fresh_and_becomes_stale_after_regeneration_and_reselection`; `::test_dependent_synthesis_becomes_stale_after_worker_regeneration`; `::test_staleness_predicate_is_bidirectional_across_reselection`; `::test_v1_missing_provenance_is_legacy_unverified_not_stale`; `::test_v2_missing_provenance_is_unverifiable_not_current`; `::test_stale_after_missing_selected_output_bytes_with_integrity_warning`; `::test_stale_after_dependency_digest_mismatch_with_integrity_warning`; `::test_explicit_synthesis_rerun_creates_new_attempt_and_preserves_old`; `::test_synthesis_rerun_refused_when_selection_or_bytes_changed_after_approval`; `::test_synthesis_rerun_refused_for_evidence_version_1_run_writing_nothing` |
| T0.6 | durable `running` after crash is not success/resumable; cancellation persists a **terminal** record; transitional labels never persist in-process | `test_phase10_desktop_lifecycle.py::test_crash_left_running_durable_state_does_not_appear_as_succeeded_or_resumable` (crash case, `running` acceptable and correctly labelled interrupted); `::test_desktop_cancellation_persists_terminal_durable_state_not_running` (requires a terminal record, never `running`); `::test_operator_cancellation_is_not_labelled_run_timeout`; `::test_cancelled_pending_is_reclassified_in_process_and_never_written_as_a_final_state`; `::test_durable_cancelled_pending_after_hard_kill_reads_as_interrupted_uncertain_and_is_not_retried` |
| T0.7 | live projection = known subtotal + explicit unknown set; no fabricated accrual; failure-uncertainty truth | new `test_phase10_desktop_projection.py::test_live_projection_shows_known_subtotal_and_explicit_unknown_set`; `::test_selected_cost_is_derived_from_selection_not_stale_cache`; `::test_ambiguous_transport_failure_is_unknown_and_definitive_rejection_is_not`; `::test_received_but_unusable_response_is_distinct_from_ambiguous_transport_failure` + reused `test_runner.py::test_unknown_partial_cost_aggregation`, `::test_incomplete_empty_output_persists_metadata_and_fails_run` |
| T0.11 | headless parity verbs require explicit consent and cannot spend by default | `test_phase10_desktop_preflight.py::test_headless_rerun_synthesis_without_approve_makes_no_provider_request`; `::test_headless_regenerate_with_approve_actor_constructs_bound_approval` |
| T0.8 | shutdown with active campaign persists truthful state, no retry | `test_phase10_desktop_lifecycle.py::test_active_campaign_shutdown_persists_truthful_state_without_retry` + reused Phase 9/Phase 1 close precedents |
| T0.9 | headless CLI preserved | reused `test_cli_views.py` (5) + `test_runner.py::test_missing_provider_mapping_fails_before_run_creation`, `::test_generic_single_provider_runner_form_is_removed` |
| T0.10 | New Job clears context, deletes nothing | `test_phase10_desktop_lifecycle.py::test_new_job_clears_ui_working_context_without_deleting_historical_run_evidence` |
| T0.12 | skipped / never-dispatched synthesis is reported by skip reason, never as an integrity failure | `test_phase10_evidence_regeneration.py::test_skipped_synthesis_provenance_absence_is_not_reported_as_unverifiable_or_stale` |

T0.6/T0.8 depend on the HSF-4 branch (cancellation vocabulary) being adjudicated; the
cancellation *mechanism* (engine-side terminalization) is fixed.

## 3. T1 — subsystem gates

| Gate | Proves | Selector |
|---|---|---|
| T1.1 | desktop operates engine through explicit seams | `test_phase10_desktop_preflight.py`, `test_phase10_desktop_projection.py` |
| T1.2 | append-only attempt semantics preserve evidence; selection reconstructable | `test_phase10_evidence_regeneration.py::test_storage_append_only_attempt_layout_preserves_old_evidence`; `::test_load_run_view_returns_correct_current_attempt_for_each_stage` |
| T1.3 | new events append without rewriting history | `test_phase10_evidence_regeneration.py::test_events_regeneration_and_staleness_are_appended_without_rewriting_history` + reused `tests/test_storage_events.py` |
| T1.4 | cancellation writes terminal persisted state | `test_phase10_desktop_lifecycle.py::test_runner_cancellation_writes_terminal_persisted_state` + reused `test_runner.py::test_overall_timeout_persists_terminal_state` |

Serial. A bridge introducing a poller/bus triggers T3.2/T3.4.

## 4. T2 — adjacent subsystem gates (not default; run on impact)

- **T2.1 CLI compatibility** — reused `tests/test_cli_views.py` (5) +
  `test_runner.py::test_completion_metadata_survives_disk_reconstruction` +
  `test_phase10_backward_compat.py::test_old_run_directory_v0_schema_remains_inspectable`.
  Always run when storage schema changes.
- **T2.2 provider mapping / unknown-cost** — reused
  `test_runner.py::test_runner_routes_local_only_job_to_local_mapping`,
  `::test_runner_routes_mixed_job_by_declared_provider`,
  `::test_unknown_partial_cost_aggregation`, `tests/test_local_provider.py`.
- **T2.3 Phase 9 shared desktop surfaces** — reused `test_desktop_draft1.py`,
  `test_phase4.py`, `test_phase9_desktop_slice_e.py` close precedents. Serial (Qt).
- **T2.4 historical run-directory readers** — `test_phase10_backward_compat.py::`
  `test_golden_v0_run_directory_loads_through_load_run_view`,
  `::test_golden_v0_stage_directory_loads_through_load_stage_view`. Mandatory on any storage
  schema change.

## 5. T3 — cross-cutting gates (evidence-triggered only)

- **T3.1 filesystem durability/reconstruction** — `test_phase10_cross_cutting.py::`
  `test_sibling_evidence_survives_mid_run_process_kill`,
  `::test_evidence_reconstruction_from_filesystem_artifacts_alone`.
- **T3.2 asyncio/concurrency/shutdown** — new
  `test_phase10_cross_cutting.py::test_desktop_hosted_campaign_task_cancellation_writes_terminal_state`
  + reused Phase 9 close precedents. Serial.
- **T3.3 provider-side outcome uncertainty** — new
  `test_phase10_cross_cutting.py::test_uncertain_provider_outcome_is_never_auto_retried`
  + reused `test_runner.py::test_worker_failure_preserves_sibling_and_blocks_synthesis`,
  `::test_per_stage_request_timeout`.
- **T3.4 desktop/core authority boundaries** — new
  `test_phase10_cross_cutting.py::test_desktop_does_not_create_second_campaign_engine`,
  `::test_desktop_state_does_not_become_second_cost_truth`.

If HSF-2 option 2b (app-DB persistence) were selected, migration becomes affected and must
be tested honestly — a further reason the design recommends option 2a.

## 6. T4 — complete repository

- Required exactly because Phase 10 is a Linux v0.1 milestone boundary and the campaign
  engine is shared substrate.
- Baseline reference: Phase 9 final T4 = 1409 tests, 0 failures, 0 errors, 1 skipped,
  exit 0 (`parcel/BASELINE.json`). The T4 count is recorded at T4 time; it is not predicted.
- Command: full repository default (`pytest`), **serial**, no `-k`/marker filter.
- Default rule: exactly one final T4 on the exact final sealed candidate. An intermediate
  full run is justified only by concrete cross-cutting evidence and must not be
  misrepresented as the final gate.
- If product bytes change after T4, that T4 is superseded.

## 7. Candidate seal binding

1. Materialize the implementation candidate manifest + seal **before** any independent
   implementation review.
2. Run the final T4 against the exact seal.
3. Verify byte-identical after T4.
4. Independent review sequence: Qwen3.8 broad falsification → targeted MiniMax lifecycle →
   targeted Step proof-sufficiency → classify/repair/focused-revalidate/reseal → final T4 →
   seal verify → optional Jamba (max one) → fresh MiMo final oracle → reconcile → pre-commit
   stop.

Any review against unsealed bytes is historical evidence only.

## 8. Design-phase note

This document is design evidence. Its gates are executable only after the Mick gate and
parcel-v2. No test file listed here exists yet; no gate has been run.
