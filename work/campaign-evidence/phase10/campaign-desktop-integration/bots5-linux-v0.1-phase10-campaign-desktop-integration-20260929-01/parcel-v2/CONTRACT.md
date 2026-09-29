# Phase 10 execution contract

## Objective

Design, then after a separate Mick gate implement, B.O.T.S. Linux v0.1 Phase 10:
the thin native desktop operational surface over the existing campaign engine.

The phase is complete technically only when the accepted Phase 10 behavior is
operator-reachable from the native desktop, the headless campaign engine remains
independently usable, the filesystem evidence remains authoritative and
reconstructable, regeneration/stale-synthesis semantics are append-only and truthful,
and the exact frozen implementation candidate passes its required validation/review
through the sealed pre-commit boundary.

## Locked product obligations

Phase 10 must provide:

1. load an existing campaign job;
2. zero-spend validation with no provider call and no run-directory creation;
3. preflight before spend;
4. explicit operator approval before any provider request;
5. live truthful campaign/worker/stage/synthesis/cost progress;
6. expandable worker/stage output and durable result inspection;
7. truthful successful, failed, timed-out and partial outcomes;
8. completed/failed result remains the current result until explicit `New Job`;
9. selected-worker regeneration that:
   - preserves the original attempt;
   - creates a sibling attempt;
   - permits an explicit model change;
   - does not silently change provider route;
   - never becomes generic automatic retry/resume;
10. a changed selected worker attempt marks dependent synthesis stale;
11. synthesis rerun is explicit and produces a new synthesis attempt while preserving
    earlier synthesis evidence;
12. existing campaign filesystem evidence remains inspectable and authoritative;
13. the headless CLI/engine remains usable without the desktop.

## Central architectural problem

The landed engine has one `StageRecord` and one output path per stage. The desktop
cannot satisfy sibling regeneration/stale synthesis merely by adding buttons. Design
an additive append-only attempt/evidence model that preserves old evidence and old
run-directory readability while making current attempt selection and stale synthesis
mechanically reconstructable.

Do not rewrite historical stage output in place.

## Preflight / approval integrity

The design must bind approval to the exact consequential work that will execute.

Today the job model contains filesystem paths and the runner re-reads referenced
inputs/contracts at execution. A prior UI validation/approval must not silently apply
if those bytes, model/provider selection, token ceilings, execution limits,
dependencies, or output target have changed.

The design must establish an immutable or mechanically reverified execution snapshot
before the first provider request. If this cannot be done inside existing accepted
semantics without a product choice, classify a HUMAN_SEMANTIC_FORK.

## Cost truth

The current campaign harness has provider-reported cost after completion but no
authoritative campaign pricing registry.

`live cost progress` therefore means truthful known cost + unknown/incomplete state as
evidence becomes available. Do not fabricate live token-dollar progress.

OPv1 separately requires conservative preflight pricing for paid execution. The design
must inspect how Phase 10 can satisfy that without inventing an unapproved pricing
authority. If operator-entered pricing, external provider pricing lookup, cached
pricing, or another approach represents a genuine product-semantic choice not settled
by authority, STOP FOR MICK with explicit options.

## Lifecycle truth

- The desktop must not become a second campaign engine.
- The desktop may observe/operate the existing engine through explicit seams.
- Filesystem run evidence remains authoritative after crash/restart.
- A crash may leave a durable run that says `running`; do not silently call that
  resumable/succeeded.
- Do not automatically retry provider work after uncertain external acceptance/spend.
- Active campaign work must participate truthfully in application shutdown.
- `New Job` clears/selects UI working context; it never deletes historical run
  evidence.

## Backward compatibility

Existing V0/V0.2 run directories and normal CLI flows must remain inspectable. Any
evidence-schema extension must be additive/versioned or otherwise safely readable
without rewriting historical evidence.

## Explicit exclusions

Not Phase 10:

- campaign-authoring UI;
- arbitrary DAG editor;
- general stage retry/resume/reuse;
- automatic provider retry;
- provider streaming redesign merely for UI progress;
- persistent daemon/remote clients;
- tools/plugins/MCP/RAG/Code/Git/Work/Research/Swarm modes;
- Phase 11 finishing/packaging;
- Phase 12 torture run;
- retained-installation cleanup;
- dependency changes unless a separately approved blocker requires one;
- external paid product canaries/tests without separate Mick authority.

## Design phase

Parcel-v1 authorizes design/discovery only.

Run:

