Recomputed design seal identity: `7ecbb502bd65dcc2ad1595b1dd709db3eff0bc5d5e079b00f8e77c29ee6d952f` — **PASS: seal integrity verified.**

# Fresh independent Phase 10 design falsification

## Review identity and evidence boundary

- Model: `openai/gpt-6-luna`.
- Requested effort: `max_if_exposed_else_highest_available`; per-child effort is not exposed by this harness, so **effective effort: not exposed** (no silent substitution).
- Design seal: recomputed `DESIGN_SEAL_v1.json` SHA-256 matches the expected identity and the adjacent `.sha256` value. Recomputed each of the 12 design-artifact hashes and 7 specialist-report hashes, including byte counts: **19/19 match; no missing or mismatched entry**.
- Baseline: live `HEAD` is `0756904481ae884bb9e864e8e1e11fc4a27a72ff`; live tree is `7fe879035a01a87339dfe6319a435fa0e84c94bd`, both matching the stated pins. `git status` showed untracked workspace material, but no tracked delta; this read-only review did not modify it.
- Review method: exact sealed design, parcel authority, selected specialist/integration evidence, and live pinned source/tests were read. No provider/API calls and no tests were run. Test results and behavior of unimplemented Phase 10 code are therefore **not claimed**.

## Gate result

**Design gate blocked.** Seal integrity passes, but the sealed design has multiple repairable approval, lifecycle, evidence, and proof defects, plus one unresolved human pricing fork whose listed fallback conflicts with the pinned operating procedure. Repair the design evidence and reseal before the next design gate; the human fork must be adjudicated separately by Mick. This is a design review result, not evidence that any Phase 10 implementation exists or that a test failed.

## Findings

### F-01 — Approval for synthesis rerun does not bind the selected dependency bytes or run context

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** Critical — approval can describe different work from the request that executes.  
**Mechanical repair:** Yes.

**Evidence:** `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md` §2, lines 50–70, enumerates snapshot fields and says dispatch uses the frozen `system_messages` and `worker_user_message`. That schema has no run identity, selected dependency attempts/digests, or explicit frozen synthesis message. Its §4, lines 94–100, compares the job configuration to the snapshot but does not bind synthesis inputs. `design/REGENERATION_AND_STALE_SYNTHESIS.md` §5, lines 138–151, authorizes after approval to resolve the *then-current* selected dependency attempts and build the synthesis message. By contrast §3, lines 72–81, acknowledges that those output bytes define what synthesis consumes. The live runner builds synthesis input from current in-memory worker outputs (`src/bots5/runner.py:404-415`); v2 changes this to selected attempt files but the approval snapshot still has no corresponding input field.

**Invariant:** Contract §“Preflight / approval integrity” (`parcel/CONTRACT.md:49-60`) requires approval to bind the exact consequential work, including dependencies, and forbids prior approval silently applying after those change. An operator can approve a rerun snapshot, change worker selection (or choose a different run with the same job), and then have the post-approval resolver dispatch different output bytes under the same declared `synthesis_rerun` scope/digest. Recording dependency digests after resolution is useful provenance but does not retroactively bind consent.

**Repair:** Define an operation-specific snapshot for regeneration/rerun. Bind at minimum `run_id`, operation/stage, requested model, and for synthesis rerun each selected dependency attempt plus its output SHA-256 and the exact rendered synthesis message. Dispatch only the frozen approved message; refuse if selection or bytes differ. Bind the intended target run/attempt before approval as well. Add direct changed-selection/changed-output invalidation tests.

### F-02 — An approval record is not made one-shot; the same approval can authorize another operation

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** Critical — conflicts with the explicit no-reuse/no-silent-repeat rule.  
**Mechanical repair:** Yes.

**Evidence:** `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md` §3–4, lines 72–108, specifies only digest equality and operation scope; the record has no unique operation nonce, consumed state, or run/attempt binding. §6, lines 129–138, says there is “no remembered consent” and that each regeneration/rerun requires a new approval, while §1, lines 46–48, calls approval in-memory and one-shot only as a UI property. The proposed runner entry points accept an `ApprovalRecord` and can be called repeatedly with the same object. Live `runner.run_job` is repeat-callable and only takes a job/provider mapping/run id (`src/bots5/runner.py:253-271`); there is currently no engine-side consent-consumption seam.

