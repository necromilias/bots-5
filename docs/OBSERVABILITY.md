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

`result.md` is present when synthesis returns usable output and its stage is persisted as succeeded.
It may therefore exist for an incomplete synthesis whose overall run is failed. File presence alone
is not evidence of normal completion or human acceptance; check `run.json` and synthesis completion
metadata.

## Stage JSON

Contains stage/provider/model identity, state, timestamps, duration, token usage when known, exact
cost when known, request ID, relative output path, completion metadata, and sanitized failure
metadata. Completion preserves the provider finish reason; only exact `stop` is complete, while
missing, malformed, and all other reasons are conservatively incomplete.
`cost_usd: null` plus `cost_known: false` means unknown. Known costs are stored as decimal strings
to avoid binary floating-point money arithmetic.

A known cost may come from exact provider-reported usage or from a harness-known zero when a stage is
skipped before any provider request is sent. A skipped stage recorded as zero is therefore not a
claim that the provider reported a zero-dollar request; it records that no request for that stage was
made and no provider cost was incurred by it.

## Events

`events.jsonl` is append-only, one compact JSON object per line, fsynced per append. Event writes are
serialized with a process-local lock. Events contain small metadata only, never complete prompts,
outputs, request headers, or API keys.

Vocabulary:

`run_started`, `stage_queued`, `stage_started`, `request_sent`, `stage_succeeded`, `stage_failed`,
`stage_skipped`, `synthesis_blocked`, `run_timed_out`, `run_succeeded`, `run_failed`.

## Usage and cost

`usage.json` contains per-stage usage and aggregate known token sums. Cost state is:

- `zero`: every stage cost is known and zero.
- `known`: every stage cost is known and the sum is nonzero (or no stages exist).
- `partial`: at least one known and at least one unknown cost.
- `unknown`: all relevant stage costs are unknown.

Unknown provider cost is never silently converted to zero. Harness-known skipped-stage zero is a
separate case: no provider request was sent for that stage.

## Status and inspect

`bots5 status RUN_ID` and `bots5 inspect RUN_ID STAGE_ID` use
`./.bots5/runs` relative to the current working directory by default. For manifests configured
elsewhere, pass `--runs-dir PATH`. A relative `--runs-dir` value is interpreted from the current
working directory, unlike manifest `output.runs_dir`, which is resolved relative to the job file.
Use an absolute `--runs-dir` when changing directories between execution and inspection.

These commands read disk only and make no provider calls. Both views display the persisted
completion state and finish reason.
