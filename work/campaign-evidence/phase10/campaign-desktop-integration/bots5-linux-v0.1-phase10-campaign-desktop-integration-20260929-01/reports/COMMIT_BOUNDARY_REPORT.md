# Phase 10 — pre-commit boundary report

Campaign: `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01`
Parcel: `parcel-v2` (immutable), manifest sha256
`45fd91f3f383620e4da7575c7a5ba92abf9298ed47c72cf7eb30a315c854391b`
Design of record: `DESIGN_SEAL_v4` sha256
`9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1` (unmodified; all four design
seals verify)
Baseline HEAD: `0756904481ae884bb9e864e8e1e11fc4a27a72ff`

**State: STOPPED AT THE SEALED PRE-COMMIT BOUNDARY, pending Mick's substantive acceptance.**
Nothing is staged, committed, pushed or ref-mutated. `git diff --cached` is empty; no OrgMem
mutation occurred; no dependency changed; no Phase 11/12 work was performed.

## 1. What was delivered

A thin native desktop operational surface over the existing headless campaign engine, plus the
engine evidence-model work the surface requires: attempt-addressed evidence, one-shot preflight
approval, truthful cancellation, derived dual cost accounting, explicit worker regeneration and
synthesis rerun, and headless CLI parity.

Fence compliance is exact. The nine `modify` files are modified and the eight `add` files exist as
new untracked files; every zero-diff guard is byte-identical to HEAD, proven from git rather than
from comments: `core/application.py`, `paths.py`, `manifest.py`, `providers/**`, `pyproject.toml`,
`db/migrations/**`, `evidence/**`, `examples/**`, and the Phase 9 desktop.

## 2. Candidate seals

| Seal | Carries | candidate_id | sha256 | Final gate |
|---|---|---|---|---|
| v1 | reviewed baseline | `2d20fe9f…76cdd2` | `260f5a9b…c3b18e` | T3 1479 |
| v2 | repair waves 1–2 | `154cfca3…247bbb` | `6db93b97…a5d295` | T4 1492; **FAILED** by oracle #1 |
| v3 | repair wave 3 | `094e05ac…0888b1` | `d31fb991…2a2e315` | T4 1484 — superseded (test loss) |
| v4 | recovered suite | `0b046836…6ffc17` | `02fbcce4…a29902` | T4 1495; **FAILED** by oracle #5 |
| v5 | wave 4 code repairs | `bf522194…4b25ae` | `ad56b9bf…ba17b6` | sealed; T4 killed (V-5 unguarded) |
| **v6** | **wave 4 + complete guards** | `d5c43307…1f2946` | `36078b88…103197` | **T4 1504; judged PASS_WITH_LIMITATIONS by oracle #6** |

`candidate_id` is the sha256 of the canonical JSON of the 17 fenced file digests. Seal v6
re-verifies against the worktree with zero file drift and zero fence violations.

## 3. Validation

Full record, exact commands, environment constraints and counts: `validation/PHASE10_T4_VALIDATION_RECORD.md`.

| Gate | Candidate | Result |
|---|---|---|
| T3 impact-based (complete suite) | v1 | 1479 passed, 1 skipped, exit 0 |
| Final T4, serial | v2 | 1492 passed, 1 skipped, exit 0 |
| Final T4, serial | v3 | 1484 passed, 1 skipped, exit 0 — superseded |
| Final T4, serial | v4 | 1495 passed, 1 skipped, exit 0 |
| **Final T4, serial (authoritative)** | **v6** | **1504 passed, 1 skipped, exit 0 (1:32:23)** |
| Phase 10 fenced suite | v6 | 96 passed, exit 0 |
| Desktop and engine guard set | v6 | 125 passed, exit 0 |

Canonical environment: `QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python` (Python
3.14.7). `.venv` (3.12.13) embeds a private SQLite that cannot load the rooted VFS and yields
roughly 1100 spurious failures, reproduced on pristine HEAD; that is an interpreter mismatch, not a
Phase 10 defect. Every number above is from `.venv314`, and non-zero collection was proven for every
named gate.

## 4. Independent review, and what it changed

### 4.1 Oracle #1 (v2): the inert surface

The first reserved implementation oracle FAILED candidate v2 with a CRITICAL finding: the dock
stored its `bridge_factory` and never called it, so the entire desktop surface was inert. Two
reviews and a green 1492-test suite had missed it, because no test instantiated the dock. Repair
wave 3 fixed it.

