# B.O.T.S. 5 First Live Smoke Report

Date: 2026-08-28

Status: **PASS WITH FINDINGS**

Revision tested: `a6686ef713e0963f4ece08020adb9e66c3b32b01` on `main`; remote `main` matched immediately before execution.

Run ID: `bots5-smoke-example-20260828T100709Z-99619249`

Overall state: `succeeded`

Elapsed wall time: approximately 22.74 seconds.

| Stage | Model | State | Duration | Tokens P/C/R/T | Exact cost |
| --- | --- | --- | ---: | --- | ---: |
| extractor | `openai/gpt-5.6-luna` | succeeded | 4.971s | 109/358/130/467 | $0.0004514 |
| analyst | `openai/gpt-5.6-luna` | succeeded | 7.503s | 111/489/54/600 | $0.000609 |
| adversary | `google/gemini-3.7-flash` | succeeded | 11.653s | 110/850/548/960 | $0.001635 |
| synthesis | `google/gemini-3.7-flash` | succeeded | 10.827s | 1062/844/417/1906 | $0.00198075 |
| **Aggregate** |  |  |  | **1392/2541/1149/3933** | **$0.00467615** |

## Key commands

```text
gh repo clone necromilias/bots-5 work/bots-5
git rev-parse HEAD
git ls-remote origin refs/heads/main

UV_CACHE_DIR=... uv venv .venv
UV_CACHE_DIR=... uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest

cp -p examples/example-job.json examples/first-live-smoke-job.json
.venv/bin/bots5 validate examples/first-live-smoke-job.json
fish -lc 'exec .venv/bin/bots5 run examples/first-live-smoke-job.json'

.venv/bin/bots5 status RUN_ID --runs-dir examples/.bots5/runs
.venv/bin/bots5 inspect RUN_ID STAGE_ID --runs-dir examples/.bots5/runs
```

The initial bare-system `pytest` probe could not collect because dependencies were absent. After isolated installation on Python 3.12.13, the repository suite passed: **32 passed in 0.22s**.

Validation returned `OK: bots5-smoke-example` and created no run directory. The copied manifest differed from the example only in the requested analyst and synthesis models; prompts, input, topology, temperatures, token/time limits, cost gate, dependencies, and output semantics remained unchanged.

## Artifact verification

- All 13 expected files exist and are non-empty.
- Four independent stage JSON and Markdown pairs are present.
- `run.json`, `usage.json`, and every stage JSON parse successfully.
- `result.md` is byte-identical to `stages/synthesis.md`.
- `events.jsonl` contains 18 valid events in contract-valid order: run start, four queues, worker start/request/success transitions, synthesis only after all worker successes, then synthesis success and run success.
- Disk-only `status` and all four `inspect` outputs agree with persisted metadata.
- Requested and returned models match for every stage.
- Every provider cost was known and persisted as an exact decimal string; aggregate status is `known`, complete is true, and the unknown-stage list is empty. The live unknown-cost path was therefore not exercised; its partial-cost behavior is covered by the passing unit suite.
- The exact API-key value does not occur anywhere under the run directory. No credential markers were found either.
- Repository history and tracked files remained unchanged during the run; only the requested untracked manifest copy existed.

## Finding

The extractor output exceeded its “explicit facts only” prompt by also producing implication-analysis and adversarial-review sections.

This is a model-instruction-following deviation, not an orchestration, persistence, cost, event-ordering, or security failure. The worker requests remained isolated. The finding is specifically that task-like language contained inside shared input data was not sufficiently separated from worker instruction authority.

No repairs or additional paid runs were performed during the first observation run.

## Result

**PASS WITH FINDINGS**

The live run demonstrated successful real-provider execution, bounded worker concurrency, synthesis gating, durable persistence, event ordering, status/inspect reconstruction, exact provider-reported cost capture, and API-key non-persistence. The remaining finding motivates a bounded V0.1 hardening pass for worker instruction/data separation and contract enforcement.
