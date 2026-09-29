# Phase 10 — Campaign Desktop Integration: integrated design

Campaign: `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01` (parcel-v1, design-only).
Supervisor/integrator: DeepSeek V4.1 Flash (retained supervisor; not an independent reviewer).
Baseline: B.O.T.S. HEAD `0756904481ae884bb9e864e8e1e11fc4a27a72ff`
(parent `7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7`, tree `7fe879035a01a87339dfe6319a435fa0e84c94bd`).
OrgMem authority: `cc0c3c80348b1d798def65988102e0d8ad966730`.

This document is the integrated Phase 10 design. It reconciles six specialist reports:
`QWEN_CAMPAIGN_ARCHAEOLOGY.md`, `GEMINI_EVIDENCE_REGEN_ARCHITECTURE.md`,
`MINIMAX_LIFECYCLE_FORENSICS.md`, `COMMAND_OPERATOR_AUTHORITY_UX.md`,
`STEP_VALIDATION_ARCHITECTURE.md`, `GLM_FEASIBILITY_AND_MUTATION_FENCE.md`.
Disagreements are preserved in `SPECIALIST_DISAGREEMENTS.md`; genuine product-semantic
choices not settled by authority are enumerated in `HUMAN_SEMANTIC_FORK_REGISTER.md`.

> **Revision history.** Seal v1 → v2 repaired fresh-falsification findings F-01…F-08
> (`reports/review/GPT6_LUNA_DESIGN_FALSIFICATION.md`; ledger
> `reports/design/DESIGN_REPAIR_RECORD_v1_to_v2.md`). Seal v2 → v3 repaired final-oracle
> findings M-1…M-6 (`reports/oracle/MIMO_FINAL_DESIGN_ORACLE.md`; ledger
> `reports/design/DESIGN_REPAIR_RECORD_v2_to_v3.md`). Seal v3 → v4 repaired residual-oracle
> findings N-1…N-6 (`reports/oracle/MIMO_FINAL_DESIGN_ORACLE_v3.md`; ledger
> `reports/design/DESIGN_REPAIR_RECORD_v3_to_v4.md`). Each seal is preserved; see
> `seals/design/`.

## 0. Boundary statement (non-negotiable)

1. **The desktop operates the existing campaign engine; it does not replace it.** The
   engine (`manifest`, `models`, `runner`, `storage`, `events`, `usage`, CLI) remains the
   only producer of campaign evidence. The desktop calls engine entry points and reads
   engine evidence; it never writes campaign evidence itself.
2. **Filesystem run evidence remains authoritative** (`run.json`, `stages/*`, `usage.json`,
   `events.jsonl`, `result.md`, and the new `selection.json`/`preflight.json`). No UI
   state, SQLite row, callback or event bus becomes a competing campaign truth.
3. **Phase 10 is not a campaign authoring system.** No job editor, no DAG editor, no
   topology builder.
4. **Regeneration is append-only sibling evidence.** Never overwrite. Never a generic
   retry/resume/reuse. Selection of a different worker attempt is an explicit operator act.
5. **A changed selected worker attempt makes dependent synthesis mechanically stale.**
   Rerun is explicit and produces a new synthesis attempt; earlier synthesis evidence is
   preserved.
6. **Provider spend always needs explicit, digest-bound approval.** A prior approval never
   silently authorizes a later provider request.
7. **Do not invent live cost or provider stop certainty.** Live cost means known subtotal +
   explicit unknown set. A stage that may have reached a provider is `unknown`, never
   "did nothing".
8. **Do not implement provider streaming merely for UI animation.** The campaign runner
   remains non-streaming.
9. **Historical V0/V0.2 run directories are never rewritten.** Evidence evolution is
   additive and versioned.

## 1. Locked obligations → exact mechanism

