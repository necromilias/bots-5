# B.O.T.S. Linux v0.1 Phase 8 closure report

Date: 2026-09-15 Australia/Melbourne

## Disposition

Linux v0.1 Phase 8 is closed and landed.

This closure report is administrative documentation added after product landing. It is not part of
Candidate E's product seal and cannot meaningfully self-record the future documentation-only commit
that first contains it.

## Accepted product identity

- accepted Candidate E manifest SHA-256:
  `542ffe3a1a6384d229c3c93f63c3d0930ea399cd952776b0516ba06f974ab6ea`;
- landed product commit:
  `f72e0ea6e10694972f3b3455d973651629de448a`;
- parent:
  `6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`;
- commit subject: `Implement Phase 8 inspection and provenance UX`.

## Independent closure review

Fresh Sol/High read-only review of exact Candidate E returned PASS with no concrete current Phase 8
blocker. The final repair resolves the shared-ancestor branch-coherence defect: when a selected user
has regenerated assistant siblings, inspection first resolves the active or historical branch and
keeps only attempts whose assistant identity belongs to that branch. Chat-level history remains able
to show ordinary sibling attempts when no message is selected.

## Final pre-commit/T4 gate

The exact accepted Candidate E passed the final full-repository T4 gate:

- **1082 passed**;
- **1 skipped**;
- **3124 warnings**;
- **3946.05s (1:05:46)**;
- exit **`0`**;
- Candidate E remained byte-identical;
- `git diff --check` passed and the index was empty;
- no external/provider/credential contact occurred.

The reason for the one skipped test is not recorded here because the durable closure evidence does not
identify it.

## Landing

Mick granted substantive acceptance, followed by separate commit approval. The product was committed
locally as `f72e0ea6e10694972f3b3455d973651629de448a`. Separate push approval then landed it on
`origin/main`. Local and remote `main` were verified equal at
`f72e0ea6e10694972f3b3455d973651629de448a`, with `0/0` divergence.

## Current Linux v0.1 boundary

Phases 1 through 8 are landed. Phase 9 — import/export, backup, verification, and restore — is the
current implementation phase.

Earlier Phase 8 implementation reports, Candidate B/C/D BLOCKED findings, uncommitted states, and
provisional candidate seals remain historical evidence. They do not override this closure disposition.

Phase 8 itself provides inspection/provenance UX over already durable request-time and Phase 6
context/attachment facts. It does not provide Phase 9 import/export or provenance backfill.
