# B.O.T.S. 5 V0 Candidate Build Report

## Result

A complete local V0 candidate was integrated from the supplied build brief and worker reports.

This is a candidate implementation only. No Git mutation, publication, deployment, persistent
service creation, or real OpenRouter smoke run was performed.

## Source reconciliation

The supplied V0 Build Brief was treated as the primary implementation contract. Worker reports were
used as specialist review material where they were consistent with it.

Notable resolutions:

- `status` / `inspect`: deterministic default `./.bots5/runs`, plus explicit `--runs-dir` for jobs
  configured elsewhere.
- empty or whitespace-only model completion: stage failure (`empty_model_response`).
- money: `Decimal` internally; exact known costs persisted as decimal strings; unknown stays null.
- events: append-only JSONL with a process-local write lock and fsync.
- artifacts: atomic temp-file + `os.replace` writes.
- stage IDs: filename-safe grammar to prevent model/manifest IDs becoming traversal paths.
- `runs_dir`: explicit job-relative or absolute path is allowed, but filesystem root is rejected;
  writes are confined beneath the resolved configured runs directory. This remains an operator-run
  trust model, not a hostile sandbox.
- skipped synthesis has known zero cost because no provider request was launched.

One supplied file, `Deepseek2.md`, is a continuity/authority-state report rather than the expected
testing specialist blueprint. The mandatory test matrix in the Build Brief and Luna implementation
contract was sufficient to build the test suite.

## Candidate contents

- Python 3.12+ `src/` package
- strict JSON manifest parser and closed-schema validation
- OpenRouter non-streaming provider boundary
- bounded asyncio worker execution
- optional synthesis stage
- per-stage and overall timeouts
- independent stage persistence
- events, run state, usage/cost, status, and inspect
- example three-worker smoke job
- required README, architecture/security/execution/etc. docs
- four ADRs
- unit tests with live-network blocking
- standard-library-only `bootstrap_bots5.py`

Project file count: **44**

## Validation actually performed

- `pytest`: **32 passed**
- example validation: `OK: bots5-smoke-example`
- `python -m compileall`: passed
- bootstrap syntax check: passed
- bootstrap reconstruction: **44/44 files byte-identical**
- package installation: succeeded under the available Python **3.13.5** environment using an
  isolated target and no dependency download
- imported installed package version: `0.1.0`

A Python 3.12 interpreter was not available in the execution environment, so 3.12-specific runtime
verification was not performed.

## Deliberately not performed

- no real OpenRouter request
- no API spend
- no Git init/commit/push/merge
- no deployment
- no V0 approval claim

## Bootstrap SHA-256

`50193994e1063039da08389b459784446c60c296f34b6d652a106e4ec90a2eaf`
