# Regeneration and stale synthesis

Scope: append-only sibling worker regeneration, explicit attempt selection, mechanically
stale dependent synthesis, and explicit synthesis rerun.
Sources: CONTRACT items 9–11; FINDINGS F-01/F-02/F-14; Qwen §1.3, §10, §12.2; Gemini §1–§3;
MiniMax §5, matrix rows 10, 15–20; Command §1.4, §5.1; Step §3.4–3.5.

> **Revision history.** v1 → v2: F-01 (operation-snapshot binding) and F-06 (three-outcome
> staleness). v2 → v3: M-2 (initial full-run provenance recorded at dispatch, §3.1) and
> M-6 (v1 runs are read-only for regeneration/rerun, §2/§5 precondition 0). v3 → v4: N-3
> (skipped / never-dispatched synthesis attempts are NOT_APPLICABLE, not UNVERIFIABLE, §4.2).
> See `reports/design/DESIGN_REPAIR_RECORD_v2_to_v3.md` and
> `reports/design/DESIGN_REPAIR_RECORD_v3_to_v4.md`.

## 1. The invariant

> Regenerating a selected worker **preserves the original attempt**, creates a **sibling**
> attempt, permits an **explicit model change**, never silently changes the **provider
> route**, and is **never** a generic automatic retry/resume.

Consequences:

- No file of the original attempt is modified or removed.
- No reuse of the old `run_id`: `create_run_tree` is a one-shot namespace claim
  (`storage.py:109-110`); regeneration therefore appends inside the existing run directory.
- Regeneration never happens automatically after a failure or timeout. It is an explicit
  operator operation with its own zero-spend validation, preflight and approval.
- "Resume" does not exist. There is no code path that continues an interrupted run.

## 2. Worker regeneration operation (engine entry point)

`runner.regenerate_worker(job, providers, *, run_dir, stage_id, model, snapshot, approval)`

`snapshot` here is an `OperationSnapshot` (see `PREFLIGHT_APPROVAL_STATE_MACHINE.md` §2.2),
not merely the job-level preflight.

Preconditions (all verified from disk before any provider call):

0. **Evidence version (M-6 repair).** The target run directory must declare
   `evidence_version >= 2`. A version 1 run (marker absent) is **read-only**: regeneration
   is refused with a typed reason and **nothing is written**, because writing
   `<id>.att<N>.*` into a v1 directory would create the mixed layout this design elsewhere
   forbids and would make the new attempts invisible to v1 readers. The operator may run
   the job as a new run instead, leaving the v1 evidence untouched.
1. The run directory exists and declares `stage_id` in `stage_order`.
2. `job.resolved.json` matches the job being used (same canonical content) — regeneration
   must not silently run a different topology.
3. The requested stage is a worker (never the synthesis stage; synthesis rerun is a
   separate operation).
4. `approval.scope == "worker_regeneration:<stage_id>"`,
   `approval.preflight_digest == snapshot.preflight_digest`, and `approval.target` matches
   `{run_id, stage_id, attempt_number}`.
5. The provider route for the stage is **identical** to the recorded route in
   `job.resolved.json` and to the frozen route, and the concrete provider instance's route
   attributes match it (`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §4.7). Only `model` may
   differ from the original attempt.
6. Referenced bytes match the snapshot (same assertion discipline as a fresh run).
7. The approval has not been consumed (`§3.1` of the preflight document).

Execution:

1. `N` is the **binding** attempt number: it is chosen at preflight time, recorded in the
   `OperationSnapshot` and `approval.target`, and re-derived from disk before dispatch. If
   the re-derived next attempt number differs from the approved `N` (a concurrent
   regeneration landed first), the engine refuses with `ApprovalInvalidatedError` — it does
   not silently renumber.
2. Create `stages/<stage_id>.att<N>.json` with **exclusive-create** semantics (refuse to
   overwrite an existing attempt); write state `queued` → `running` → terminal, and on
   success `stages/<stage_id>.att<N>.md`.
3. The attempt record carries `attempt_number = N` and `preflight_digest`.
4. Events: `worker_regeneration_started`, `stage_started`, `request_sent`,
   `stage_succeeded`/`stage_failed`, `worker_regeneration_finished`.
5. `usage.json` `cumulative_spend`/`per_attempt` include the new attempt immediately.
   `selected_spend` and `selection.json` are unchanged by the execution itself.

### 2.1 Model change

The new attempt may request a different `model` string. The `requested_model` of the
sibling records the new model; the original attempt's `requested_model` is untouched. The
provider route is never altered (HSF-5 for any explicit provider switch).

### 2.2 Selection is explicit

After a successful regeneration, the dock offers the new attempt and a separate explicit
**"Make current"** action writes `selection.json` (event `attempt_selected`). A failed
regeneration never changes selection.

Design rationale: selection is current evidence; changing which attempt is current should be
an explicit, recorded operator act, and obligation 10 ties staleness to a *changed selected
attempt*. Auto-selecting the new attempt on completion is a possible UX variant and is
recorded as a non-blocking open UX question (not a product fork) in
`SPECIALIST_DISAGREEMENTS.md` D-5.

## 3. Synthesis provenance

Every synthesis attempt permanently records what it consumed:

```
consumed_dependencies = { dep_id: attempt_number }   # for dep in synthesis.depends_on
dependency_digests    = { dep_id: sha256(output bytes of that attempt) }
```

Recorded **at preflight time** from the then-current `selection.json`, bound into the
`OperationSnapshot`/approval, re-verified against disk immediately before dispatch, and
persisted on the attempt record. The digest is computed from the exact bytes that are placed
into the frozen synthesis user message. The engine dispatches only that frozen message; if
the selection or bytes changed between preflight and dispatch, it refuses before any
provider request (F-01 repair).

### 3.1 Initial full run — provenance is recorded at dispatch (M-2 repair)

A full run cannot bind dependency digests at preflight: no `selection.json` exists yet and
the worker output bytes do not yet exist
(`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §2.2 therefore leaves
`dependency_attempts`/`dependency_digests` empty for `full_run`). Provenance is instead
persisted **at synthesis dispatch inside `run_job`**, which is where the exact bytes exist
(`runner.py:404-415`):

