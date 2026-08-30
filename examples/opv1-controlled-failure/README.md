# OPv1 Controlled Failure Campaign

This campaign exercises the failure semantics required by the Operating Procedure v1 freeze gate.
It is intentionally designed to fail mechanically in a controlled, interpretable way.

## Revision under test

Harness revision: `de70e4460506623ee119efd5c5d76a57a99ff6bb` (`main` when this campaign was created).
The campaign assets add no harness-code changes on top of that revision.

## Worker selection

`openai/gpt-5.6-luna` was selected because prior campaign evidence showed reliable bounded-output
behavior and exact `stop` completion when given sufficient capacity. The worker ceiling was
deliberately set to `1` output token, far below the five required bullet points, to attempt to exercise
provider `length` completion and the harness `dependency_incomplete` path.

The synthesis stage used the same model and was expected not to be called.

## Intended failure path

The preferred controlled path was:

1. `bounded-worker` returns `state=succeeded`, `completion=incomplete`, `finish_reason='length'`.
2. `synthesis` is skipped before any provider request with `dependency_incomplete`.
3. The overall run persists terminal `state=failed` and exits nonzero.
4. `bots5 status` exposes the incomplete completion and exits nonzero.
5. Synthesis cost is harness-known zero.
6. No `result.md` is produced.

## Observed run

Run ID: `bots5-opv1-controlled-failure-20260830T001907Z-076fc9d3`.

The one-token request instead produced `ProviderResponseError: malformed_provider_response` before a
normal provider completion could be reconstructed. B.O.T.S. persisted the worker as terminal
`failed`, skipped synthesis with `dependency_failed`, recorded synthesis cost as harness-known zero,
preserved the worker cost as unknown, aggregated cost as `partial`, persisted the overall run as
terminal `failed` with `ended_at`, and kept the run inspectable through `status` and `inspect`.

This divergence was adjudicated rather than automatically rerun. The exact `dependency_incomplete`
semantics were already covered by the deterministic test suite and had also been exercised by prior
real truncation runs. The observed provider-response failure therefore supplied additional controlled
failure evidence without exposing a blocking harness or procedure defect.

## Procedure

Validate before the paid request:

```bash
bots5 validate examples/opv1-controlled-failure/job.json
```

Run exactly once:

```bash
bots5 run examples/opv1-controlled-failure/job.json
```

Do not automatically rerun when observed behavior differs from the intended path. Inspect and
adjudicate the first run before changing the campaign.
