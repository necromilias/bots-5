# B.O.T.S. Linux v0.1 Phase 9 Slice E closure report

Date: 2026-09-28 Australia/Melbourne

## Disposition

Phase 9 Slice E is technically complete in this candidate: the native desktop surfaces for the
landed Phase 9 capabilities are implemented, wired to production actions, and validated, and the
tracked status documentation is made truthful to landed fact.

This report is the technical-closure record embedded in the candidate. The candidate is
pre-commit: final Git/OrgMem landing identity is a later administrative fact, reserved to a
separate acceptance decision, and this report claims no commit, push, merge, tag, or
remote-landing fact for it.

## Campaign and candidate identity

- design campaign: `bots5-linux-v0.1-phase9-slice-e-20260927-01`;
- implementation campaign: `bots5-linux-v0.1-phase9-slice-e-impl-20260927-02`;
- baseline HEAD: `7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7` (the Slice D landing commit, whose
  parent `81a818a3` is the Slice C landing commit), mechanically verified before the first
  tracked mutation with a clean tracked tree;
- OrgMem authority: `7ab91a51ddb3aea626420c9d74dac12adf6e6181`.

The candidate comprises the baseline plus exactly the fenced changed paths: narrow additive
core/application/store projections, the new desktop module set, thin `window.py`/`widgets.py`
wiring, the bootstrap restore-handoff plumbing, additive tests, and the five documentation paths
including this report. No commit identity exists for the candidate at the time of writing.

## What Slice E added

Slice E adds the production-reachable desktop surfaces for the Phase 9 operator workflows 1-9
(`work/campaign-evidence/phase9/slice-e/.../design/DESKTOP_PHASE9_WORKFLOWS.md`), through four
new modules:

- `src/bots5/desktop/phase9.py` — `Phase9DesktopController` (task orchestration, schedule,
  status), `Phase9ProgressBridge` (worker-to-GUI-thread `QObject` signal bridge), and
  `RestoreHandoffCoordinator` (the sealed workflow 8 chain: live verify, consequence dialog,
  one-shot request registration, orderly close, post-close result reporting). The workflow's
  inhibit/cutoff-past-settlement step is not a separate coordinator call: it is delivered by the
  landed desktop close path that the coordinator's orderly close invokes, which cancels the
  archive-import scheduler, drains cutoff-past settlements and releases the authority last;
- `src/bots5/desktop/phase9_imports.py` — `ImportQueueViewModel` and `QueueRowView` projecting
  the flat queue display page (basename label, read-only row revision CAS token, state, failure
  label, timestamps, derived control flags);
- `src/bots5/desktop/phase9_dialogs.py` — the workflow dialogs: `TranscriptExportDialog`,
  `ArchiveExportDialog`, `ArchiveImportAdmissionDialog`, `ContinuationResolutionDialog`,
  `BackupCreationDialog`, `BackupVerificationDialog`, `RestoreHandoffDialog`, plus the
  GUI-visible `RestoreHandoffResultDialog` for the post-close child outcome;
- `src/bots5/desktop/phase9_queue_dock.py` — `ImportQueueDockWidget` with the bounded refresh
  path and the queue controls (cancel, remove waiting, reorder, retry, clear history).

`window.py`/`widgets.py` received thin wiring only (menus, dock registration, chat-rail context
actions, the imported-continuation readiness banner and send/regenerate intercept; imported-user
edit is deliberately not intercepted). `bootstrap/desktop.py` received the runtime-owned
restore-handoff plumbing: `RestoreHandoffRequest`, `RestoreHandoffRegistry`,
`RestoreHandoffCapability` (register/take/clear/pending plus `request_orderly_close` over
`DesktopRuntime.windows`), `DesktopRuntime.handoff`, `restore_exit_code`, and the post-close
consumer inside `serve()` that — only after a successful `runtime.close()` and authority
release — awaits the pre-store bootstrap child with captured stdout/stderr and presents the raw
receipt or typed refusal and the exact 0/1/2/3 status in a GUI-visible dialog.

The core/application additions are narrow and additive: `write_transcript_export` and
`write_archive_export` (projection commands whose crash-safe writers are offloaded with
`asyncio.to_thread`, mirroring the `create_backup` offload precedent); the in-place offload of
`verify_backup` (signature, return type and error contract unchanged); the flat
`QueuedImportDisplay`/`QueueDisplayPage` value types with the additive read-only
`list_archive_import_display` SELECT and the `queued_import_display` command; and offloaded
admission/retry intake fingerprinting so a slow one-shot source cannot block the qasync loop.
Queue refresh is the sealed bounded poll (at most 1 second while the dock is visible and any row
is non-terminal) plus an immediate reload when the dock becomes visible and after every locally
issued queue command; the optional event-driven acceleration was not adopted and
`desktop/session.py` is untouched.

## What Slice E did not change

- Archive v1/v2 and Backup v1 formats, schemas, identity, version selection, and receipt
  semantics — unchanged (`core/export.py`, `infrastructure/archive_package.py`,
  `infrastructure/archive_v2.py`, `infrastructure/backup_capture.py`,
  `infrastructure/backup_package.py` untouched).
