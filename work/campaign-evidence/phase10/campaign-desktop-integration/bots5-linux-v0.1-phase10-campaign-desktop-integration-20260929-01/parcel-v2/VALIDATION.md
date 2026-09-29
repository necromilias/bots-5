# Phase 10 validation constitution

## General

Validation follows v0.3 impact tiers. Test selection must be based on changed contracts,
not vibes. Every selected pytest gate records non-zero collected count and exit status.

Automated product tests must not contact OpenRouter, local external endpoints, or any
provider service unless Mick separately authorizes a specific canary. Use deterministic
fake/stub providers.

Do not rerun a passing suite solely to scrape a count.

## T0 — exact behavior

The accepted design must include direct tests for:

- load job and parse/display exact job identity;
- zero-spend validation: no provider request, no run directory;
- invalid job/reference handling;
- preflight state and exact approval binding;
- changed job/input/prompt/model/limits after preflight invalidates or refuses approval;
- no provider request before explicit approval;
- live state transition projection for workers and synthesis;
- known/unknown/partial cost truth;
- success, failure, timeout, skipped synthesis and partial result inspection;
- expandable persisted worker/synthesis output;
- New Job changes UI working context without deleting evidence;
- worker regeneration preserves prior attempt and creates a sibling;
- explicit model change is recorded without silently changing provider route;
- regeneration is never an automatic retry;
- dependent synthesis becomes mechanically stale;
- explicit synthesis rerun creates a new attempt and preserves old synthesis;
- old/current attempt selection is reconstructable from filesystem evidence;
- crash/restart/old-run viewing does not invent resume/success;
- desktop shutdown with active campaign does not silently lose or retry uncertain provider work.

## T1 — subsystem

Campaign-engine models/runner/storage/events plus Phase 10 desktop/controller tests.

## T2 — adjacent

At minimum inspect and select where applicable:

- existing CLI validate/run/status/inspect compatibility;
- schema-v1/v2 manifest compatibility;
- provider mapping and unknown-cost semantics;
- run-directory path safety;
- historical run-directory readers;
- desktop session/window shutdown;
- existing Phase 9 desktop surfaces where shared widgets/lifecycle are touched.

## T3 — cross-cutting

Only when evidence justifies it. Likely cross-cutting areas are:

- filesystem evidence durability/reconstruction;
- asyncio/concurrency/shutdown;
- provider-side outcome uncertainty;
- desktop/core authority boundaries.

Migration/database authority should not be dragged into T3 merely because it exists.
If the accepted design adds durable desktop selection state to the app DB, migration
becomes affected and must then be tested honestly.

## T4 — complete repository

Required exactly because Phase 10 is a Linux v0.1 milestone boundary and the campaign
engine is shared substrate.

Default rule: **one final T4 on the exact final sealed candidate**.

Do not run an early/full T4 after every repair. Use focused T0–T3 gates. A broader
intermediate run is justified only by concrete cross-cutting evidence and must not be
misrepresented as the final gate.

The final T4 must be serial where the repository/environment requires serial execution.
Record collection count and exact exit result.

If candidate bytes change after final T4, the final T4 is superseded.

## Independent review sequence

### Design
1. integrated design frozen and sealed;
2. fresh GPT-6 Luna falsification;
3. repair/reseal as needed;
4. fresh MiMo final design oracle;
5. STOP FOR MICK.

### Implementation
1. focused milestone validation;
2. freeze exact implementation candidate;
3. materialize candidate manifest+seal **before** independent review;
4. fresh Qwen3.8 broad implementation falsification;
5. targeted MiniMax lifecycle review if material;
6. targeted Step proof-sufficiency review if material;
7. classify findings, repair, focused revalidate, reseal;
8. run final T4 on exact final seal;
9. verify seal unchanged;
10. optional Jamba final reviewer only if justified, max one;
11. fresh MiMo final implementation oracle against exact seal + final validation;
12. reconcile evidence and stop at pre-commit boundary.

Any review performed against unsealed product bytes is historical evidence only and
does not satisfy the post-seal gate.

---

## Parcel-v2 implementation-phase additions

Carried by Mick adjudication (`parcel-v2/NORMALIZED_CORRECTIONS.md`). These refine task
wording and test obligations only; no design artifact was edited and no scope was added.

- **O-1 ordering.** A terminal synthesis attempt that was never dispatched is classified
  **NOT_APPLICABLE before** the staleness predicate is evaluated. Required selector:
  `test_phase10_evidence_regeneration.py::test_skipped_synthesis_provenance_absence_is_not_reported_as_unverifiable_or_stale`.
- **O-2 sibling cancellation proof.** Validation must explicitly prove, from durable
  artifacts, that a sibling stage cancelled during a generic run failure persists
  `error_type == "cancelled"` while the run remains `FAILED` with `internal_error`.
  Required selector:
  `test_phase10_desktop_lifecycle.py::test_cancelled_sibling_keeps_cancelled_cause_while_run_remains_failed_internal_error`.
- **O-3 stage label.** Stage-level cancellation label is exactly `error_type = "cancelled"`
  under both run-level vocabularies; HSF-4/4a adds run-level `RunState.CANCELLED` only.
- **O-4 naming.** The new serialized field name is `api_key_source`; `key_source` is
  superseded in new serialized artifacts.

### Implementation review sequence (unchanged, with the reserved oracle)

1. focused milestone validation (T0–T3);
2. freeze the exact implementation candidate;
3. materialize the candidate manifest + seal **before** independent review;
4. fresh Qwen3.8 broad implementation falsification;
5. targeted MiniMax lifecycle review if material;
6. targeted Step proof-sufficiency review if material;
7. classify findings, repair inside the fence, focused revalidate, reseal;
8. run the one final complete-repository T4 (serial) on the exact final seal;
9. verify the seal is unchanged after T4 — if any product byte changed, that T4 is
   superseded;
10. fresh MiMo final implementation oracle against the exact seal + final validation
    evidence (reserved fourth launch; may not be spent on anything else);
11. reconcile evidence and stop at the pre-commit boundary.

Any review performed against unsealed product bytes is historical evidence only and does not
satisfy the post-seal gate.
