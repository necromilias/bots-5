# B.O.T.S. Linux v0.1 Phase 10 closure and landing report

Date: 2026-09-30 Australia/Melbourne

## Disposition

Phase 10 — native desktop surface over the headless campaign engine — is **technically complete,
substantively accepted, committed, and landed**.

This record distinguishes four separate states that must not be collapsed:

1. **Technical campaign completion** — the accepted design was implemented inside the sealed mutation
   fence and passed its validation and independent review.
2. **Substantive acceptance** — granted by Mick on 2026-09-30 against candidate v6 and the recorded
   limitations.
3. **Commit / landing fact** — landed as commit `9762170099889ecd87d451341a15a29ce7aceae8`, whose
   parent is the Phase 9 Slice E landing commit `0756904481ae884bb9e864e8e1e11fc4a27a72ff`.
4. **Accepted limitations** — carried as recorded, unrepaired items (below).

This record does **not** imply or grant Phase 11 authority. Phase 11 — product finishing and
standalone packaging — is next in the accepted sequence and remains unauthorized.

## Campaign and candidate identity

- implementation campaign: `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01`;
- pre-commit baseline HEAD: `0756904481ae884bb9e864e8e1e11fc4a27a72ff` (Phase 9 Slice E landing);
- accepted design seal: `DESIGN_SEAL_v4`, sha256
  `9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1`;
- accepted candidate: `PHASE10_CANDIDATE_SEAL_v6`, candidate_id
  `d5c433073332b9a0c0de3eaf7dc93d8550168348f8f0603c0e888515181f2946`, seal sha256
  `36078b8838dd1d6266e732e6161ce1bbb7979bb64e9328de4d2957a1cd103197`;
- mutation fence: sha256 `602e6d5d3a88eb893d11d064dfc28c70e8f692c52cb823cd0c8b2711907cf452`;
- candidate file count: 17 (9 tracked modifications plus 8 additions), with no fence violations and
  no out-of-fence tracked modification;
- OrgMem authority referenced by the campaign: `cc0c3c80348b1d798def65988102e0d8ad966730`.

The tracked candidate changed exactly: `bootstrap/desktop.py`, `cli.py`, `desktop/window.py`,
`errors.py`, `events.py`, `models.py`, `runner.py`, `storage.py`, `usage.py` (modify) and
`core/campaign.py`, `desktop/campaign_dock.py` plus six `tests/test_phase10_*.py` files (add). No
dependency, migration, `examples/`, `evidence/`, `work/`, or Phase 9 desktop change was in the fence.

## What Phase 10 added

- **Qt-free campaign seam.** `core/campaign.py` is the sole desktop/campaign boundary and imports no
  Qt. It exposes `CampaignBridge`, `CampaignProjection`/`StageProjection`, `PreparedOperation`, and
  `project_run`. Projections are built only from durable run files; providers are constructed only
  inside an approved dispatch.
- **Native campaign dock.** `desktop/campaign_dock.py` renders projections and issues commands through
  an injected bridge factory. `desktop/window.py` exposes it as a dismissible bottom dock behind a
  View→Campaign action; `bootstrap/desktop.py` supplies the production factory.
- **Attempt-addressed evidence and `selection.json`.** Flat `stages/<stage_id>.att<N>.json`/`.md`
  attempt files with exclusive claims; `selection.json` is the authoritative stage→selected-attempt
  map. Version-1 evidence remains readable and is read-only under the new engine.
- **Preflight snapshot and approval binding.** Full-run, worker-regeneration, and synthesis-rerun
  operations bind to a preflight/operation snapshot digest, scope, and exact target before any run-tree
  mutation or provider request.
- **One-shot approval consumption.** A durable exclusive marker makes approval single-use; a replayed
  or mismatched approval is refused before dispatch.
- **Truthful cancellation.** Cancellation persists terminal state, keeps already-persisted partial
  output authoritative, and preserves provider-side uncertainty; work is never automatically retried.