- resolve the selected attempt for each declared dependency (for an initial run, selection
  is absent ⇒ attempt 1 for every stage, per
  `CAMPAIGN_EVIDENCE_EVOLUTION.md` §2.2);
- render the synthesis user message from exactly those outputs and compute
  `dependency_digests = {dep: sha256(bytes rendered into that message)}`;
- set `consumed_dependencies = {dep: <selected attempt>}`;
- persist both on the synthesis attempt record **before** the synthesis provider request
  (`request_sent`), so a crash cannot leave a synthesis attempt without provenance.

No pre-dispatch approval byte-binding is needed for the full-run case because the dependency
bytes are produced by the same run under the same approval; the approval still binds the
job/contracts/limits/route. Regeneration and rerun (whose dependency bytes pre-exist and can
change after approval) do require pre-dispatch binding, as specified in §5.

Consequence, and the point of the M-2 repair: an initial-run synthesis is **FRESH** as soon
as it exists (selection defaults to attempt 1 and the digests match), and it becomes
**STALE** the moment a dependency is regenerated and reselected. Obligation 10 fires for
first-generation runs too.

This is the central new evidence obligation: today the relation "which output did synthesis
consume" is implied only by id equality and in-memory strings (`runner.py:307, 331-333,
404`), and is not persisted (Qwen §10).

## 4. Mechanical staleness predicate

Let:

- `s = job.synthesis.id`, `D = job.synthesis.depends_on`;
- `S_M` = synthesis attempt `M` (metadata `stages/<s>.att<M>.json`);
- `sigma(w)` = `selection.json.selected_attempts[w]`;
- `bytes(w, N)` = output text of attempt `N` of `w` (`stages/<w>.att<N>.md`);
- `H(w, N) = sha256(bytes(w, N))`.

`S_M` is **FRESH** iff for every `w in D`:

```
sigma(w) == S_M.consumed_dependencies.get(w)
and H(w, sigma(w)) == S_M.dependency_digests.get(w)
```

Otherwise `S_M` is **MECHANICALLY STALE**. The predicate is a pure function of durable
filesystem evidence; no GUI memory, no flag, no database row participates.

### 4.1 Bidirectionality

Because freshness is evaluated against the *current selection*, staleness is reversible:

- `w1` has attempts 1 and 2;
- `synth` attempt 1 consumed `w1` attempt 1; `synth` attempt 2 consumed `w1` attempt 2;
- select `w1` attempt 2 ⇒ synth 1 STALE, synth 2 FRESH;
- select `w1` attempt 1 again ⇒ synth 1 FRESH, synth 2 STALE.

No evidence is destroyed in either direction.

### 4.2 Three outcomes, never a fabricated "current" (F-06 repair)

The predicate yields exactly one of **FRESH**, **STALE**, or **UNVERIFIABLE**, and the
distinction is driven by the evidence version:

| Case | Outcome | Display |
|---|---|---|
| v2 attempt, provenance present, selection+digests all match | FRESH | current |
| v2 attempt, provenance present, selection or digest mismatch | STALE | stale (+ integrity warning on digest mismatch) |
| v2 attempt, provenance present, selected output bytes missing/unreadable | STALE | stale + integrity warning |
| **v2 attempt in a non-dispatched terminal state** (`SKIPPED`: dependency failed/incomplete, known-cost threshold exceeded, not reached before timeout) | **NOT_APPLICABLE** | show the recorded skip reason; synthesis never ran, so provenance is not applicable — **no integrity warning** |
| **v2 attempt, dispatched, provenance absent or malformed** | **UNVERIFIABLE** | integrity warning; never "current", never silently "stale" |
| **v1 attempt (no `evidence_version`, no provenance fields)** | LEGACY_UNVERIFIED | "current (provenance not recorded — pre-v2 evidence)" |

Rationale: a v1 run genuinely predates provenance, so it must not be reported as stale from
absent data. A **v2** attempt is required by this design to carry provenance **when it is
dispatched**; absence on a dispatched attempt is an evidence-integrity defect and must not be
laundered into a fabricated "current" result. A **skipped** synthesis (`StageState.SKIPPED`,
set at `runner.py:347-361`, `:366-380`, `:385-399`, `:449`) was never dispatched and
therefore never had provenance to record (M-2's recording point is dispatch, §3.1): it is
NOT_APPLICABLE and is displayed with its recorded `synthesis_skipped_reason`, not as an
integrity failure (N-3 repair). Malformed maps (non-integer attempt numbers, non-hex
digests) on a dispatched attempt are UNVERIFIABLE.

### 4.3 Digest / byte anomalies

If the recorded dependency digest does not match the current bytes of the selected attempt,
or the selected attempt's output is missing/unreadable, the synthesis is STALE **and** the
anomaly is surfaced as an evidence-integrity warning. The design never repairs it silently
and never re-digests to "make it fresh".

## 5. Explicit synthesis rerun

`runner.rerun_synthesis(job, providers, *, run_dir, snapshot, approval)`

`snapshot` is an `OperationSnapshot` binding `target_run_id`, the synthesis stage, the next
attempt number, every selected dependency attempt number and output SHA-256, and the exact
rendered synthesis user message (`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §2.2).

