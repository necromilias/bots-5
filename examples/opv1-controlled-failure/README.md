# OPv1 Controlled Failure Campaign

This campaign exercises the failure semantics required by the Operating Procedure v1 freeze gate.
It is intentionally designed to fail mechanically in a controlled, interpretable way.

## Revision under test

Harness revision: `de70e4460506623ee119efd5c5d76a57a99ff6bb` (`main` when this campaign was created).
The campaign branch adds only these test assets on top of that harness revision.

## Worker selection

`openai/gpt-5.6-luna` is selected because prior campaign evidence showed reliable bounded-output behavior and exact `stop` completion when given sufficient capacity. Here the worker ceiling is deliberately set to `1` output token, far below the five required bullet points, so the expected provider finish reason is `length` rather than `stop`.

The synthesis stage uses the same model but is expected not to be called because its only dependency should be incomplete.

## Expected behavior

The run is a PASS for this controlled-failure campaign only if all of the following occur:

1. `bounded-worker` reaches provider execution and persists `state=succeeded` with `completion=incomplete` and `finish_reason='length'`.
2. `synthesis` is skipped before any provider request with failure type `dependency_incomplete`.
3. The overall run persists terminal `state=failed` and the `bots5 run` command exits nonzero.
4. `bots5 status` displays the worker completion state and finish reason and exits nonzero for the failed run.
5. Synthesis cost is harness-known zero because no synthesis provider request was sent.
6. No `result.md` is produced.
7. The authoritative run directory remains inspectable from disk with `status` and `inspect`.

Any provider refusal to execute a one-token request, any synthesis request being sent, a run left in `running`, misleading CLI completion output, incorrect skipped-stage accounting, or loss of inspectable artifacts is a campaign failure requiring adjudication before OPv1 freeze.

## Procedure

Validate before the paid request:

```bash
bots5 validate examples/opv1-controlled-failure/job.json
```

Run exactly once:

```bash
bots5 run examples/opv1-controlled-failure/job.json
```

Record the printed run ID. Do not automatically rerun if observed behavior differs from the expected path; inspect and adjudicate first.

Inspect using the exact printed run ID and an absolute `--runs-dir` if the working directory changes.
