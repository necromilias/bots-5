# MiMo final implementation oracle #6 (candidate v6) — SUPERVISOR RECONSTRUCTION

**Status of this document: RECONSTRUCTED, not authored by the oracle.**

The reserved sixth launch (MiMo V2.6 Flash, the last authorized launch) ran the full oracle
assignment against candidate v6 and **completed all nine of its verification milestones**, then
died while writing its own report. Its session shows 92 steps, a final todo list of
"9 completed, 1 in progress", and then five consecutive upstream failures —
`Upstream idle timeout exceeded (TIMEOUT)` — while its reasoning said: *"Now write the report file.
It's long; I'll write it in one go."* The turn ended in error and the workflow returned null.

`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v3.md` therefore **does not exist**, and no
oracle-authored verdict line exists anywhere. Everything below is reconstructed by the supervisor
from that run's session transcript (tool calls and their outputs, and the agent's own reasoning).
It is evidence of what the oracle executed and concluded, but it is **not** an independent
document and must not be cited as one. Where the oracle reasoned to a conclusion, that is stated
as its reasoning; where it only executed a check, only the observed output is reported.

Requested effort `provider_maximum_if_exposed`: the harness exposes no per-child effort control, so
**effective effort is NOT APPLIED**. (Recorded by the oracle in its own reasoning and mandated by
its brief.)

## Milestones — all completed

| # | Milestone | Status per the run's own todo list |
|---|---|---|
| M1 | Seal verify + git drift/fence proof | completed |
| M2 | V-1 approve dispatch by execution | completed |
| M3 | Guard discrimination (scratch mutation) | completed |
| M4 | V-2 stage controls by execution | completed |
| M5 | V-5 + version regression | completed |
| M6 | V-6 write ordering | completed |
| M7 | Test coverage reality vs v4 baseline | completed |
| M8 | T4 attribution 1504/1/0 | completed |
| M9 | New-defect hunt | completed |
| — | Write the report | **in progress when the run died** |

## Executed evidence

**M1 — seal, drift and fence (executed).**

```
36078b8838dd1d6266e732e6161ce1bbb7979bb64e9328de4d2957a1cd103197  PHASE10_CANDIDATE_SEAL_v6.json
seal sha256 match: True
file drift: none
EXIT=0
```

Final re-verification after all of its own activity: `seal sha256 match: True / file drift: none`,
`HEAD 0756904481ae884b...`, exactly the nine fenced `modify` files present, staged set empty, eight
untracked fenced adds, and **0 files outside `work/` modified since the T4 began**.

**M2 — V-1 by execution.** The dock probe, run against the sealed bytes, showed the bridge factory
called exactly once on Load Job, `dock._bridge` becoming a real `CampaignBridge`, validation
succeeding, Approve enabled after preflight — i.e. the D-1 repair still holds — and the
operation-dispatched approve path exercised.

**M9 — qasync probes re-run.** The comparison against the previous oracle's output ends
`SITES FAILING UNDER QASYNC: NONE`, so the first oracle's `create_task` suspicion stays falsified
under the repaired candidate as well.

**M3/M7 — guard discrimination by mutation, in a scratch copy.** The oracle copied the tree to
`logs/demo-scratch/.oracle-v3/`, discovered its first copy baseline was not green, traced that to a
missing `build/native/libbots5_rooted_sqlite_vfs.so`, copied `build/`, and established a **faithful
copy baseline of 96 passed — identical to the worktree**. It then mutated the copy:

- **MUTATION A** — make version-1 freshness return `None` (losing `LEGACY_UNVERIFIED`):
  **96 passed**, and the legacy-v1 test alone *1 passed*. The suite does not catch it.
- **MUTATION B** — re-couple Rerun-synthesis to the selected worker row's state, re-introducing
  half of V-2: **96 passed**. No test catches it.

Both mutations were then reverted, and the restored hashes were confirmed:
`campaign_dock.py 775fcdcc9e8bc4ab613b076206d3cfe492a577f2b8d3407db63817247eea510f`,
`storage.py 74f7df0f65721f29f5d7b3602531c46e0b5636d9e8c57f68fb88baa94565d0c2` — both identical to
the sealed candidate.