- Import semantics — the queue state machine, CAS/retry/reorder/cancel rules, journal phases,
  DDL/schema and migration identity are unchanged; the migration head remains
  `0012_phase9_archive_import` and Slice E adds no migration. Durable provenance and
  historical source truth are untouched; source/resolver intake arguments never become
  provenance or watched roots.
- Restore identity/semantics — `RestoreService`, `RestoreStartupCoordinator`,
  `DataRootAuthority` and the default-off destructive override are unchanged; Slice E adds no
  `BotsApplication.restore_*` command and never calls restore inside the live session; restore
  remains a pre-store bootstrap operation reached through the landed Slice D path.
- No dependency change (`pyproject.toml` untouched), no Phase 10 surface, no destructive-override
  UI, and no retained-installation cleanup surface. No existing test was modified, weakened, or
  removed; test changes are additive.

## Phase 9 A-E technical contract

**Slice A — interchange: Transcript v0.1 and Archive v1 export.** Readable Transcript v0.1
projection over the active path or full lineage, and strict one-chat Archive v1 export that
refuses a running generation rather than claiming a full-fidelity snapshot of unsettled chat
truth. Landed evidence: `core/export.py`, `infrastructure/archive_package.py`,
`tests/test_phase9_interchange.py`, `tests/test_phase9_archive.py`, and the Slice A closure
report `docs/LINUX_V0_1_PHASE9_SLICE_A_CLOSURE_REPORT.md`.

**Slice B — validated import and durable provenance.** Additive migration
`0012_phase9_archive_import`, validated Archive v1 intake, strict Archive v2
provenance-preserving evolution, fresh local identities with durable immediate-source
provenance, persistent import-queue/recovery state, truthful broken-external-reference handling
with SHA-based healing, and branch-aware imported continuation without provider resurrection.
Landed evidence: `infrastructure/persistence/migrations/versions/0012_phase9_archive_import.py`,
`core/archive_import.py`, `core/import_queue.py`,
`infrastructure/persistence/archive_import_store.py`, `infrastructure/archive_v2.py`,
`tests/test_phase9_import.py`, `tests/test_phase9_import_queue.py`,
`tests/test_phase9_import_recovery.py`, `tests/test_phase9_import_migration.py`, and
`docs/LINUX_V0_1_PHASE9_SLICE_B_CLOSURE_REPORT.md`.

**Slice C — Backup v1 and independent verification.** Whole-installation backup capture under
the data-root authority fence into a strict closed-manifest package, staged re-verification
before publication, typed outcomes including `BackupUncertainPublication`, independent
artifact-only verification without altering live state, and migration recovery points. Landed
evidence: `core/backup.py`, `domain/backup.py`, `infrastructure/backup_capture.py`,
`infrastructure/backup_package.py`, `tests/test_phase9_backup.py`; Slice C is the baseline
parent `81a818a3`.

**Slice D — staged restart restore and destructive failure semantics.** Whole-installation
restore as a durable journal state machine (validate, preserve, stage, verify, fsynced adoption
barrier, single atomic leaf exchange), startup reconciliation of interrupted restores before any
store can open, indefinite operator-directed retention of the displaced installation, the non-UI
`bots5-desktop --restore-from` entry point reporting typed outcomes with exit codes 0/1/2/3, and
fail-closed behaviour under authority or identity uncertainty. Landed evidence:
`infrastructure/restore_service.py`, `bootstrap/desktop.py` (`RestoreStartupCoordinator`,
`_initiate_restore`), `tests/test_phase9_restore.py`, and the restore sections of
`docs/LINUX_V0_1_DESIGN.md`; Slice D is the baseline HEAD
`7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7`.

**Slice E — desktop integration and Phase 9 closure.** The thin-client desktop surfaces,
controller orchestration, and bootstrap handoff described above, binding the landed A-D
capabilities to operator-reachable workflows without moving product semantics into widgets, plus
this closure record. Landed evidence: the four new `src/bots5/desktop/phase9*.py` modules, the
additive projections in `core/application.py`, `core/import_queue.py` and
`infrastructure/persistence/sqlite.py`, `tests/test_phase9_desktop_slice_e.py`, the additive test
sections in the Phase 9 suites, and this report.

## Validation evidence

Baseline-identity reconciliation: the campaign pre-mutation gate verified HEAD
`7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7` and a clean tracked tree before the first mutation;
the T0 baseline regression gate then passed on those exact baseline bytes. OrgMem authority for
both campaigns is revision `7ab91a51ddb3aea626420c9d74dac12adf6e6181`.

Focused supervisor-run gates (junit XML under `work/`; all with 0 failed, 0 errors):

| Gate | Selection | Result | Evidence |
|---|---|---|---|
| T0 baseline identity/regression | landed Phase 9 suites, baseline bytes | **279 passed** | `work/t0-baseline.xml` |
| M1 core projections | same suites, additive projections | **291 passed** | `work/t0-m1.xml` |
| M2 lifecycle/dialogs | Phase 9 suites / desktop suites | **303 passed / 34 passed** | `work/m2-phase9.xml`, `work/m2-desktop.xml` |
| M3 continuation resolution | queue + desktop selection | **91 passed** | `work/m3-gate.xml` |
| M4 backup create/verify | backup + desktop selection | **78 passed** | `work/m4-gate.xml` |
| M5 restore handoff (T3) | restore + desktop selection | **101 passed** | `work/m5-gate.xml` |