**Invariant:** Main design §0.6 (`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:32-33`) says prior approval never silently authorizes a later provider request; F-13 is expressly identified at `parcel/FINDINGS.md:77-80`. A cleared modal is not an engine guarantee. Repeating `start_run`, `regenerate_worker`, or `rerun_synthesis` with a still-valid digest/scope must not be an unapproved second request.

**Repair:** Make approval bound to one operation instance and consume it engine-side exactly once, including replay attempts through non-UI callers. Define when consumption occurs and fail closed on reuse. Ensure a consumed approval cannot be reused for another run, attempt, stage, or later invocation. Add replay tests for all three operation types.

### F-03 — The approved provider route is not tied to the provider object that dispatches

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** High — the digest can match while the actual endpoint/provider mapping differs.  
**Mechanical repair:** Yes.

**Evidence:** `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md:56-58` snapshots route `base_url` and API-key environment-variable *name*; §4, lines 96–100, requires disk/config consistency but names no check of the actual `providers` mapping passed to the runner. In the pinned source, `run_job` accepts a separate `providers: Mapping[str, Provider]` and validates only that entries exist and expose `complete` (`src/bots5/runner.py:231-250, 253-260`). The actual HTTP endpoint is held by the provider instance (`src/bots5/providers/openrouter.py:47-63, 217-220`; `src/bots5/providers/openai_compatible.py:48-79, 238-241`).

**Invariant:** Contract §“Preflight / approval integrity” (`parcel/CONTRACT.md:51-60`) includes provider selection/route among the changes that invalidate approval. Hashing the manifest route while passing a different provider instance does not bind the bytes/configuration that execute. The bridge’s provider construction is not specified sufficiently to prove that this cannot happen.

**Repair:** Define a single engine-side provider-resolution path from the frozen route snapshot, or validate each concrete provider instance’s route/configuration against it immediately before any call. Do not let the UI pass an independently mutable provider map whose route is outside the approved digest. Add a swapped-provider-object test; no change to provider semantics is needed.

### F-04 — Provider errors can be terminalized as certain failure although dispatch outcome is unknown

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** High — can fabricate “did not run” certainty and mislead retry decisions.  
**Mechanical repair:** Yes.

**Evidence:** `design/DESKTOP_SURFACE_AND_LIFECYCLE.md` D-4, lines 111–114, infers uncertainty only when a durable stage is still `running` with a `started_at`. The design does not specify uncertainty handling for already-terminal failed stages. In the live runner, `ProviderError` is caught at `src/bots5/runner.py:169-179`, marks the stage failed, and leaves `provider_side_outcome_unknown` false. Both HTTP adapters turn transport errors into `ProviderError` after attempting the request (`src/bots5/providers/openrouter.py:208-227`; `src/bots5/providers/openai_compatible.py:229-248`). A transport failure can occur after a provider accepted work. The design’s new cancellation rule only sets uncertainty for cancellation; it does not repair this ordinary error branch.

**Invariant:** Main design §0.7 (`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:34-36`) says a stage that may have reached a provider is `unknown`, never “did nothing”; `parcel/FINDINGS.md` F-09 (`:55-59`) forbids claiming provider stop or retry certainty after uncertain acceptance.

**Repair:** Specify which provider error classes prove rejection versus leave acceptance uncertain. Persist/infer `provider_side_outcome_unknown` for ambiguous transport/response failures and project a failed-but-uncertain outcome distinctly. Never auto-retry it. Add deterministic fake-provider tests for a transport failure after request dispatch and for any explicitly known pre-dispatch rejection.

### F-05 — Manual task cancellation is recorded as a run timeout at stage level

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** High — operator cancellation is given a false cause, even if run-level terminalization succeeds.  
**Mechanical repair:** Yes.

