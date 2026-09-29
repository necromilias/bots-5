# IMPL_FALSIFICATION_ENGINE — Phase 10 Adversarial Implementation Review

STATUS: complete

## A. Seal verification

**VERIFIED — PASS.**

- `PHASE10_CANDIDATE_SEAL_v1.json` on-disk sha256 = `260f5a9bee2b6433f4ea21134d2f0af8d29c3542e33c9ae49f1366c484c3b18e` — matches the required seal hash exactly, and matches the sidecar `PHASE10_CANDIDATE_SEAL_v1.sha256`.
- All 17 listed file digests recomputed from the worktree (`sha256sum` / Python `hashlib`) — **0 mismatches**; declared `file_count: 17` equals the number of listed paths.
- Sealed `mutation_fence_sha256` `602e6d5d…07cf452` equals the live digest of `design/MUTATION_FENCE.json`.
- Sealed `head_commit` `0756904481ae884bb9e864e8e1e11fc4a27a72ff` equals live `git rev-parse HEAD`.
- Seal claims `tracked_modified_in_fence` (9 src files), `tracked_modified_out_of_fence: []`, `fence_violations: []`; cross-checked against git in area A1 below.
- Related seals referenced by the candidate: design_seal_v4 `9739e87c…ed9c1`, parcel-v2 manifest `45fd91f3…4391b` (recorded, not independently re-derived here).

## B. Findings

### F-A1-1 — INFO — Worktree zero-diff outside fence holds for all protected paths
- Evidence: see area A1 in section C. `git diff HEAD --name-only` yields exactly the 9 fenced `modify` files; no tracked file outside the fence is modified or deleted (`git ls-files -d` empty); `git diff --cached --stat` empty (no staged mutation).
- Inside-fence: yes (compliance check, not a defect).

### F-A1-2 — LOW — Stray untracked artifact at repo root outside the fence
- Evidence: `AUDIT-2026-09-28-evidence-only-review.md` is untracked and not in the fence `add` list. Its mtime is **2026-09-28 11:43 (+1000)**, i.e. before this campaign's parcel-v2/seal window (seal created 2026-09-29), and its content is an unrelated "evidence-only blind review" of the upstream repos → pre-existing workspace debris, not a Phase 10 candidate mutation. Untracked scratch dirs `.audit-tmp4/`, `.falsify/`, `.verifyrun/`, `.verifyrun2/`, `work/` are likewise test/probe output, not tracked code.
- Minimal reproduction: `git status --porcelain | grep '^??'`.
- Impact: does not affect sealed digests or runtime behavior; should be moved under `work/` or gitignored so future fence audits have a clean untracked signal.
- Inside-fence: no (outside-fence observation).

### F-A2-1 — MEDIUM — Full-run approval accepts a mismatched stage/attempt target
- Evidence: `src/bots5/runner.py::_require_approval_binding` checks `approval.scope == "full_run"` and compares only `approval.target.run_id` to the executing `run_id` (lines 267–277). Unlike `_require_operation_snapshot_binding` for regeneration/rerun (lines 1443–1462), it never requires full-run `stage_id is None` and `attempt_number is None`. Approval target metadata is not part of `PreflightSnapshot.compute_digest()`, so changing those fields while retaining the digest is accepted. Dispatch still uses the job's declared stages; the defect is a consent/audit binding mismatch rather than an unintended alternate stage dispatch.
- Minimal reproduction: prepare a valid full-run snapshot and approval for run `R`; replace only `approval.target` with `{"run_id": R, "stage_id": "some-other-stage", "attempt_number": 99}`; call `run_job(job, providers, run_id=R, snapshot=snapshot, approval=approval)`. Expected: `ApprovalInvalidatedError` for wrong operation target before any run tree/provider call. Actual: the binding assertion passes, approval is consumed and normal full-run dispatch can proceed.
- Inside-fence: yes (`runner.py`).