Preconditions:

0. **Evidence version (M-6 repair).** The target run directory must declare
   `evidence_version >= 2`; a version 1 run is read-only and rerun is refused with a typed
   reason, writing nothing (same reasoning as §2 precondition 0).
1. `approval.scope == "synthesis_rerun"`, digest identity asserted, and `approval.target`
   matches `{run_id, stage_id, attempt_number}`.
2. All declared dependencies' currently selected attempts are `SUCCEEDED` with
   `completion_complete is True` (`finish_reason == "stop"`), matching the existing gate
   semantics (`runner.py:344-380`). Otherwise the operation is **refused** with a typed
   reason; nothing is written.
3. The pre-synthesis known-cost gate is evaluated against the **derived** worker selected
   cost (not cumulative), preserving today's semantics for the active pipeline.
4. The approval has not been consumed.
5. **Selection/bytes binding**: the currently selected dependency attempts and the SHA-256
   of their output bytes equal the operation snapshot's `dependency_attempts` /
   `dependency_digests`; otherwise `ApprovalInvalidatedError` before dispatch. The frozen
   synthesis user message is dispatched verbatim — no re-render, no re-read.

Execution:

1. `M_next` is the approved, disk-re-derived next synthesis attempt number; a mismatch with
   the approval target refuses rather than renumbers.
2. Create `stages/<s>.att<M_next>.json` with **exclusive-create** semantics, carrying
   `consumed_dependencies`/`dependency_digests` from the frozen snapshot, and `.md` on
   success.
3. Nothing about earlier synthesis attempts is modified.
4. On success, `selection.json[s] = M_next` (event `attempt_selected`), `usage.json`
   selected summaries are refreshed (see `CAMPAIGN_EVIDENCE_EVOLUTION.md` §4), and
   `result.md` is updated to mirror the new attempt's output. On failure, selection is
   unchanged.

Rerun is never triggered automatically by worker regeneration, worker completion, staleness
detection, timeout or shutdown.

## 6. `result.md` semantics

`result.md` is the mirror of the currently selected synthesis attempt's output text.

- It is written when a run's synthesis attempt completes **and** is selected (initial run).
- It is rewritten when selection intentionally moves to another synthesis attempt.
- It is **not** touched by worker regeneration.
- "The existence of `result.md` is not proof of overall success"
  (`docs/OPERATING_PROCEDURE_V1.md:112-116`) remains true; the projection reads run state,
  not the file's existence.

## 7. Forbidden operations (fail-closed)

- automatic retry of a failed/timed-out/uncertain stage;
- "resume" of an interrupted run;
- overwriting or deleting any attempt;
- reusing a `run_id`;
- continuing an interrupted paid provider request;
- regeneration whose provider route differs from the recorded route (HSF-5);
- rerun synthesis when dependencies are not normally complete;
- selecting an attempt that does not exist on disk.

## 8. Reconstructability

Any later reader, with no application state, can recover from disk alone: which attempts
exist per stage, which is selected, what each synthesis attempt consumed, whether the
selected synthesis is fresh or stale, and the full cost/token accounting per attempt and in
aggregate.