**Evidence:** The proposed engine handler in `design/DESKTOP_SURFACE_AND_LIFECYCLE.md:145-155` cancels/awaits outstanding worker tasks, then sweeps nonterminal records. But the existing `_execute_stage` cancellation handler has already changed a cancelled record to `FAILED`, `error_type="run_timed_out"` before it re-raises (`src/bots5/runner.py:201-215`). Once awaited, that record is terminal and is not covered by the proposed “sweep every non-terminal record” step. The handler therefore preserves a timeout label for a user-requested close/cancel, even where the run itself gets a cancellation terminal state.

**Invariant:** Lifecycle truth requires a truthful typed outcome and explicit uncertainty, not conflation of timeout and user cancellation (`parcel/CONTRACT.md:76-87`; `DESKTOP_SURFACE_AND_LIFECYCLE.md:102-120`).

**Repair:** Carry cancellation cause explicitly into the stage task or have engine terminalization reclassify records cancelled by the outer user-cancellation path to a cancellation-specific stage error while retaining `provider_side_outcome_unknown=True` when `started_at` exists. Preserve timeout labels for actual timeout. Test both paths and their persisted stage/run records.

### F-06 — Missing provenance on a v2 synthesis is presented as “current”

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** High — malformed/partial v2 evidence can be represented as fresh.  
**Mechanical repair:** Yes.

**Evidence:** `design/REGENERATION_AND_STALE_SYNTHESIS.md` §4.2, lines 118–123, says *any* synthesis attempt without `consumed_dependencies` is displayed as “current (provenance not recorded — pre-v2 evidence).” That includes a v2 selected synthesis attempt with missing/corrupt provenance, not merely legacy v1 evidence. The design elsewhere says v2 attempts record provenance (`CAMPAIGN_EVIDENCE_EVOLUTION.md:103-114`) and malformed v2 directories fail closed (`:48-62`), but §4.2’s unconditional rule defeats that distinction.

**Invariant:** A deterministic mechanical predicate must not claim freshness without the dependency relation it compares. Avoiding a fabricated stale result for legacy v1 evidence does not authorize fabricating a current result for malformed v2 evidence.

**Repair:** Use three outcomes: fresh, stale, and unverifiable/integrity-warning. Treat absent provenance as legacy-unverified only for evidence version 1; fail closed or report v2 provenance absence as unverifiable. Keep mismatch or missing selected-output bytes stale with an integrity warning as §4.3 already intends. Add tests for v1 missing provenance, v2 missing provenance, malformed maps, missing bytes, digest mismatch, and forward/backward selection changes.

### F-07 — Explicit selection has no specified update to `selected_spend`/legacy aggregate

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** Medium-high — cost gate and displayed selected cost can diverge from the authoritative selection.  
**Mechanical repair:** Yes.

**Evidence:** `design/REGENERATION_AND_STALE_SYNTHESIS.md:58-68` says regeneration leaves selection unchanged and a separate “Make current” action changes it. `design/DESKTOP_SURFACE_AND_LIFECYCLE.md:37-42` exposes that selection write through `CampaignBridge.make_current`. Yet `design/CAMPAIGN_EVIDENCE_EVOLUTION.md:116-144` defines `selected_spend` as the currently selected attempt per stage and requires legacy `aggregate` to mirror it; neither the selection operation nor the selection-change transition specifies recomputing/persisting those summaries. `rerun_synthesis` has a similar success-time selection change (`REGENERATION_AND_STALE_SYNTHESIS.md:148-155`).

**Invariant:** The selected attempt, selected cost, and synthesis cost gate must describe the same active pipeline; cumulative spend must still include all executed attempts.

**Repair:** Make selection mutation update `usage.json`’s selected-stage map/selected summary and legacy aggregate, or explicitly make those values derived at read time from attempt records plus `selection.json`. Specify crash ordering/reconstruction so a selection write cannot leave a permanently contradictory summary. Do not modify cumulative accounting.

### F-08 — Some named gates permit behavior the design says is forbidden, and do not prove the mechanical predicate

