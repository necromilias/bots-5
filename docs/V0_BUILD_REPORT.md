# V0 Build Report

## Result

A complete local B.O.T.S. 5 V0 candidate was integrated from the supplied Build Brief and available
specialist reports.

This is a candidate implementation record, not a V0 approval, deployment record, or production
claim.

## Source reconciliation

The V0 Build Brief was treated as the primary implementation contract. Worker reports were used as
specialist review material where consistent with it.

One supplied worker output, `Deepseek2.md`, was a continuity/authority-state report rather than the
intended testing/failure specialist blueprint. The Build Brief and Luna implementation contract
already contained a comprehensive mandatory test matrix, so the missing report did not require
inventing new product scope.

Key deterministic resolutions in the candidate:

- `status` / `inspect`: default `./.bots5/runs` with explicit `--runs-dir` override.
- Empty or whitespace-only model completion: stage failure (`empty_model_response`).
- Money: `Decimal` internally; exact known cost persisted as decimal strings; unknown remains null.
- Events: append-only JSONL with serialized writes and fsync.
- Files: atomic temporary-file + `os.replace` writes.
- Stage IDs: filename-safe grammar to prevent IDs becoming traversal paths.
- `runs_dir`: job-relative or absolute operator-selected path is accepted, but filesystem root is
  rejected and generated writes remain beneath the resolved configured directory. This is not a
  hostile sandbox.
- Skipped synthesis has known zero cost because no provider request was launched.

## Validation performed

The candidate was exercised locally without making a real provider request:

- `pytest`: **32 passed**.
- `bots5 validate examples/example-job.json`: **passed** (`OK: bots5-smoke-example`).
- `python -m compileall`: **passed**.
- generated bootstrap syntax check: **passed**.
- bootstrap reconstruction: **44/44 project files byte-identical** to the pre-bootstrap candidate.
- package installation: **passed** in an isolated target under available Python **3.13.5** without
  dependency download.
- installed package import: **passed**, version `0.1.0`.

The runtime environment did not contain Python 3.12, so 3.12-specific execution was not independently
verified. The project metadata requires Python 3.12+ and the implementation deliberately avoids
3.13-only syntax.

## Deliberately not performed

- no real OpenRouter request;
- no API spend;
- no deployment;
- no persistent service;
- no Git mutation as part of runtime behavior;
- no claim that V0 is accepted or production-ready.

## Test boundary

Ordinary unit tests use fake or `httpx.MockTransport` providers. `tests/conftest.py` removes
`OPENROUTER_API_KEY` and blocks live socket connections. A real smoke call remains an explicit
operator action after review.

## Campaign context

See `BUILD_CAMPAIGN.md` for the Langflow/OpenRouter fan-out experiment, timeout failures, worker
checkpointing lessons, giant-integrator failure shape, and the method findings carried forward into
B.O.T.S. 5.
