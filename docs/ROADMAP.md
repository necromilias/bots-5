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

Phases **1 through 8 and Phase 9 Slices A and B are implemented, validated, committed, and landed on `main`**.
Phase 9 Slices C, D, and E are likewise implemented, validated, and landed, so **Phase 9 is closed**.
**Phase 10 — campaign desktop integration — is landed** at
`9762170099889ecd87d451341a15a29ce7aceae8`.
**Phase 11 — product finishing and standalone packaging — is closed and landed** at
`59265916abeb2e9f6cbf953726f22a9f7c00f3b5`; see the [Phase 11 closure record](LINUX_V0_1_PHASE11_CLOSURE_REPORT.md).
Final retained validation: **2189 passed / 1 permitted skip / 0 failures / 0 errors** on T4,
**71/71** frozen cleanup and **156/156** M7 checks. The validated standalone remains local and unreleased.

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
- Phase 9 Slice B: validated Archive v1 import, strict Archive v2 provenance evolution, durable import
  provenance, persistent import queue/recovery, attachment healing, and branch-aware continuation;
  substantively accepted and landed at `9a84d38b6ad2d3968db58f471d53bf85820656b1`.
- Phase 9 Slice B current-byte T2: **1038 passed, 1 skipped**; final T4: **1238 passed, 1 skipped**,
  exit `0`, candidate unchanged. Independent adversarial review and final oracle both returned PASS;
  the authorized shared review-model substitution is retained as an evidence-diversity limitation.
- Phase 9 Slice C: Backup v1 capture and independent closed-manifest verification, and migration
  recovery points — closed and landed (`core/backup.py`, `infrastructure/backup_capture.py`,
  `infrastructure/backup_package.py`).
- Phase 9 Slice D: whole-installation staged restart restore, startup reconciliation of interrupted
  restores, indefinite operator-directed retention, the non-UI `--restore-from` entry point, and
  typed destructive-failure semantics — closed and landed (`infrastructure/restore_service.py`,
  `bootstrap/desktop.py`); the restore semantics are documented in `docs/LINUX_V0_1_DESIGN.md`.
- Phase 9 Slice E: native desktop integration for the accepted Phase 9 workflows and Phase 9
  technical closure — **landed** at `0756904481ae884bb9e864e8e1e11fc4a27a72ff`; recorded in
  `docs/LINUX_V0_1_PHASE9_SLICE_E_CLOSURE_REPORT.md`. Phase 9 is closed.
- Phase 10: campaign desktop integration — **closed and landed** at
  `9762170099889ecd87d451341a15a29ce7aceae8`; recorded in
  `docs/LINUX_V0_1_PHASE10_CLOSURE_REPORT.md`.
- SQLite ≤ 3.45.1 parser-compatibility repair — landed at
  `dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f`; recorded in
  `docs/SQLITE_COMPATIBILITY_REPAIR.md`.
- CI v1 — landed, culminating in the CI-v1 admission-retirement commit
  `5bb783326b5a8c68bb1e2b2719aca6070e22f225`, which made the workflow dispatch-only; the
  authoritative run exercised candidate `58fca2c7b1b4111d982733980c303565bf91695e`; recorded in
  `docs/CI_V1.md`.

### Current implementation phase

**Phase 7 — search and exact navigation**, **Phase 8 — inspection/provenance UX**, **Phase 9 Slice A —
Transcript v0.1 and strict Archive v1 export**, and **Phase 9 Slice B — validated archive import and
durable import provenance** are closed and landed. Slice B is landed at
`9a84d38b6ad2d3968db58f471d53bf85820656b1`. **Phase 9 Slices C, D, and E are also closed and landed**,
so **Phase 9 is closed** (see their bullets above and `docs/LINUX_V0_1_DESIGN.md`). **Phase 10 —
campaign desktop integration — is closed and landed** at
`9762170099889ecd87d451341a15a29ce7aceae8` (`docs/LINUX_V0_1_PHASE10_CLOSURE_REPORT.md`). The
SQLite ≤ 3.45.1 parser-compatibility repair landed at
`dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f`, and CI v1 landed through the admission-retirement commit
`5bb783326b5a8c68bb1e2b2719aca6070e22f225`, with the authoritative run exercising candidate
`58fca2c7b1b4111d982733980c303565bf91695e` rather than `5bb7833` or any later `main` descendant
(`docs/CI_V1.md`).

**Phase 12: Linux v0.1 torture run and separate closure adjudication** is next. Phase 11 is
closed and landed as recorded above. Phase 12 design and execution are not authorized by this
documentation reconciliation; detailed planning remains a later inspect/propose/approve boundary.

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
10. Phase 9: import/export, backup, verification, and restore — **closed; Slices A through E landed**;
11. Phase 10: campaign desktop integration — **closed and landed**;
12. Phase 11: product finishing and standalone packaging — **closed and landed**;
13. Phase 12: Linux v0.1 torture run and separate closure adjudication — **next; not authorized**.

Every phase retains inspect -> propose -> approve -> edit -> validate -> separate commit approval ->
separate push/landing approval.

### Current product target

Linux v0.1 remains a real native Linux desktop application centred on persistent multi-chat with lossless
lineage, concurrent streaming generation, deterministic context construction, reusable attachments,
search, provenance/inspection, backup/recovery, and a thin operational campaign surface.

The landed Phase 11 shell follows the accepted later chrome/colour authority recorded in the
[closure report](LINUX_V0_1_PHASE11_CLOSURE_REPORT.md). Historical Draft 1 and later visual candidate
documents retain their provisional/draft scope; they are not promoted wholesale by this reconciliation.

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