**Classification:** `REPAIRABLE_DESIGN_DEFECT`  
**Severity:** Medium-high — a claimed gate can pass without proving required lifecycle/staleness behavior.  
**Mechanical repair:** Yes.

**Evidence:** `design/VALIDATION_PLAN.md:71` selects `test_desktop_cancellation_persists_aborted_or_running_with_truthful_uncertainty`. Allowing `running` as an acceptable desktop-initiated cancellation conflicts with D-7 at `DESKTOP_SURFACE_AND_LIFECYCLE.md:117-120`, which says cancellation must produce a terminal durable record. A crash test separately and correctly permits the durable `running` case at validation-plan `:71`. T0.5 at `VALIDATION_PLAN.md:69-70` names one “becomes stale after regeneration” test but does not require bidirectional reselection, legacy-v1 versus malformed-v2 missing provenance, missing output bytes, or digest-mismatch behavior. T0.2 at `:67` names job/input/model/limits changes but does not explicitly select coverage for changed prompt bytes, provider route, dependency selection, or output target despite the invalidation list in `PREFLIGHT_APPROVAL_STATE_MACHINE.md:129-137`.

**Invariant:** Every T0 obligation must be directly provable; a test that accepts a forbidden nonterminal user-cancelled state is not such proof. The design’s non-vacuity rules (`VALIDATION_PLAN.md:7-19`) prevent zero-collection selectors but do not by themselves ensure the assertions cover the obligation.

**Repair:** Make the desktop-cancel selector require a terminal durable state and keep the crash-running assertion in its separate test. Add exact direct selectors/cases for bidirectional staleness and all missing/corrupt provenance cases, and for every named approval invalidator (including rerun selection/output). Record collected counts at execution as already required. These are planned future tests; none were run here.

### F-09 — Pricing fallback D is not an equivalent Phase 10-compliant branch under OPv1

**Classification:** `HUMAN_SEMANTIC_FORK`  
**Severity:** High / blocking human adjudication for paid preflight.  
**Mechanical repair:** No; Mick must choose policy/authority. Documentation can be corrected mechanically after that choice.

**Evidence:** `design/HUMAN_SEMANTIC_FORK_REGISTER.md:31-37` recommends operator-entered rates but presents “unknown + approval” as an acceptable always-available fallback; `PREFLIGHT_APPROVAL_STATE_MACHINE.md:110-127` calls both A and D adopted branches. Pinned `docs/OPERATING_PROCEDURE_V1.md:62-75` requires immediately before a paid run: record the highest applicable currently advertised input/output pricing for each selected model/provider route and calculate a conservative expected upper bound from prompt size, stage count, and configured output ceilings. Branch D explicitly supplies no dollar bound. Branch A’s listed record shape (`:115-118`) does not require the advertised-rate source/observation, highest-route basis, or a defined conservative input-token calculation.

**Invariant:** Do not invent a pricing authority, but do not silently relax the applicable paid-preflight obligation either. The fact that no in-repository pricing registry exists is the reason to stop for a product/authority choice, not proof that “unknown” satisfies OPv1.

**Options for Mick:** (A) authorize operator-entered *currently advertised highest applicable rates*, with recorded source/time/route and a specified conservative prompt/output bound; or (D) explicitly waive/supersede the OPv1 dollar-bound requirement for this workflow and authorize unknown-cost approval. Static catalogue/live lookup remain outside the present accepted fence/zero-network guarantee unless separately adjudicated. Do not implement paid approval UI until this fork is decided. HSF-1 is correctly identified as a fork, but its options must not present D as compliant without the explicit waiver.

## Requested attack-surface disposition

