# B.O.T.S. Linux v0.1 Phase 9 Slice A closure report

Date: 2026-09-19 Australia/Melbourne

## Disposition

Linux v0.1 Phase 9 Slice A is substantively accepted, committed, pushed, and landed.

Slice A implements readable Transcript v0.1 export and strict one-chat Archive v1 export. It does not
implement Archive v1 import, Backup v1, backup verification, restore, or Phase 9 Qt integration.

## Accepted product identity

- accepted FINAL03 candidate manifest SHA-256:
  `a1261a65b047bb779d4eae7ab1a5ee200c9b43addead1878d3e1c99784d2af0d`;
- final technical seal SHA-256:
  `781ecf9a392dd9ac30ccba533d529c9bc4d1633a7fba9e067fe001b8106f3820`;
- final fresh Sol/High review SHA-256:
  `48b03c0100fc7538a5bbd9329c04592f71fd3364cdf8ce7941f230d2c9dadc69`;
- landed product commit:
  `35b206a404d4cd3e2dd05a5c07ffdc6dd0e1ba40`;
- parent:
  `f658cfeaf0970d1c382978b39453853a08d28ea1`;
- commit subject:
  `Implement Phase 9 Slice A transcript and archive export`.

The accepted candidate comprised exactly eight product/test paths. Campaign evidence under `work/**`
was excluded from the product commit.

## Independent closure review

Fresh Sol/High read-only falsification of exact FINAL03 returned PASS with no concrete current blocker.
Post-review candidate identity remained unchanged. Historical BLOCKED candidates and reviews remain
preserved evidence and are not rewritten as successful attempts.

## Final validation

The exact final candidate recorded:

- T0: **5 passed**, 1 warning, exit 0;
- T1: **52 passed**, 1 warning, exit 0;
- T2: **714 passed**, plus the exact known roadmap baseline failure;
- T3: **52 passed**, 1 warning, exit 0;
- T4: **1133 passed, 1 skipped, 1 failed**, 3124 warnings in 3634.02s.

T4 disposition was **PASS WITH ACCEPTED BASELINE EXCEPTION**. The sole failure was
`tests/test_phase8_inspection.py::test_roadmap_current_state_includes_landed_phase7`, classified
`PRE_EXISTING_BASELINE_TEST_DEFECT`. The sole skip was the opt-in local-Qwen acceptance path with
provider activation variables unset. No other failure was absorbed or deselected.

The stale roadmap assertion is documentation-maintenance debt, not a Phase 9 Slice A product defect.

## Scope and fences

Slice A preserved the existing migration head `0011_phase8_inspector_state`; no migration 0012 or
schema change occurred. No Archive import, backup/verify/restore feature, Phase 9 Qt work, dependency
change, provider/network/credential access, OrgMem mutation, or stale-roadmap repair was included in the
accepted product candidate.

Transcript/Archive export remains non-mutating and does not add durable export-history truth.

## Landing

Mick granted substantive acceptance, then separate commit authority, then separate push authority.

The exact eight-path product candidate was committed as
`35b206a404d4cd3e2dd05a5c07ffdc6dd0e1ba40` and pushed exactly from local `main` to
`origin/main`. Live remote `main` was verified at the same commit with `0 ahead / 0 behind`.
The index and tracked worktree were clean after landing. Local `work/**` campaign evidence remained
untracked and was not pushed.

## Current Phase 9 boundary

Phase 9 Slice A is closed and landed.

Slice B — validated Archive v1 import and durable import provenance — is the next design/oracle and
implementation-planning boundary. Its accepted product semantics are recorded in Organisational Memory
decision 0010 at commit `cd8d8f4638359c68a778ff211329a0c530b29712`.

That decision does not itself authorize Slice B implementation, migration 0012, commit, or push.
Backup/verification, restore, and Phase 9 desktop integration remain later bounded slices.