### 4.2 Repair wave 3 destroyed the lifecycle test file

The final T4 on v3 returned 1484 — eight fewer than v2. Chasing the arithmetic showed repair wave 3
had **replaced the entire lifecycle test file** instead of extending it, destroying eleven contract
tests. The supervisor recovered them verbatim from the DSH session transcripts (preserved in
`logs/recovery/`) and merged them with the three new tests. Lesson recorded: a falling
total-collected count is a first-class failure signal.

### 4.3 Oracle #5 (v4): the dead Approve path

The second reserved oracle FAILED candidate v4 — 1 CRITICAL, 1 HIGH, 4 MEDIUM, 5 LOW, every one
classified in-fence repairable, **no semantic or fence problem**. It independently confirmed the
D-1 repair, confirmed the 1495 T4 was attributable to exact v4, confirmed no tracked drift, and
falsified the first oracle's qasync suspicion by executing all five `create_task` sites under a real
event loop.

| Finding | Sev | Disposition |
|---|---|---|
| V-1 Approve always called `approve_and_start`, so a prepared regeneration or synthesis rerun was always refused — obligations 9 and 11 unreachable | CRITICAL | **Fixed**: `_on_approve` dispatches by operation. Guard added and mutation-proven. |
| V-2 Regenerate/Rerun-synthesis disabled for every `succeeded` stage, i.e. dead on every row of a successful run | HIGH | **Fixed**: Regenerate offered for any non-running selected stage; rerun is run-level. |
| V-5 a run cancelled before synthesis dispatch read `UNVERIFIABLE` with a warning falsely asserting a *dispatched* attempt | MEDIUM | **Fixed**: Rule 1 also keys on an explicit `started_at: null`, with a version guard. Guard added and mutation-proven. |
| V-6 (D-12) metadata written before the output mirror could advertise `succeeded` with an unreadable artifact | LOW | **Fixed**: output precedes metadata on the `create=False` path; `create=True` ordering preserved. Fault-injection guard added. |
| V-3 D-2 guard was an `inspect.getsource` string assertion | MEDIUM | **Fixed**: replaced by a behavioural dialog test. |
| V-4 the two window tests were still vacuous; attach and T0.8 drain untested | MEDIUM | **Fixed**: both rewritten to build a real `MainWindow`; attach and bounded-drain tests added. |
| V-10 `inspect --attempt`, `regenerate|rerun-synthesis --approval` untested | MEDIUM | **Fixed**: four CLI tests added. |
| V-7/V-8/V-9 (D-10, D-11, 200 px output cap) | LOW | Accepted as documented limitations. |
| V-11 documentation contradiction ("eight" vs "eleven" destroyed) | LOW | **Corrected** in both documents. |

Wave 4 used the two GLM implementation launches Mick granted, plus supervisor work. Both new
guards were **mutation-tested**: each FAILS against the pre-repair behaviour and PASSES against the
fix, with the source restored byte-identically.

### 4.4 Oracle #6 (v6): no candidate-changing defect — but it died before reporting

The sixth and final launch ran the full assignment against v6, **completed all nine of its
verification milestones**, and then died while writing its report: five consecutive
`Upstream idle timeout exceeded` failures. The workflow returned null.

**No oracle-authored report exists.** Its executed evidence and conclusions were recovered from the
run's transcript and are preserved as a clearly-labelled
`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v3_SUPERVISOR_RECONSTRUCTION.md`, with the run's own
scratch directory at `logs/demo-scratch/.oracle-v3/`. That document is **not** an independent
artifact and must not be cited as one.

What it established, by execution:

- Seal v6 verifies with zero drift; HEAD unchanged; exactly nine fenced files modified; nothing
  staged; eight fenced adds; **0 files outside `work/` modified** since the T4 began.
- The dock still binds its bridge exactly once, and the V-1 operation-dispatched Approve path works.
- The qasync probes re-run clean (`SITES FAILING UNDER QASYNC: NONE`).
- Its scratch copy reached a faithful baseline of 96 passed, identical to the worktree.
- **No CRITICAL or HIGH defect, and no candidate-changing defect of any severity, was found.**

It raised two test gaps:

