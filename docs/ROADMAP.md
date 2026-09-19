# Roadmap

## Closed campaign baseline

B.O.T.S. 5 V0 is closed. Final zero-spend closure validation passed against
`13e3ac463c44d66e57d4443027f0cc9dfe9b93a5`; see `docs/V0_CLOSURE_REPORT.md`.

The closed baseline includes strict manifest validation, explicit UTF-8 inputs, bounded worker
parallelism, optional synthesis, durable run/stage/event/usage artifacts, disk-only inspection,
completion-aware state, and exact-known/unknown cost semantics.

## V0.2 — local OpenAI-compatible provider

Implemented, deterministically validated, live-proven, and landed. Schema v2 adds explicit per-stage
provider mapping and one built-in non-streaming local OpenAI-compatible provider while preserving the
closed campaign execution model and schema-v1 OpenRouter behavior.

See `docs/V0_2_DESIGN_CAMPAIGN_REPORT.md` and `docs/OPERATING_PROCEDURE_V2.md`.

## Linux v0.1 — native desktop

The Linux v0.1 product, architecture, implementation technology, and phased construction sequence are
accepted. The build-facing contract is `docs/LINUX_V0_1_DESIGN.md`.

### Landed status

Phases **1 through 8 and Phase 9 Slice A are implemented, validated, committed, and landed on `main`**.

- Phase 1: native walking skeleton, core, persistence, events, fake streaming backend.
- Phase 2: conversation truth, immutable lineage/revisions, and deterministic fake generation.
- Phase 3: real generation backend contract, streaming, cancellation, checkpointing, and local-model
  acceptance.
- Phase 4: concurrency, multi-window workspace, shutdown, and crash reconciliation.
- Phase 5: provider/model usability, secrets, catalogue/capability discovery, and settings.
- Phase 6: deterministic context construction, reusable content-addressed attachments, unified data-root
  authority/effect grants, rooted SQLite/native durability handling, recovery/fail-closed integrity, and
  complete resource-lifetime ownership.

The Phase 6 landing commit is
`20847c7a49e26679d0d3dfe99798a2c211bec436`.

The final exact Phase 6 candidate passed:

- complete Phase 6 + authority/grant suite: **616 passed, 0 skipped, 0 failed**;
- complete repository suite: **956 passed, 1 expected skip, 0 failed**.

The expected skip was the opt-in local-provider acceptance test; no provider was contacted. Final external
Astra closure disposition was **PASS WITH CAVEATS**, with no concrete current blocker. See
`docs/LINUX_V0_1_PHASE6_CLOSURE_REPORT.md`.

The cumulative implementation archaeology remains in the phase implementation reports. Earlier
candidate-status statements such as “unstaged”, “uncommitted”, or “not yet accepted” describe those
historical checkpoints and do not override the current closure record.

- Phase 7: search and exact navigation, closed and landed at
  `6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`.
- Phase 8: inspection and provenance UX, closed and landed at
  `f72e0ea6e10694972f3b3455d973651629de448a`.
- Phase 8 final T4 closure evidence: **1082 passed, 1 skipped, 3124 warnings in
  3946.05s (1:05:46)**, exit `0`, with Candidate E byte-identical and no
  provider/network/credential contact.
- Phase 9 Slice A: Transcript v0.1 and strict Archive v1 export, accepted and landed at
  `35b206a404d4cd3e2dd05a5c07ffdc6dd0e1ba40`.
- Phase 9 Slice A final T4: **1133 passed, 1 skipped, 1 accepted pre-existing Phase 8 roadmap-test
  failure**; fresh Sol/High final falsification reported no concrete current blocker.

### Current implementation phase

**Phase 7 — search and exact navigation** and **Phase 8 — inspection/provenance UX** are closed and
landed. **Phase 9 Slice A — Transcript v0.1 and Archive v1 export** is closed and landed at
`35b206a404d4cd3e2dd05a5c07ffdc6dd0e1ba40`. **Phase 9 Slice B — validated Archive v1 import and
durable import provenance** is the current design/oracle boundary. Slice B implementation is not
implicitly authorized by Slice A landing or by its accepted product semantics.

The accepted desktop sequence remains:

1. Phase 0: durable design authority and live-repo reinspection;
2. Phase 1: walking skeleton;
3. Phase 2: conversation truth/lineage/revisions;
4. Phase 3: real generation;
5. Phase 4: concurrency/workspace;
6. Phase 5: provider/model usability;
7. Phase 6: context and attachments — **closed**;
8. Phase 7: search and exact navigation — **closed and landed**;
9. Phase 8: inspection/provenance UX — **closed and landed**;
10. Phase 9: import/export, backup, verification, and restore — **Slice A landed; Slice B design/oracle next; Slices C–E later**;
11. Phase 10: campaign desktop integration;
12. Phase 11: product finishing and standalone packaging;
13. Phase 12: Linux v0.1 torture run and separate closure adjudication.

Every phase retains inspect -> propose -> approve -> edit -> validate -> separate commit approval ->
separate push/landing approval.

### Current product target

Linux v0.1 remains a real native Linux desktop application centred on persistent multi-chat with lossless
lineage, concurrent streaming generation, deterministic context construction, reusable attachments,
search, provenance/inspection, backup/recovery, and a thin operational campaign surface.

The provisional Draft 1 UI authority remains `docs/LINUX_V0_1_UI_UX_DRAFT_1.md`; it is intentionally not
promoted to final UI design authority merely because later backend phases have landed.

## Deferred beyond Linux v0.1

Unless a concrete Linux v0.1 blocker separately promotes a bounded requirement, defer:

- Android;
- Code/Git;
- persistent daemon/remote clients;
- MCP/general tool/plugin frameworks;
- scheduler/automation;
- remote B.O.T.S. execution nodes;
- RAG/semantic search;
- mode-governed capability envelopes and post-training/LoRA mode selection;
- multi-capability workflow composition.

## Later campaign-harness candidates

The original campaign harness may later gain, under separate authority:

- stage-output reuse/resume after partial campaign failure;
- planner-generated manifests;
- richer but still explicit DAG semantics;
- deliberate retry policy where idempotence is understood;
- stronger filesystem/tool capability models.

These are separate from the bounded Linux v0.1 desktop sequence.

## Carried deferred defect

Extremely large JSON integers can escape `_number()` through `float()` overflow instead of a clean
validation error. This remains low operational consequence and explicitly deferred beyond V0 closure.