| # | Contract obligation | Integrated mechanism |
|---|---|---|
| 1 | Load an existing campaign job | `manifest.load_job` + `validate_referenced_files` reused verbatim via the bridge (`core/campaign.py`); job path supplied explicitly (HSF-3) |
| 2 | Zero-spend validation, no provider call, no run-directory creation | Bridge calls the same side-effect-free validator pair as `bots5 validate`; asserted by tests proving no provider construction and no runs-dir creation |
| 3 | Preflight before spend | `PreflightSnapshot` constructed in memory; `preflight.json` written into the run dir only when a run is actually approved and started |
| 4 | Explicit operator approval before any provider request | `ApprovalRecord` bound to `preflight_digest`; `runner.run_job` asserts approval + re-verifies disk bytes before the first `provider.complete(...)`; refusal creates no run directory |
| 5 | Live truthful campaign/worker/stage/synthesis/cost progress | Bounded filesystem polling projection (250 ms, max 1000 ms) over `run.json`/`stages/*`/`usage.json`; known subtotal + explicit unknown set |
| 6 | Expandable worker/stage output and durable result inspection | Attempt-aware readers return persisted `stages/<id>.att<N>.md`; the dock renders selected-attempt output and `result.md`; CLI `status`/`inspect --attempt` unchanged in shape |
| 7 | Truthful successful / failed / timed-out / partial outcomes | Projection maps durable states exactly; `running` with no hosted task is `interrupted/uncertain`, never success/failure/resumable |
| 8 | Completed/failed result remains current until explicit `New Job` | Selection is durable on disk; `New Job` only clears desktop working context; restart persistence is HSF-2 |
| 9 | Selected-worker regeneration: preserve original, sibling attempt, explicit model change, no silent provider-route change, never generic retry/resume | Flat `<id>.att<N>.*` append-only attempts; provider route locked; explicit model string recorded; regeneration is a distinct approved engine operation (HSF-5 for provider change) |
| 10 | Changed selected worker attempt marks dependent synthesis stale | Mechanical predicate over recorded `consumed_dependencies` + `dependency_digests` vs current `selection.json` + current output digests |
| 11 | Synthesis rerun explicit, new attempt, earlier evidence preserved | `rerun_synthesis` engine entry point; `stages/<synth>.att<M+1>.*`; selection updated only on success; `result.md` mirrors selected synthesis |
| 12 | Existing filesystem evidence inspectable and authoritative | `evidence_version` 1 (absent) and 2 coexist; legacy readers byte-identical; golden-fixture tests land before any schema mutation |
| 13 | Headless CLI/engine usable without the desktop | CLI `validate/run/status/inspect` shape and exit codes unchanged; new parity verbs `inspect --attempt`, `status` attempt/stale markers, `rerun-synthesis` and `regenerate` (both preflight-only unless `--approve --actor <label>` or `--approval <path>` is given); engine entry points `regenerate_worker`/`rerun_synthesis` are directly usable as library calls; `core/application.py` zero-diff |

## 2. The central architectural problem and its resolution

The landed engine encodes **one record and one output path per stage id**
(`storage.persist_stage` derives `stages/<id>.json`/`.md` from `stage.id` alone;
`persist_run` aggregates a dict keyed by stage id; `record_by_id` is id-keyed). Sibling
regeneration cannot be expressed through that model.

The integrated resolution is an additive, versioned evidence evolution:

- `run.json` gains `"evidence_version": 2`; absence means version 1.
- Attempts are stored **flat inside `stages/`** as `<stage_id>.att<N>.json` and
  `<stage_id>.att<N>.md`.
- `selection.json` at the run root is the authoritative, filesystem-reconstructable
  mapping `stage_id -> selected attempt number`.
- `StageRecord` gains additive fields (`attempt_number`, `consumed_dependencies`,
  `dependency_digests`, `preflight_digest`), all defaulted.
- Synthesis attempts record exactly which attempt of each dependency they consumed, plus
  the SHA-256 of that attempt's output bytes, making staleness a pure function of durable
  evidence.
- `usage.json` gains dual accounting (`cumulative_spend` = financial truth across all
  attempts; `selected_spend` = the active pipeline used by the synthesis cost gate).
- `events.py` gains additive event kinds; the existing vocabulary is untouched.
- Preflight/approval binding is closed engine-side with a frozen `PreflightSnapshot` and a
  digest assertion immediately before the first provider request.

Full detail: `CAMPAIGN_EVIDENCE_EVOLUTION.md`, `REGENERATION_AND_STALE_SYNTHESIS.md`,
`PREFLIGHT_APPROVAL_STATE_MACHINE.md`.

### 2.1 Why flat attempt names, not nested directories

`storage.load_stage_view` enforces a security predicate: the resolved output path's parent
must be exactly `<run_dir>/stages` (`storage.py:196-198`). Flat `<id>.att<N>.md` files
satisfy that predicate unchanged; a nested `stages/<id>/<att>.md` layout would require
weakening a security guard. The design therefore adopts flat names and deliberately leaves
`paths.py` and the containment predicate **unchanged**.