- **F-2 (MEDIUM, TEST_GAP)** — re-coupling Rerun-synthesis to the selected worker row's state
  (re-introducing half of V-2) leaves all 96 tests passing: the V-2 repair is only half guarded.
- **F-3 (LOW, TEST_GAP)** — making version-1 freshness return `None` leaves the suite green, so
  `test_v1_run_no_version_2_marker_reads_as_legacy_unverified` does not discriminate; the V-5
  version guard is likewise unexercised.

On that evidence the outcome is **PASS_WITH_LIMITATIONS**. That verdict is the supervisor's reading
of the run's own outputs and reasoning, not a statement the oracle signed.

## 5. Honest limitations

1. **F-2 and F-3 are open and deliberately unrepaired.** Both are test gaps rather than defects.
   Repairing them would change the candidate to v7 and invalidate the 1504-test T4, and **no launch
   remains to re-oracle a v7** — so acting on them would leave the final candidate with no
   independent verification at all.
2. **The final oracle's verdict is reconstructed, not authored.** The run completed its checks but
   died before writing its report. Mick should weigh a reconstructed verdict differently from a
   signed one.
3. **No real GUI session and no real provider call** were exercised anywhere. All review and
   validation used deterministic fakes and the offscreen Qt platform. Operator-supplied rate
   currentness cannot be machine-verified by design.
4. **D-10, D-11 and the 200 px output cap** (oracle #5's V-7/V-8/V-9) are accepted, documented
   limitations.

## 6. Budget and process deviations (recorded, not concealed)

Full detail in `reports/MODEL_USAGE_LEDGER.md` and `logs/BUDGET_AMENDMENT_2026-09-30.md`.

- **The GLM 5.3 Flash family cap was exceeded.** Cap 8; this campaign used **14** (2 design + 12
  implementation). The cap was reached at the end of M0.3b; four further launches happened because
  per-family caps were not re-checked before each launch. Mick subsequently raised the allowance to
  14 explicitly, folding the overage into an amended ceiling rather than treating it as a second
  breach. **This is a supervisor accounting failure** and is recorded as such.
- **Two launches were spent and produced nothing**: the Qwen3.8 falsification returned null twice
  (completed instead on GPT-6 Luna), and one workflow dispatch of the wave-4 GLM workers failed
  before any child session existed because the script returned un-awaited promises; that attempt was
  re-dispatched and is recorded as not consumed.
- **Launch 36 was consumed without producing a report**, as described above.
- **Final accounting: 36 of 36 launches used** (MiMo 6/6, GLM 14/14). No launch remains.

## 7. Evidence reconciliation

`PACK_MANIFEST.json` is the **untouched issuance record**: its 38 entries all still hash-match with
zero mismatches, and it is referenced as such by `REMOTE_RUN.md`,
`logs/PREFLIGHT_VERIFICATION.md` and `prompts/SUPERVISOR_LAUNCH.md`. It was deliberately not
rewritten. All later artifacts are indexed with sha256 and byte counts in
`EVIDENCE_INDEX_FINAL.json`, which is additive and excludes itself.

| Check | Result |
|---|---|
| Issuance manifest hashes | 38 of 38 match, 0 mismatches |
| Parcel-v2 integrity | `PARCEL_V2_MANIFEST.sha256` verifies |
| Design seals v1–v4 | all four verify |
| Candidate seal v6 | verifies; zero file drift after the final T4 |
| Mutation fence | exactly the 9 `modify` files changed; 8 `add` files present; zero guards broken |
| Staging state | `git diff --cached` empty; nothing staged, committed, pushed or ref-mutated |

## 8. What Mick is asked to decide

1. **Substantive acceptance of Phase 10** against design seal v4 and the adjudications, at
   candidate **v6**.
2. Whether a **reconstructed oracle verdict** (oracle #6 completed every check but died before
   writing its report) is acceptable evidence, or whether a budget amendment should authorize one
   further oracle.
3. Whether **F-2 and F-3** — both test gaps, neither a defect — are acceptable for this release.
4. Whether the **GLM family overrun** (now folded into the amended ceiling) is accepted as a
   recorded deviation or requires another disposition.
5. Whether to **stage, commit and push** — all of which remain unauthorized and unperformed.

No staging, commit, push, ref mutation, OrgMem mutation, dependency change or Phase 11/12 work was
performed. The mutation fence was never expanded.
