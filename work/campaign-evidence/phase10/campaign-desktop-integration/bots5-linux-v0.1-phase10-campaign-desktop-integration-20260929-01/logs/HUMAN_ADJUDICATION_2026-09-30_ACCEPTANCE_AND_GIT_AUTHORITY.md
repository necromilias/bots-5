# Human adjudication — Phase 10 substantive acceptance and git authority

Date: 2026-09-30
Authority: Mick (direct human)
Subject: Phase 10 candidate v6 — `PHASE10_CANDIDATE_SEAL_v6.json`,
candidate_id `d5c433073332b9a0c0de3eaf7dc93d8550168348f8f0603c0e888515181f2946`,
seal sha256 `36078b8838dd1d6266e732e6161ce1bbb7979bb64e9328de4d2957a1cd103197`

Persisted verbatim. Nothing in this file paraphrases or narrows the adjudication.

## Ruling

> Substantive acceptance granted for Phase 10 candidate v6.
>
> Human adjudication of the five open items in §8 of COMMIT_BOUNDARY_REPORT.md:
>
> 1. Candidate v6 is substantively ACCEPTED with the recorded limitations.
>
> 2. The reconstructed MiMo #6 outcome is acceptable evidence for this campaign.
>    - Preserve the distinction that no oracle-authored final report exists.
>    - Do not rewrite the reconstruction as a signed/oracle-authored verdict.
>    - The surviving transcript evidence shows the oracle completed all nine verification
>      milestones before upstream timeout during report emission.
>    - No seventh oracle is required or authorized.
>
> 3. F-2 and F-3 are accepted as non-blocking test debt for this release.
>    - F-2: V-2 rerun-synthesis independence is not fully regression-discriminated.
>    - F-3: legacy-v1 freshness behavior is not fully regression-discriminated.
>    - Do not modify candidate v6 to address either item now.
>    - Preserve both limitations accurately for later hardening/torture work.
>
> 4. The GLM accounting overrun is accepted as a recorded supervisor/process failure.
>    - Preserve the historical fact that the earlier ceiling was exceeded before later
>      Mick-authorized amendment.
>    - Do not launder or rewrite that history.
>    - Final amended usage of GLM 14/14 and total launch budget 36/36 is accepted.
>    - This does not block candidate acceptance.
>
> 5. Git authority:
>    - STAGING: AUTHORIZED.
>    - COMMIT: AUTHORIZED, subject to successful staged-boundary verification.
>    - PUSH: NOT AUTHORIZED.
>
> Proceed only with landing the already-accepted Phase 10 candidate v6.

## Required procedure (verbatim)

> A. Do not make any further candidate-changing source, test, documentation, dependency, OrgMem,
>    ref, Phase 11, or Phase 12 changes.
>
> B. Stage exactly:
>    - the accepted Phase 10 candidate v6 files inside the authorized fence; and
>    - the authorized Phase 10 campaign evidence required to preserve the truthful campaign record.
>
> C. Before committing, verify the staged state against the accepted boundary:
>    - HEAD remains the expected pre-commit baseline until commit.
>    - candidate v6 seal remains exact.
>    - no candidate drift.
>    - staged source changes remain exactly within the accepted 9 modify / 8 add fence.
>    - zero-diff protected areas remain untouched.
>    - no unrelated files are staged.
>    - campaign evidence remains truthful, including:
>      - reconstructed MiMo #6 provenance;
>      - F-2/F-3 limitations;
>      - GLM accounting history;
>      - final T4 = 1504 passed, 1 skipped, exit 0;
>      - launch budget = 36/36;
>      - MiMo = 6/6;
>      - GLM = 14/14.
>
> D. If staged verification differs materially from the accepted v6 boundary, STOP and report to
>    Mick. Do not repair, reseal, restage around the discrepancy, or widen authority.
>
> E. If staged verification passes, create the Phase 10 commit.
>
> F. After commit:
>    - verify resulting HEAD and tree;
>    - verify working/staging state as appropriate;
>    - report the exact commit hash;
>    - report what was committed;
>    - report any remaining untracked or intentionally excluded evidence/state;
>    - confirm that no push or other ref mutation occurred.
>
> Then STOP at the committed-but-unpushed boundary.
>
> Do not push.
> Do not begin Phase 11.
> Do not begin Phase 12.
> Do not mutate OrgMem.
> Do not perform opportunistic cleanup.
> Do not reopen F-2/F-3.
> Do not launch additional workers or oracles.
>
> If the accepted candidate cannot be landed exactly as authorized, stop for Mick rather than
> improvising.

## Standing constraints carried into the landing

- The reconstruction at `reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v3_SUPERVISOR_RECONSTRUCTION.md`
  remains labelled as a supervisor reconstruction. **No oracle-authored final report exists**, and
  none may be fabricated or implied.
- F-2 and F-3 remain open, accurately described, and unrepaired.
- The GLM overrun history in `reports/MODEL_USAGE_LEDGER.md` and
  `logs/BUDGET_AMENDMENT_2026-09-30.md` is preserved as-is, including the fact that the original
  ceiling was exceeded before the amendment.
- PUSH is not authorized; no ref mutation beyond the authorized commit is permitted.