- **Cumulative versus selected cost/usage accounting.** `usage.json` records cumulative spend across
  every attempt and a read-time derived selected-attempt spend; the selected figure is the authority.
- **Worker regeneration and synthesis rerun.** Explicit, approval-bound operations that add sibling
  attempts and never rewrite historical evidence; synthesis rerun dispatches the frozen messages
  verbatim and selects the new attempt only on success.
- **Synthesis staleness detection.** Mechanical classification (`FRESH`, `STALE`, `UNVERIFIABLE`,
  `NOT_APPLICABLE`, `LEGACY_UNVERIFIED`) against the current selection and dependency output bytes.
- **Headless CLI parity.** `bots5 inspect RUN STAGE --attempt N`, `bots5 regenerate ...`, and
  `bots5 rerun-synthesis ...`; the existing `validate`/`run`/`status`/`inspect` shapes and exit codes
  are preserved.

## Validation

- Authoritative final serial complete-repository T4, candidate v6, canonical offscreen environment:
  **1504 passed, 1 skipped, exit 0** (1:32:23).
- Fenced Phase 10 suite: **96 passed**; desktop/engine guard set: **125 passed**; non-zero collection
  proven for every gate.
- Candidate seal v6 was re-verified after the final T4 with zero file drift; `git diff --cached` was
  empty at the pre-commit boundary.
- Earlier candidate runs were superseded: v2 (1492) was superseded when the reserved oracle found the
  dock was not actually reachable, and v3 (1484) was invalid because a repair wave replaced a test
  file and lost contract tests. Neither is a valid final gate.

## Independent review

Three implementation oracles ran against successive candidates. Oracle #1 rejected v2 (the dock never
invoked its bridge factory, so the surface was inert). Oracle #5 rejected v4 (the approve path always
dispatched a full run, so regeneration and rerun were always refused). Both were repaired inside the
fence and resealed, with mutation-tested guards. Oracle #6 ran the full assignment against v6 and
completed all nine verification milestones, then died on repeated upstream timeouts while emitting its
report. **No oracle-authored final report exists.** Its executed evidence is preserved as a supervisor
reconstruction, explicitly labelled as reconstructed rather than signed; it found no
candidate-changing defect.

## Accepted limitations

These are recorded and deliberately unrepaired; they must not be silently presented as resolved:

- **F-2** — V-2 rerun-synthesis independence is not fully regression-discriminated (test gap, not a
  defect).
- **F-3** — legacy-v1 synthesis freshness is not fully regression-discriminated (test gap, not a
  defect).
- **D-10** — operator pricing entries need not affirm the route (LOW, accepted).
- **D-11** — legacy-mode runs created after Phase 10 carry the additive `attempt_number` key (LOW,
  accepted).
- **200 px output-pane cap** — accepted UI limitation.

## Budget and process deviations

The GLM 5.3 Flash family cap of 8 was exceeded before a later Mick-authorized amendment to 14; that
overrun is preserved as a recorded supervisor accounting failure and is not rewritten. Final
accounting: **36 of 36 authorized child launches used** (MiMo 6/6, GLM 14/14).

## Evidence

The full campaign record is committed under
`work/campaign-evidence/phase10/campaign-desktop-integration/bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01/`:
design and adjudication records, the mutation fence, design and candidate seals, validation records,
review and oracle reports (including the labelled reconstruction), recovery transcripts, the model
usage ledger, and the human adjudication granting acceptance and git authority. `PACK_MANIFEST.json` is
the untouched issuance record; its 38 entries all hash-match.

The final technical disposition on that evidence is **PASS_WITH_LIMITATIONS**, which is the
supervisor's reading of the oracle run's own outputs and reasoning, not a statement the oracle signed.

## Landing statement

Phase 10 candidate v6 was substantively accepted, then separately authorized for staging and commit.
It is commit `9762170099889ecd87d451341a15a29ce7aceae8`, an ancestor of current `main`. The Phase 10
adjudication authorized staging and commit but **not** push; how that commit subsequently reached
`origin/main` is a later, separate governance fact not established by this campaign record. This
record claims no Phase 11 authority.