**M8 — T4 attribution.** The oracle reconciled the 1504 passed / 1 skipped / exit 0 run against
candidate v6, including the arithmetic from the v4 baseline.

## Findings

Two findings, both `TEST_GAP`. **No CRITICAL or HIGH defect, and no candidate-changing defect of
any severity was found in the repaired candidate.**

### F-2 — MEDIUM — the V-2 repair is only half guarded

- **Evidence (executed):** MUTATION B above. Re-coupling Rerun-synthesis to the selected worker
  row's state — re-introducing an entire half of the HIGH finding V-2 — leaves **all 96** Phase 10
  tests passing.
- **Why it matters:** the behaviour is correct today (V-2 was verified fixed by execution), but the
  specific repair that made Rerun-synthesis a run-level control has no guard, so it can silently
  regress.
- **Classification:** `TEST_GAP`.

### F-3 — LOW — the legacy-v1 freshness guard does not discriminate

- **Evidence (executed):** MUTATION A above. Making version-1 freshness return `None` leaves the
  suite green and `test_v1_run_no_version_2_marker_reads_as_legacy_unverified` still passing. The
  oracle's reasoning traced this to that test's assertion accepting `None` as well as
  `LEGACY_UNVERIFIED` together with a fixture that yields `None`, so the property its name asserts
  is never observed unconditionally. It also noted the version guard added for V-5 is itself
  unexercised, because no test carries a version-1 record with `started_at: null`.
- **Why it matters:** nothing operator-visible today; the risk is that a future change could stop
  classifying version-1 evidence truthfully without any test failing.
- **Classification:** `TEST_GAP`.

## Verdict

No oracle-authored verdict line exists, because the run died before writing its report. On the
evidence it actually produced — nine of nine milestones completed, every mandated check executed
(including V-1 dispatch, V-2 enablement, V-5 plus the version regression, V-6 write ordering, test
coverage reality, T4 attribution, drift and fence), guard discrimination proven by mutation,
worktree left pristine and the seal re-verified with zero drift, and **only two TEST_GAPs and no
candidate-changing defect** — the outcome is **PASS_WITH_LIMITATIONS**.

That verdict is the supervisor's reading of the run's own outputs and reasoning, not a statement
the oracle signed. Mick is asked to weigh it accordingly.

## Consequence

Under the standing rule — "if launch #6 returns PASS/PASS_WITH_LIMITATIONS with no
candidate-changing defect, STOP FOR MICK at the same pre-commit boundary" — the campaign stops at
the pre-commit boundary with candidate v6.

F-2 and F-3 are **not** repaired, deliberately. Both are test gaps rather than defects; repairing
them would change the candidate to v7 and invalidate the 1504-test T4, and **no launch remains to
re-oracle a v7**, so acting on them would leave the final candidate with no independent verification
at all. They are recorded for Mick to accept, or to authorize a further bounded round.

## Note on the preserved scratch

The run's scratch directory `logs/demo-scratch/.oracle-v3/` retains every probe script it wrote
(`p1_dock_operator_path.py`, `p3_qasync_create_task.py`, `p4_adversarial.py`, `p5_adversarial2.py`,
`p6_coverage_gaps.py`, `p7_v5_v6_matrix.py`, `p8_v1_freshness.py`, `p9_dispatch_and_hunt.py`,
`independent_seal_check.py`), every captured output (`p1_output_v6.txt`, `p3_..._v6.txt`,
`p4_adversarial_v6.txt`, `p5_adversarial2_v6.txt`, `p6_coverage_gaps_v6.txt`, `p7_output*.txt`,
`p8_output.txt`, `p9_output.txt`), the suite captures (`phase10_suite.txt`,
`desktop_regression.txt`, `engine_guards.txt`, `collect_all.txt`) and the mutation logs
(`mut_d2_pre_repair.log`, `mut_v2_pre_repair.log`, `mut_v5_pre_repair.log`, `mut_v6_pre_repair.log`,
`mut_v6_pristine_all.log`, `mutation_results.txt`).

Its 29 MB duplicate copy of the repository tree (`repo/`) was deleted by the supervisor after the
run, to prevent a stale copy of the sources from being mistaken for the deliverable; the worktree
and the v6 seal remain the authority, and the mutation logs record what was done to the copy.