1. Qwen3.8 Flash repository/campaign-engine archaeology.
2. Then in parallel:
   - Gemini 3.8 Flash evidence/regeneration architecture;
   - MiniMax M2.7 lifecycle/failure forensics;
   - Command A+ operator authority/preflight/spend UX analysis;
   - Step 3.7 Flash validation/proof architecture.
3. GLM 5.3 Flash implementation feasibility + exact mutation fence after reading the
   prior specialist reports.
4. DeepSeek supervisor integrates one exact design and preserves disagreements.
5. Materialize an immutable design seal.
6. Fresh GPT-6 Luna design falsification against that seal.
7. Classify/repair/reseal in design evidence only.
8. Fresh MiMo V2.6 final design oracle.
9. STOP FOR MICK for exact design acceptance + implementation authority.

Required integrated design artefacts:

- `design/PHASE10_CAMPAIGN_DESKTOP_DESIGN.md`
- `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md`
- `design/CAMPAIGN_EVIDENCE_EVOLUTION.md`
- `design/REGENERATION_AND_STALE_SYNTHESIS.md`
- `design/DESKTOP_SURFACE_AND_LIFECYCLE.md`
- `design/MUTATION_FENCE.json`
- `design/IMPLEMENTATION_SEQUENCE.md`
- `design/VALIDATION_PLAN.md`
- `design/CLOSURE_STRATEGY.md`
- exact design seal under `seals/design/`

A real semantic fork must be presented rather than hidden in the integrated design.

## Implementation phase after Mick gate

Do not implement from parcel-v1.

After exact design acceptance:

1. persist Mick's verbatim adjudication;
2. create retained continuation + successor campaign record;
3. create immutable parcel-v2 carrying the accepted design/fence;
4. re-run v0.3 structural, semantic and prompt consistency checks;
5. implement only inside the accepted mutation fence;
6. use focused T0–T3 gates during milestones;
7. freeze and **materialize the implementation candidate seal before any independent
   implementation review**;
8. run fresh independent review against that exact seal;
9. classify/repair/reseal/revalidate as needed;
10. run one final T4 against the exact final sealed candidate because Phase 10 is a
    milestone closure;
11. verify candidate unchanged after T4;
12. optional Jamba final reviewer only if justified;
13. fresh MiMo final implementation oracle against the exact seal + validation evidence;
14. reconcile evidence and write `reports/COMMIT_BOUNDARY_REPORT.md`;
15. STOP FOR MICK at the sealed pre-commit boundary.

If product bytes change after the final T4, that T4 is no longer the final gate.
Reseal and rerun the applicable final validation.

## Anti-vacuity / anti-loop rules

- Every selected pytest gate must prove a non-zero collection count.
- A passing suite is not rerun solely to obtain its numeric count.
- Do not launch multiple pytest processes concurrently when a gate is specified as
  serial.
- Full T4 is not a routine repair loop. Use focused tests during repair and reserve
  the final full suite for the final exact candidate unless newly discovered
  cross-cutting impact justifies otherwise.
- Same semantic defect class persisting/recurring requires a different model family
  for diagnosis before another mutation attempt.

## Human gates

Technical completion, substantive acceptance, staging/commit, push, and OrgMem landing
are separate consequences. This campaign never infers later authority from an earlier
gate.

---

## Parcel-v2 authorization (Mick gate satisfied)

Parcel-v1's design phase is complete and its human gate is satisfied. Mick accepted design
seal v4 (`9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1`) as the
implementation design and granted implementation authority **inside the unchanged sealed
mutation fence**. See `parcel-v2/ADJUDICATION.md` (verbatim) and
`parcel-v2/NORMALIZED_CORRECTIONS.md` (O-1…O-4 task-facing clarifications).

- The design of record is sealed under `seals/design/DESIGN_SEAL_v4.json` and **must not be
  mutated or replaced** by this parcel.
- Authorized: implementation, impact-based validation, independent implementation
  falsification/review, bounded repairs, candidate resealing, final T4, evidence
  reconciliation, and the reserved fresh MiMo final implementation oracle.
- Not authorized by this parcel: staging, commit, push, ref mutation, OrgMem mutation,
  dependency changes, Phase 11/12 work, or any change outside the accepted fence.
- Human semantic forks HSF-1…HSF-5 are adjudicated (Branch A; 2a; 3a; 4a; 5a). No further
  fork may be invented; if the fence, accepted product semantics, dependency boundary, or an
  adjudication must change, STOP FOR MICK.
- MiMo V2.6 Flash budget is amended 3 → 4 launches; the fourth is reserved exclusively for
  the fresh final implementation oracle against the final frozen/resealed candidate.