### F-A2-2 — HIGH — Paid approval omits Mick-adjudicated pricing evidence
- Evidence: Mick's `parcel-v2/ADJUDICATION.md` §14 requires for any paid approval operator-supplied currently advertised input/output rates, source, observation time, route and conservative upper-bound basis (HSF-1 Branch A). Yet `ApprovalRecord` has no pricing field (`src/bots5/models.py:399–429`), `CampaignBridge._full_run_summary` states pricing evidence is none (`core/campaign.py:825–830`), and `runner._preflight_document` writes `pricing_evidence: None` while incorrectly claiming the fork is not adjudicated (`runner.py:386–398, 421–438`). The ordinary approval path still dispatches paid providers without those required details or any bound.
- Minimal reproduction: load a normal paid-provider job, call `prepare_full_run(actor)`, then approve and start it without any rate/source/time/bound inputs. Expected: refuse paid approval until Branch A evidence and conservative bound are supplied and bound to the approval. Actual: summary explicitly says no pricing evidence, approval is constructed, and a provider request proceeds.
- Inside-fence: yes (`models.py`, `runner.py`, `core/campaign.py`).

### F-A3-1 — HIGH — Cancelling a regeneration persists hard-kill-only `cancelled_pending`
- Evidence: `src/bots5/runner.py::regenerate_worker` and `rerun_synthesis` call `_execute_stage` but have no outer cancellation terminalizer. `_execute_stage` writes `state=failed,error_type=cancelled_pending` then re-raises `CancelledError` (lines 654–672). `CampaignBridge.cancel()` treats any attempt not still `running` as terminal and returns; it does not relabel the attempt (`core/campaign.py:1371–1384, 1386–1404`). Thus an ordinary handled operator cancellation is left looking exactly like the design's exceptional hard-kill window.
- Minimal reproduction: start and complete a v2 full run; prepare worker regeneration for `w1`; use a delayed route-faithful provider; approve regeneration, wait until dispatch, then call `await bridge.cancel()`. Expected (O-3/D-7): durable sibling attempt `w1.att2` has `failure.type == "cancelled"`, with unknown provider outcome preserved; the original run state stays succeeded. Actual: confirmed with the prescribed Python 3.14 harness, parent run remains `succeeded` but `w1.att2.failure.type == "cancelled_pending"` despite cancellation returning normally. The same uncaught path exists for synthesis rerun.
- Inside-fence: yes (`runner.py`, `core/campaign.py`).

### F-A4-1 — MEDIUM — A completed worker's usage remains stale until the whole run ends
- Evidence: `src/bots5/runner.py::_execute_stage` persists a completed stage record but does not persist v2 usage at attempt completion; `run_job` persists usage only before dispatch and after the entire pipeline/terminalization. This contradicts `CAMPAIGN_EVIDENCE_EVOLUTION.md` §4 write ordering (per-attempt + cumulative usage on attempt completion). `core/campaign.py::project_run` derives selected spend from `usage.json.per_attempt`, so an active/cut-short run can expose the earlier stale/unknown accounting as selected/cumulative spend. Its separate `live_cost` correctly reads stage metadata, which limits—but does not remove—the stale summary.
- Minimal reproduction: start a v2 run with two workers, where `w1` completes immediately at known cost `0.01` and `w2` is delayed; after `w1.att1` is persisted but before the run ends, inspect `usage.json` and `project_run`. Confirmed output: stage metadata `cost_usd="0.01"`, `usage.per_attempt["w1.att1"].cost_usd == null`, projected `selected_spend.cost_usd_known_sum == "0"`, while `live_cost.known_subtotal_usd == "0.01"`. Expected: completed-attempt usage/cumulative evidence updated as soon as that attempt completes; the derived selected spend should not omit known completed spend. If killed in this window, stale usage summaries remain durable because no startup reconciliation occurs.
- Inside-fence: yes (`runner.py`, `usage.py`, `core/campaign.py`).