This is an engineering compatibility decision, not a product-semantic one: every additive
layout satisfies the product requirement ("preserve the original attempt; create a sibling
attempt"). The specialists disagreed about classifying it as a human fork; see
`SPECIALIST_DISAGREEMENTS.md` D-2.

### 2.2 Attempt-name ambiguity guard

`SAFE_ID_RE` (`src/bots5/paths.py:10`) permits `.` and `-`, so a declared stage could itself
be named `w1.att2`. Attempt resolution is therefore **declared-id-driven and
selection-first**:

- for `evidence_version >= 2`, the selected attempt comes from `selection.json`;
- if `selection.json` is absent, the selected attempt is **1** for every declared stage
  (the initial state) — no filename scanning, no max-scan guesswork;
- when reading attempt `N` of declared stage `w1`, the reader builds the exact basename
  `w1.att<N>.json`; a missing file is a hard `ValidationError`, never a fallback guess;
- mixed or malformed v2 directories fail closed rather than silently selecting an
  unintended artifact.

This tightens Gemini §2.2 (which proposed a max-scan fallback) into a deterministic,
ambiguity-free rule. See `SPECIALIST_DISAGREEMENTS.md` D-3.

## 3. Desktop surface

A single new dock, `CampaignDock`, attached to the existing `MainWindow` composition seam,
plus a Qt-free `CampaignBridge` in `core/campaign.py`. The bridge owns:

- construction of `PreflightSnapshot`/`ApprovalRecord` (digests, frozen bytes);
- hosting the campaign coroutine on the desktop event loop through the existing
  single-flight/loop-pinned task discipline;
- a bounded filesystem poller producing an immutable `CampaignProjection`;
- shutdown participation: cancel → await terminal persistence → then allow the rest of the
  close driver to proceed.

The dock renders only the projection and issues explicit operator commands. It contains no
campaign truth. Full detail: `DESKTOP_SURFACE_AND_LIFECYCLE.md`.

## 4. Mutation fence

`MUTATION_FENCE.json` lists the exact tracked paths Phase 10 may change: **9 modify,
8 add**. `src/bots5/core/application.py`, `src/bots5/paths.py`, `src/bots5/manifest.py`,
`src/bots5/providers/**`, the Phase 9 desktop modules, and `evidence/**` are explicit
zero-diff guards. No dependency changes. No manifest schema change (therefore no
`JOB_SPEC.md` obligation under `docs/DEVELOPMENT.md:110-113`).

## 5. Sequence and validation

`IMPLEMENTATION_SEQUENCE.md` defines the bounded milestones (backward-compat fixtures
first, then engine truth, bridge, surface, cross-cutting). `VALIDATION_PLAN.md` defines
T0–T4 with non-vacuous selectors, serial constraints, and candidate-seal governance.
`CLOSURE_STRATEGY.md` defines the pre-commit boundary and what is *not* implied by
technical completion.

## 6. Human-semantic forks (must be adjudicated by Mick)

| ID | Fork | Supervisor recommendation |
|---|---|---|
| HSF-1 | Preflight pricing authority (absent in code; OPv1 requires conservative preflight pricing) | Operator-entered **currently advertised** rates (A) — the only branch compliant by default; unknown-cost+approval (D) only under an **explicit Mick waiver of the OPv1 §4 dollar bound**, not an equivalent branch |
| HSF-2 | Restart persistence of the loaded campaign/result | No app-DB campaign persistence in Phase 10 (filesystem-only) |
| HSF-3 | Run discovery seam | Explicit operator-supplied path/id only (no enumeration) |
| HSF-4 | Cancellation terminal vocabulary | Add distinct `RunState.CANCELLED` (engine-side terminalization is fixed regardless) |
| HSF-5 | Explicit provider change on regeneration | Provider route locked; model change only |

See `HUMAN_SEMANTIC_FORK_REGISTER.md` for options, consequences and evidence. Forks are
presented, not hidden; the design remains implementable under either branch inside the
same fence except where explicitly noted (HSF-2 option 2b expands the fence).

## 7. What this design does not authorize

No tracked mutation, no dependency change, no external provider call, no campaign
authoring, no generic retry/resume, no provider streaming, no daemon/remote client, no
Phase 11/12, no staging/commit/push/ref, no OrgMem mutation. Implementation requires
Mick's explicit acceptance of this design and a separate implementation-authority grant
plus an immutable successor parcel-v2.
