# Normalized corrections O-1…O-4 (task-facing clarifications)

Authority: Mick adjudication (`parcel-v2/ADJUDICATION.md`), verbatim: *"Record the following
normalized corrections in the retained continuation and immutable parcel-v2 before any
implementation worker launches."*

These are **mechanical task-facing clarifications**. They do not constitute a new design
round, do not expand scope, and grant no authority to alter unrelated semantics. **Design
seal v4 was not mutated.** Where a clarification refines wording that oracle v4 flagged, the
sealed design artifacts remain the design of record and these statements govern
implementation and test wording.

---

## O-1 — Non-dispatched terminal synthesis is NOT_APPLICABLE

**Ordering rule:** a terminal synthesis attempt that was **never dispatched** is classified
**NOT_APPLICABLE before the staleness predicate is evaluated**. The staleness predicate
(FRESH / STALE / UNVERIFIABLE / LEGACY_UNVERIFIED) applies only to synthesis attempts that
were actually dispatched. Outcomes covered by NOT_APPLICABLE include `StageState.SKIPPED`
with `synthesis_skipped_reason` in {dependency failed, dependency incomplete, known-cost
threshold exceeded, not reached before run timeout}.

**Consequences for wording and tests:**
- Task-facing text must say "not applicable — synthesis never ran" and show the recorded
  skip reason; it must **not** say "unverifiable" and must **not** raise an evidence-integrity
  warning.
- No provenance fields (`consumed_dependencies`, `dependency_digests`) are expected on such
  an attempt, and their absence is **not** a defect.
- T0.12 selector:
  `test_skipped_synthesis_provenance_absence_is_not_reported_as_unverifiable_or_stale`.
- This refines the sealed `REGENERATION_AND_STALE_SYNTHESIS.md` §4.2 table; the sealed table
  splits the same distinction and this rule fixes the evaluation order explicitly.

## O-2 — Sibling cancellation proof during generic run failure

**Validation requirement:** validation must **explicitly prove** that a sibling stage
cancelled during a generic run failure is persisted with `error_type == "cancelled"` **while
the run remains `FAILED` with `internal_error`**. The run-level state and the stage-level
cause are asserted separately, in one test, against the durable artifacts.

- Required selector (T0.6 companion):
  `test_cancelled_sibling_keeps_cancelled_cause_while_run_remains_failed_internal_error`.
- The assertion must read the persisted stage record and the persisted run state, not the
  in-memory objects.
- `provider_side_outcome_unknown` must be `True` for such a sibling when it had started, and
  the run must not be presented as cancelled.

## O-3 — Stage-level cancellation label

**Rule:** the stage-level cancellation label is exactly `error_type = "cancelled"`. HSF-4
(adding `RunState.CANCELLED`) changes the **run-level** state only. Generic run failure
remains run-level `FAILED` with `internal_error`; a sibling cancelled during that failure
retains its own `"cancelled"` cause rather than inheriting the run failure (per O-2). Durable
`cancelled_pending` after a hard kill reads as interrupted/uncertain and is never
represented as successful or automatically retried.

**Consequence:** any task-facing wording that named the stage label differently under the
HSF-4 4a branch is superseded by this rule. Stage `error_type` is `"cancelled"` under both
possible run-level vocabularies.

## O-4 — Serialized field name

**Rule:** the new serialized field name is standardized as **`api_key_source`**. Any use of
`key_source` in new serialized artifacts (`preflight.json`, snapshot records) is superseded
by `api_key_source`. The frozen `PreflightSnapshot.provider_routes` route record serializes:

```
{"kind": ..., "base_url": ..., "api_key_env_name": ..., "api_key_source": ...}
```

`api_key_source` records where the credential comes from (for example `environment`); it
never records a secret value. This is a naming standardization only — no semantic change.

---

## Scope guard

Applying O-1…O-4 may touch only: `REGENERATION_AND_STALE_SYNTHESIS` task wording as
implemented in `src/bots5/storage.py` / `src/bots5/usage.py` readers, `src/bots5/runner.py`
cancellation terminalization, the serialized route field naming in
`src/bots5/models.py` / `src/bots5/runner.py`, and the fenced Phase 10 test files. No path
outside `design/MUTATION_FENCE.json` is authorized by these clarifications.