### F-A5-1 — MEDIUM — Incomplete per-dependency provenance is mislabeled STALE
- Evidence: `src/bots5/storage.py::_synthesis_freshness` (lines 933–951) treats provenance as present if both values are dictionaries and validates individual value shapes only. It does not require each declared dependency key to appear in both maps. The later predicate treats an absent `recorded_attempt` as a selection mismatch and returns STALE, without an integrity warning. A missing dependency binding is unverifiable evidence, not evidence of a known stale result.
- Minimal reproduction: construct v2 evidence with declared synthesis dependency `w1`, selected synthesis state `succeeded`, and `consumed_dependencies={}`, `dependency_digests={}`. Expected: `UNVERIFIABLE` with an integrity warning because required provenance for `w1` is absent. Actual: confirmed `STALE`, `integrity_warning=False`, `warnings=[]`. (The same occurs with either map missing only the declared dependency entry.)
- Inside-fence: yes (`storage.py`).

## C. Per-area verdict

### A1 FENCE AND ZERO-DIFF — SOUND
From git only:
- `git status --porcelain`: 9 tracked modifications — `bootstrap/desktop.py, cli.py, desktop/window.py, errors.py, events.py, models.py, runner.py, storage.py, usage.py` — exactly matching the seal's `tracked_modified_in_fence`, with `tracked_modified_out_of_fence: []`.
- `git diff --stat`: same 9 files, +3289/−77. `git diff --cached --stat`: empty. `git ls-files -d`: no deletions.
- Protected-path proof (per-file blob comparison `git hash-object <file>` vs `git rev-parse HEAD:<path>`): **IDENTICAL to HEAD** for `src/bots5/core/application.py`, `src/bots5/paths.py`, `src/bots5/manifest.py`, every file under `src/bots5/providers/**` (`base.py`, `openai_compatible.py`, `openrouter.py`, `discovery.py`, `__init__.py`), and `pyproject.toml`. `git diff HEAD --stat` over `evidence examples db src/bots5/core/{application,execution,import_queue}.py src/bots5/providers pyproject.toml manifest.py paths.py` returns nothing; `git status --porcelain -- evidence` is clean.
- Cross-check vs `design/MUTATION_FENCE.json`: every modified/untracked code+test path is on the fence allowlist. One untracked exception exists — finding F-A1-2 above, dated before the campaign window.
- The two new untracked modules (`core/campaign.py`, `desktop/campaign_dock.py`) and six new test files match the fence `add` list and their digests match the seal (section A).

### A2 APPROVAL BINDING AND TOCTOU — DEFECTIVE
- Replayed approvals are refused before run-tree creation; for a simultaneous same-run full-run race, `create_run_tree` is exclusive and only its winner reaches `consume_approval`; marker creation is `O_CREAT|O_EXCL`, before `preflight.json` and all dispatch. Thus the stated settlement preserves no-run-dir/no-provider-on-replay and one winner for same target. Regeneration/rerun similarly consume before exclusive attempt claim/dispatch. Inputs/contracts are verified once and frozen messages are dispatched without post-approval rereads; rerun also binds selected dependency attempt+bytes. Confirmed exceptions: `run_job` accepts a full-run approval whose `target.stage_id` / `target.attempt_number` are non-null or wrong because only `target.run_id` is checked (F-A2-1); and paid execution proceeds with no Mick-adjudicated HSF-1 Branch A pricing evidence or upper-bound (F-A2-2). No concurrent double-dispatch counterexample found beyond the target-validation defect.

### A3 CANCELLATION TRUTH — DEFECTIVE
- Full `run_job` cancellation paths preserve the required distinctions: overall `TimeoutError` relabels neutral markers `run_timed_out` and returns TIMED_OUT; explicit cancellation labels affected stages `cancelled` and run CANCELLED; `_best_effort_internal_failure` turns sibling `cancelled_pending` into `cancelled` while run stays FAILED/internal_error. `reconstruct_run_state` does not treat cancelled_pending as success, and projection marks a started failed stage uncertain; no retry path exists. But normal cancellation of a regeneration/synthesis-rerun leaves durable `cancelled_pending` due to no operation-level relabeler (F-A3-1), contrary to O-3/D-7 and the hard-kill-only meaning.