| Attack surface | Result | Evidence / note |
|---|---|---|
| Flat attempt grammar, `.`/`-` stage IDs, containment | **PASS** | Live `src/bots5/paths.py:10-18` allows dots/hyphens; `storage.load_stage_view` resolves output and requires its parent equal resolved `stages` (`src/bots5/storage.py:188-202`). The design constructs filenames from a declared ID plus a complete `.att<N>` suffix and resolves selected attempts from declared IDs/selection rather than suffix scanning (`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:88-115`; `CAMPAIGN_EVIDENCE_EVOLUTION.md:48-62`). Thus `w1` attempt 1 (`w1.att1.*`) and declared `w1.att2` attempt 1 (`w1.att2.att1.*`) are distinct; flat files preserve the existing parent predicate. Implementation must retain exact basename construction and fail-closed selection validation.
| Desktop as second engine / competing truth | **PASS at design boundary** | `DESKTOP_SURFACE_AND_LIFECYCLE.md:8-23, 89-100` names filesystem evidence authoritative and rules out event bus/SQLite shadow state; `CampaignDock` renders projection and issues commands (`:50-71`). Implementation must keep the bridge as an engine seam and not put persistence/arithmetic into widgets.
| Candidate-seal governance | **PASS** | `VALIDATION_PLAN.md:139-150` requires candidate manifest+seal before review, final T4 against the exact seal, post-T4 byte verification, and treats unsealed review as historical only. It correctly states no tests/gates have run yet (`:152-155`).
| Mutation fence scope | **PASS, subject to F-01–F-08 repairs remaining inside it** | `MUTATION_FENCE.json:19-141` has 9 modify + 8 add paths; includes `src/bots5/errors.py` for the required typed refusal (`:21-24`), and keeps `core/application.py`, providers, paths, manifest, Phase 9 surfaces, dependencies, and historical evidence out (`:143-165`). No demonstrated missing product path or forbidden scope expansion for the identified repairs: runner/storage/errors and the already-fenced tests suffice. Any later need to expand still requires successor authority.
| Backward compatibility for V0/V0.2 directories | **PASS as a design mechanism; proof remains future evidence** | `CAMPAIGN_EVIDENCE_EVOLUTION.md:8-19, 161-175, 190-208` selects v1 when the marker is absent and specifies the legacy `stages/<id>.json` reader path unchanged; `IMPLEMENTATION_SEQUENCE.md:7-21` makes golden fixtures a pre-storage-mutation gate. No test was run and no implementation exists, so this is not a claim that real historical samples already passed. The fixture gate should use actual historical shapes or byte-exact fixtures, not merely assume compatibility.
| Five declared human forks | **PASS as exposed choices, with F-09 caveat for HSF-1** | HSF-1 pricing authority, HSF-2 restart/current-result persistence, HSF-3 run discovery, HSF-4 terminal cancellation vocabulary, and HSF-5 provider switching are each separately listed with options/consequences in `HUMAN_SEMANTIC_FORK_REGISTER.md:16-138`. HSF-2 correctly flags the possible conflict between “current until New Job” and restart reset (`:44-67`); it is not resolved until Mick decides. No additional UX fork was found that the design silently settles: attempt layout is a justified engineering choice, and explicit-vs-auto selection is clearly treated as a nonblocking UX choice (`SPECIALIST_DISAGREEMENTS.md:24-55, 69-80`). F-09 narrows which HSF-1 branch may be called compliant.
| Shutdown/crash/partial matrix | **FAIL pending F-04, F-05, F-08** | The core intent is truthful (never resume, infer running uncertainty, verify terminal after close), but ordinary ambiguous provider failures and cancellation’s stage-level timeout label are not covered by the proposed rules/tests. OS kill/power loss remaining `running` is correctly called interrupted/uncertain (`DESKTOP_SURFACE_AND_LIFECYCLE.md:165-179`).

## Overall classification counts

- `PASS`: 5 attack-surface dispositions (attempt grammar/containment; no second truth; seal governance; fence scope; old-run reader mechanism); plus the declared-forks register as exposed, subject to F-09.
- `REPAIRABLE_DESIGN_DEFECT`: **8** (F-01 through F-08).
- `HUMAN_SEMANTIC_FORK`: **1** (F-09; HSF-1 pricing authority/OPv1 compatibility).
- Seal-integrity mismatches: **0**.

These counts are findings/dispositions, not test failures. The gate remains blocked until the repairable defects are repaired and resealed, and the paid-preflight pricing fork is adjudicated by Mick.
