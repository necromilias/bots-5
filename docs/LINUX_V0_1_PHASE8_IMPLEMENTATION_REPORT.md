# B.O.T.S. Linux v0.1 Phase 8 implementation report

Date: 2026-09-15 Australia/Melbourne

## Scope and status

Phase 8 delivered inspection and provenance UX over durable facts already persisted by Phases 1–7.
It did not implement import/export, backup, verification, or restore; those are Phase 9 work.

This is a retrospective administrative implementation-archaeology report. Historical provisional and
BLOCKED candidates remain historical evidence with their original identities and dispositions. They
are not rewritten as later successes. This report does not alter the accepted Candidate E product
identity.

The Phase 8 baseline was the landed Phase 7 commit
`6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`.

The durable repository evidence consulted for this report was the landed Git state at
`f72e0ea6e10694972f3b3455d973651629de448a`, the current repository documents, the Phase 6 closure
report, the Phase 7 implementation report, and the landed Phase 8 core, desktop, persistence, and
integration-test paths. The available `work/**` inventory was checked; it contains preserved campaign
material but no separately named Phase 8 candidate/review report was present. The chronology and exact
candidate seals below therefore retain the supplied Phase 8 evidence identities and locked facts
without inventing unavailable artifact details.

## Accepted boundary and architecture

The accepted boundary was a read-only Details/Inspector surface over authoritative persisted state:

- `BotsApplication.inspect_chat()` owns branch resolution, selected-message coherence, and the query
  passed to presentation;
- the typed `InspectionProjection` and `InspectionField` model the display-safe result;
- Qt presents the projection and does not interpret raw versioned request snapshots;
- per-message and chat-level inspection expose request-time provider, model, settings, provenance,
  generation, lineage, and Phase 6 context/attachment facts where durable evidence supports them;
- legacy, supported, future, and corrupt request-snapshot states are represented honestly, with no
  secret or fabricated provenance exposure;
- attachment inspection reads metadata only and does not read attachment payload bytes;
- active and historical paths retain exact message/attempt branch identity, including regenerated
  shared ancestors; with no selected message, ordinary chat-level sibling history remains available;
- Inspector visibility, selected message, and historical leaf restoration use additive migration
  `0011_phase8_inspector_state` after `0010_phase7_search_navigation`, with stale identities falling
  back safely;
- import provenance is `unavailable (Phase 9 not implemented)` and export provenance is
  `not applicable (Phase 9 not implemented)`.

No provenance backfill or fabrication was added. Current-state documentation was corrected without
rewriting historical phase reports.

## Discovery and implementation archaeology

The existing desktop Details interaction was retained, but its raw snapshot interpretation belonged in
the core rather than in Qt. The implementation added a typed core projection and routed the desktop
Inspector through it. Historical request snapshots are validated according to their known format;
versionless records are kept within their closed legacy boundaries, unknown future versions are not
trusted as provenance, and malformed data is shown as corrupt rather than partially rendered.

The persistence surface supplies message and attempt attachment metadata separately from payload reads.
The application first resolves a valid historical leaf or the authoritative active path. For a selected
message it then retains only attempts whose resulting assistant identity belongs to that resolved branch
and whose user/assistant identity matches the selection. This is the branch-coherence rule needed for
regenerated siblings sharing one user ancestor. A missing or stale saved leaf is advisory and falls
back to the active path; an incoherent selected identity is not silently paired with another branch.

## Candidate chronology

The candidates below are distinct historical checkpoints. Earlier green targeted validation did not
override a later independent blocker for the same candidate.

### Provisional Candidate A

Manifest SHA-256:
`61f7795251153a82df1869067153ae397bfd4e2fad553453ae48dcb58e952f46`

This initial partial candidate passed focused T0. Broader validation was BLOCKED by the validation
runtime/environment precondition. Candidate A was not independently accepted.

### Candidate B