Final complete-repository T4 against the frozen candidate (`work/t4-final.xml`, complete
testsuite element): **1409 tests, 0 failures, 0 errors, 1 skipped**, exit `0`. The sole skip is
`tests/test_phase3_local_qwen.py::test_opt_in_local_qwen_acceptance_path`, the opt-in
local-provider acceptance probe whose activation variables are deliberately unset; no provider or
network activity occurred. The recorded counts are the JUnit XML values for the run against
exactly these candidate bytes.

T4 candidate binding: this T4 was executed against the complete candidate — the frozen
production and test surface together with the final documentation set recorded in this report.
No production, test, or documentation byte changed after it. Its counts are the XML counts of
that run, and the two documentation files edited after the earlier M5-bytes run (`docs/ROADMAP.md`
and `docs/ARCHITECTURE.md`) were re-edited so that every landed assertion they carry still holds;
the previously predicted roadmap-test failure therefore does not occur and is not carried as a
limitation below.

## Known limitations and deferred items

- FORK-1 (retained-installation cleanup surface) is deferred: no operator surface for removing a
  retained pre-restore installation exists in the CLI or the desktop, Slice E adds none, and
  `RestoreService.remove_retained_installation` remains reachable by no shipped entry point, so
  that removal remains unavailable pending Mick's explicit adjudication of Option A.
- Stale-but-untouched documentation: `docs/OPERATING_PROCEDURE_V1.md`,
  `docs/OPERATING_PROCEDURE_V2.md`, `docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md`, and
  `docs/adr/**` still describe pre-Slice-E state and were deliberately not edited, because they
  lie outside the sealed mutation fence; they are recorded here rather than silently rewritten.
- Documentation truth-rule conflict (R-M6-1, resolved inside the fence): the sealed closure
  strategy's verification step asks that the changed documentation contain no 40-hex string other
  than the three candidate identities. That literal rule cannot be satisfied together with the
  landed test `tests/test_phase8_inspection.py::test_roadmap_current_state_includes_landed_phase9_slice_b`,
  which pins the already-landed Slice B commit `9a84d38b6ad2d3968db58f471d53bf85820656b1` in
  `docs/ROADMAP.md`, and it would also delete true historical landing identities while the same
  strategy separately requires that landed history not be rewritten. The conflict was resolved in
  favour of the strategy's operative truth rule — no *future* commit, push, merge, or landing fact
  may be claimed — so the status documents retain every historical landed identity and add only
  the truthful Slice C/D landed and Slice E pre-commit status. The landed roadmap-pinning test
  passes unchanged, and no test was edited, weakened, or removed.
- No automatic application relaunch is claimed: after the restore handoff completes, the
  operator must relaunch the application; commit finalisation happens on a later normal startup,
  rollback requires a restart, and the result dialog states exactly that.
- Route deviations actually taken:
  - R-M4-1: a pre-existing landed defect was found and repaired inside the fence —
    `BotsApplication.create_backup` offloaded with a plain `asyncio.to_thread` that copied the
    caller's authority grant into the worker thread, so the landed command could never complete
    against a real data-root authority ("effect grant belongs to another executor"); the command
    now uses the code base's landed fresh-context offload pattern. Signature, return type, error
    contract and progress/cancellation semantics are unchanged.
  - R-M1-1: the private archive-import drain task is now created with a fresh context so a drain
    started from inside a command cannot inherit the caller's effect grant (required for the
    sealed admission/retry offload to be correct).
  - R-M6-1: the sealed documentation verification step's literal 40-hex rule conflicts with the
    landed roadmap-pinning test and with landed-history preservation; resolved by retaining all
    true historical landed identities and claiming no future landing fact (see the known
    limitations section).
  - The queue dock uses the bounded at-most-1-second poll (plus immediate reload on visibility
    and after locally issued commands) as the refresh path; the event-driven acceleration is
    optional and was not adopted, leaving `desktop/session.py` untouched.
  - The sealed fence expected an inert-by-default post-close slot in `main()`; the implementation
    made `serve()` a module-level function with a verbatim-identical body so the post-close
    consumer is directly reachable by the production-reachability proof.
- No separate Slice C or Slice D closure report exists in `docs/`; their records are the landed
  source, the restore sections of `docs/LINUX_V0_1_DESIGN.md`, the Slice C/D subsections added
  to `docs/ARCHITECTURE.md` by this candidate, and the preserved `work/` campaign evidence.

## Landing statement

Phase 9 technical closure is completed in this candidate. The Phase 9 Slice A-E technical
contracts are implemented and validated locally against the candidate bytes documented above.
The final Git commit identity for this candidate, any push or merge, and the OrgMem landing
record are subsequent administrative governance facts, reserved to Mick's separate acceptance
and commit/push authority; this report claims none of them and grants no authority to begin
Phase 10.
