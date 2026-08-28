# Observability

Each run is self-contained:

```text
<RUNS_DIR>/<run-id>/
  run.json
  job.resolved.json
  events.jsonl
  usage.json
  stages/
    <stage-id>.json
    <stage-id>.md
  result.md
```

`result.md` is present only when synthesis succeeds.

## Stage JSON

Contains stage/provider/model identity, state, timestamps, duration, token usage when known, exact
provider-reported cost when known, request ID, relative output path, and sanitized failure metadata.
`cost_usd: null` plus `cost_known: false` means unknown. Known costs are stored as decimal strings to
avoid binary floating-point money arithmetic.

## Events

`events.jsonl` is append-only, one compact JSON object per line, fsynced per append. Event writes are
serialized with a process-local lock. Events contain small metadata only, never complete prompts,
outputs, request headers, or API keys.

Vocabulary:

`run_started`, `stage_queued`, `stage_started`, `request_sent`, `stage_succeeded`, `stage_failed`,
`stage_skipped`, `synthesis_blocked`, `run_timed_out`, `run_succeeded`, `run_failed`.

## Usage and cost

`usage.json` contains per-stage usage and aggregate known token sums. Cost state is:

- `zero`: every stage reported exact zero.
- `known`: every stage cost is known and the sum is nonzero (or no stages exist).
- `partial`: at least one known and at least one unknown cost.
- `unknown`: all relevant stage costs are unknown.

Unknown cost is never silently converted to zero.

## Status and inspect

`bots5 status RUN_ID` and `bots5 inspect RUN_ID STAGE_ID` use
`./.bots5/runs` relative to the current working directory by default. For manifests configured
elsewhere, pass `--runs-dir PATH`. These commands read disk only and make no provider calls.