Manifest SHA-256:
`0b48c255f26e7259e0faf2a9c11d2a724366adda7c51e4e315451618263b21fd`

Environment/runtime recovery allowed T0–T3 to complete. Fresh independent review nevertheless BLOCKED
Candidate B on four findings:

- F1: legacy/future snapshot handling could expose secret-shaped data and fabricate provenance;
- F2: a stale restored Inspector identity could blank an otherwise valid transcript;
- F3: migration-journal source/target ordering was not strict enough;
- F4: current-state ROADMAP wording contradicted the actual phase position.

Candidate B's earlier green validation remains evidence of those checks at that checkpoint, not
acceptance.

### Candidate C

Manifest SHA-256:
`93d49d8f2dbe73b4100e453c979ca6809857cd1d79e6d3cbf62de3b2cf005fd9`

Candidate C repaired F1–F4 and added explicit representation that Phase 9 was not implemented. Fresh
Sol review BLOCKED it after finding that Inspector attachment metadata queries read payload bytes and
that historical leaf, message, and attempt branch identities could still be independently combined
across branches.

### Candidate D

Manifest SHA-256:
`331bac84ef2e59164c9bd4681905a116bfbaf6560e686f397c1dd1b0bb4d9969`

Candidate D closed the attachment-payload-read defect and the direct cross-branch selection defect.
Its campaign-ending review still BLOCKED it: selecting the shared user ancestor of regenerated
assistant siblings projected attempts from both sibling branches rather than only the attempt or
attempts coherent with the resolved historical or active path. The authorized repair waves were
exhausted and the campaign correctly stopped. Candidate D remains BLOCKED history.

### Candidate E

Manifest SHA-256:
`542ffe3a1a6384d229c3c93f63c3d0930ea399cd952776b0516ba06f974ab6ea`

Candidate E was a narrow, human-authorized continuation from exact Candidate D. It changed only
`src/bots5/core/application.py` and `tests/test_phase8_inspection_integration.py`. The repair filters
selected-message attempts by the assistant identities in the resolved branch, including the shared
ancestor/regenerated-sibling case, while preserving unfiltered chat-level history when no message is
selected.

Fresh Sol / High read-only review returned PASS against exact Candidate E with no concrete current
Phase 8 blocker. Candidate E remained byte-identical during that review. Mick then granted substantive
acceptance. Candidate E is the accepted product candidate represented by the landed product commit.

## Final validation and landing

The exact substantively accepted Candidate E received the explicit T4/pre-commit full-repository gate:

```text
env -u OPENROUTER_API_KEY \
    -u BOTS5_PHASE3_QWEN_BASE_URL \
    -u BOTS5_PHASE3_QWEN_MODEL \
    -u BOTS5_PHASE3_QWEN_API_KEY_ENV \
    PYTHONDONTWRITEBYTECODE=1 \
    QT_QPA_PLATFORM=offscreen \
    PYTHONPATH=src \
    .venv314/bin/python -m pytest -p no:cacheprovider
```

It exited `0` with **1082 passed, 1 skipped, 3124 warnings in 3946.05s (1:05:46)**. The one skipped
test is not identified here because the durable evidence supplied for this closure does not identify
it. Candidate E remained byte-identical at
`542ffe3a1a6384d229c3c93f63c3d0930ea399cd952776b0516ba06f974ab6ea`; `git diff --check` passed and
the index remained empty. No network, provider, credential, Organisational Memory, staging, commit,
or push activity occurred during T4.

After separate Mick commit approval, the product was committed as:

- commit: `f72e0ea6e10694972f3b3455d973651629de448a`;
- subject: `Implement Phase 8 inspection and provenance UX`;
- parent: `6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`.

After separate push approval, local `main` and `origin/main` were verified equal at that commit with
`0/0` divergence.

This report is retrospective administrative evidence. It neither changes Candidate E's product bytes
or seal nor claims Phase 9 functionality.