### A4 COST HONESTY — DEFECTIVE
- `project_run` does not trust the stored selected-spend cache: with `per_attempt` present it derives selected spend and warns on mismatched cache; live cost comes from selected stage metadata, with known subtotal and explicit unknown IDs (no token-dollar accrual). However, full-run usage is not refreshed when each attempt completes, so `per_attempt` and cumulative/selected summaries can stay stale while the run is active or after a crash; confirmed counterexample F-A4-1 shows selected spend `0` despite completed `w1` cost `0.01` (live cost remains truthful).

### A5 FRESHNESS ORDER — DEFECTIVE
- For the specified O-1 path, `storage._synthesis_freshness` checks `state == "skipped"` and returns NOT_APPLICABLE before version/provenance/staleness checks; cross-cutting tests explicitly cover skipped synthesis, legacy v1, missing digest map and malformed digest. Legacy dispatched synthesis reads LEGACY_UNVERIFIED, and wholly absent/malformed provenance reads UNVERIFIABLE. However, a dispatched record with both provenance maps present but missing a declared dependency key is classified STALE without integrity warning (F-A5-1); incomplete provenance is not mechanically distinguishable from a known selection mismatch as implemented.

## D. Requirements with no test

The tests exercise the main happy/refusal paths and the suite is reported as passing (1479 passed, 1 skipped), but the following specific design cases are not covered by the inspected Phase 10 tests:

- Full-run approval target shape when `run_id` is correct but `stage_id` / `attempt_number` are wrong or non-null; existing wrong-target test changes only `run_id` (`test_phase10_desktop_preflight.py::test_approval_refused_when_target_does_not_match_the_operation`).
- Actual concurrent-process one-shot contention / crash boundary around run-tree creation then exclusive approval consumption. Replay is tested sequentially; concurrency settlement was reasoned from `mkdir` and `O_EXCL` behavior, not covered by a test.
- HSF-1 Branch A paid approval: required operator rates, route, source, observation time, fixed upper-bound calculation, and refusal if absent. The implementation currently has no pricing-input surface or pricing evidence field.
- Operator cancellation during worker regeneration or synthesis rerun; full-run cancellation is tested, but these operation paths leave `cancelled_pending` under ordinary cancellation (F-A3-1).
- Per-attempt cost/usage update immediately after an individual full-run stage completes, including abrupt process termination before the entire pipeline finishes; this leaves selected/cumulative usage stale (F-A4-1).
- Synthesis provenance maps that are dictionaries but omit one or more declared dependency keys (including both empty maps); existing tests cover wholly missing/malformed map cases, not partial key coverage (F-A5-1).
- Stale/malformed `selected_spend` cache cases with all cache fields varied independently; the exercised selection-change test covers a normal stale cache, not the full integrity-validation matrix.

## E. Overall verdict and the single most important thing not to miss

**NO — this candidate does not honestly satisfy the sealed design.** Confirmed defects include: paid approval without Mick-adjudicated HSF-1 pricing evidence and conservative bound (F-A2-2); incomplete full-run target binding (F-A2-1); ordinary cancellation of regeneration/rerun persisted as the hard-kill-only `cancelled_pending` marker (F-A3-1); completed full-run attempt usage not written until the entire pipeline ends (F-A4-1); and incomplete per-dependency synthesis provenance mislabeled STALE rather than UNVERIFIABLE (F-A5-1).

**The single most important thing not to miss:** the implementation proceeds with paid provider work despite the explicit human adjudication requiring operator-cited current rates and a conservative upper bound. The implementation's own preflight says pricing is absent because the fork was “not adjudicated,” but Mick did adjudicate HSF-1 as Branch A; a click-through approval therefore does not satisfy the approved spend-control design.
