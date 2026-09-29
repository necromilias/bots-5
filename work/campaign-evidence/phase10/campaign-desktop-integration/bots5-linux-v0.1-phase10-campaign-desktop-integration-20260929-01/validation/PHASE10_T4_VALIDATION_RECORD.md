# Phase 10 validation record

Per `validation/README.md`: exact command, environment constraints, collected count, exit code,
candidate/seal identity and applicability.

## Environment (binding constraint — read this first)

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest ...
```

`.venv314` is **Python 3.14.7** and is the canonical environment (`docs/DEVELOPMENT.md:89`).
`.venv` is Python 3.12.13 and embeds a private, uv-managed SQLite that cannot register the
rooted VFS, which produces roughly 1100 spurious `RootedVfsUnsupported` failures across phases
1-9. A pristine-HEAD worktree reproduces that count, so it is an interpreter mismatch, not a
Phase 10 defect. **Every number below was produced with `.venv314`.**

`testpaths = ["tests"]` and `addopts = "-q"` (`pyproject.toml`). Passing suites are not rerun
merely to scrape counts.

## Run 1 — impact-based T3 (complete suite, pre-repair)

- Command: `QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest --no-header -p no:cacheprovider`
- Candidate: `PHASE10_CANDIDATE_SEAL_v1`,
  candidate_id `2d20fe9fc10a80af99384e961ed55aa89ce823d38e3bedecbf166d54d376cdd2`,
  seal sha256 `260f5a9bee2b6433f4ea21134d2f0af8d29c3542e33c9ae49f1366c484c3b18e`
- Result: **1479 passed, 1 skipped, 43133 warnings in 5609.66s (1:33:29), exit 0**
- Applicability: impact-based validation. `models.py`, `storage.py` and `errors.py` are imported
  by essentially the whole tree, so the impacted set is the repository and the complete suite is
  the honest impact-based check. **Not** the final T4: the independent falsification raised five
  confirmed defects afterwards, and this run predates those repairs.

## Run 2 — final T4 on repaired candidate v2

- Command: identical to run 1.
- Candidate: `PHASE10_CANDIDATE_SEAL_v2`,
  candidate_id `154cfca37a265e03542021b0a2bd0cd431315ccccf7448353a6f76ca84247bbb`,
  seal sha256 `6db93b97cebcbd5e5087e564e1053cc135a82a6d7b115d9bb81657fd64a5d295`
- Result: **1492 passed, 1 skipped, 44101 warnings in 5246.22s (1:27:26), exit 0**
- Applicability: complete repository suite, serial, against the frozen repaired candidate after
  repair waves 1 and 2 and after the post-repair lifecycle review. Seal re-verified immediately
  after the run with zero file drift. **Superseded**: the reserved final implementation oracle
  subsequently found a CRITICAL desktop-reachability defect (D-1) that this gate could not
  detect, because the suite contained no test that instantiates the campaign dock.

## Run 3 — final T4 on repaired candidate v3 — **SUPERSEDED, do not cite**

- Command: identical to run 1.
- Candidate: `PHASE10_CANDIDATE_SEAL_v3`,
  candidate_id `094e05ac53116cbac5580a99cac93ed3fb68f221133c6efab6a232dc0d0888b1`,
  seal sha256 `d31fb991255cebf0502150e3eabe2bde2671150ed388e24938b678b46da2e315`
- Result: **1484 passed, 1 skipped, 43059 warnings in 5310.98s (1:28:30), exit 0**
- Applicability: **superseded and invalid as a final gate.** The run is green, but its total is
  eight tests below run 2 because repair wave 3 had replaced the whole lifecycle test file
  instead of extending it, deleting eleven contract tests. A green exit code with a fallen
  collected count is not a pass. See `logs/IMPLEMENTATION_LOG.md`, "Repair wave 3 collateral
  damage". The eleven tests were recovered verbatim from the DSH session transcripts and merged
  with the three new widget-level tests; candidate v4 carries the correction.

## Run 4 — final T4 on corrected candidate v4 (authoritative)

- Command: identical to run 1.
- Candidate: `PHASE10_CANDIDATE_SEAL_v4`,
  candidate_id `0b04683646f14ab66b71aa381aa506ffc82e5aa9fbb4e3a29ec9ca65346ffc17`,
  seal sha256 `02fbcce47185dd83645febe14faab5808027f68593de3edfd806441626a29902`
- Result: **1495 passed, 1 skipped, 42591 warnings in 5298.92s (1:28:18), exit 0**
- Applicability: complete repository suite, serial, against the corrected candidate carrying
  repair wave 3 plus the restored lifecycle contract tests. **This is the authoritative final
  gate.** The total is exactly run 2's 1492 plus repair wave 3's three genuine widget-level tests,
  confirming that the eleven destroyed contract tests were restored and that nothing else
  changed. (Precisely: eleven were destroyed and three added, a net of eight fewer.) Seal v4
  re-verified immediately after the run: sha256 match, zero file drift.
- Post-run state: `git diff --cached` empty (nothing staged); exactly the nine fenced `modify`
  files appear as tracked modifications.

## Focused gates run alongside the above

| Gate | Command scope | Result |
|---|---|---|
| Phase 10 contracts | the six `tests/test_phase10_*.py` files | 76 passed, exit 0 |
| Desktop regression | `test_phase9_desktop_slice_e.py`, `test_phase7_desktop.py`, `test_phase1_desktop.py`, `test_desktop_draft1.py`, `test_phase1_core.py` | 62 passed, exit 0 |
| Engine guards | `test_runner.py`, `test_cli_views.py`, `test_audit_blockers.py`, `test_storage_events.py`, `test_manifest.py`, `test_local_provider.py` | 84 passed, exit 0 |

## Collection proof

Non-zero collection was proven for every named Phase 10 gate (collected counts are recorded per
file in `logs/IMPLEMENTATION_LOG.md`). No gate was reported as passing without a non-zero
collected count.

## Run 5 — final T4 on repaired candidate v6 (authoritative)

- Command: identical to run 1.
- Candidate: `PHASE10_CANDIDATE_SEAL_v6`,
  candidate_id `d5c433073332b9a0c0de3eaf7dc93d8550168348f8f0603c0e888515181f2946`,
  seal sha256 `36078b8838dd1d6266e732e6161ce1bbb7979bb64e9328de4d2957a1cd103197`
- Result: **1504 passed, 1 skipped, 37688 warnings in 5543.31s (1:32:23), exit 0**
- Applicability: complete repository suite, serial, against the candidate carrying repair wave 4
  (the four code repairs and the test coverage demanded by the second implementation oracle).
  1504 is exactly run 3-v4's 1495 plus the nine tests added in wave 4 (fenced Phase 10 suite
  87 -> 96: cross-cutting 16 -> 21, lifecycle 14 -> 17, regeneration 15 -> 16).
- Post-run: seal v6 re-verified by the supervisor and independently by the sixth oracle —
  sha256 match, zero file drift. `git diff --cached` empty; exactly the nine fenced `modify` files
  modified; HEAD `0756904481ae884bb9e864e8e1e11fc4a27a72ff`.

## Candidate lineage at the boundary

| Seal | Carries | Final gate |
|---|---|---|
| v4 | D-1 repair wave 3 | T4 1495; **FAILED** by oracle #5 |
| v5 | wave 4 code repairs | sealed, T4 killed deliberately (V-5 had no guard) — superseded |
| **v6** | wave 4 + complete guards | **T4 1504; judged PASS_WITH_LIMITATIONS by oracle #6** |
