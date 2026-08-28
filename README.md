# B.O.T.S. 5

B.O.T.S. 5 V0 is a small local Python CLI that executes a fixed, reviewable multi-model job:
strict JSON manifest -> explicit text inputs -> bounded parallel workers -> optional synthesis ->
durable run artifacts.

It is deliberately not an autonomous agent platform. Models cannot spawn workers, invoke a shell,
discover repository context, mutate Git, perform RAG, or invent execution topology. The harness owns
validation, scheduling, persistence, limits, and state.

## Install

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

For real OpenRouter execution:

```bash
export OPENROUTER_API_KEY='...'
```

The key is read only at runtime and is not written to job or run artifacts.

## Validate first

```bash
bots5 validate examples/example-job.json
```

Validation performs no API calls and creates no run directory.

## Run intentionally

```bash
bots5 run examples/example-job.json
```

The example writes beneath `examples/.bots5/runs/` because its `runs_dir` is job-relative.

## Inspect

With the default run location (`./.bots5/runs`):

```bash
bots5 status RUN_ID
bots5 inspect RUN_ID STAGE_ID
```

For a custom manifest run directory:

```bash
bots5 status RUN_ID --runs-dir PATH
bots5 inspect RUN_ID STAGE_ID --runs-dir PATH
```

Both commands are disk-only and make no API calls.

## V0 constraints

- strict JSON, closed objects, no coercion;
- IDs must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`;
- temperature range is 0 through 2 inclusive;
- OpenRouter is the only provider;
- non-streaming chat completions;
- no retries;
- exact provider-reported cost only; unknown stays unknown;
- the cost threshold is only a pre-synthesis gate, not a hard whole-run budget;
- local operator trust model, not a hostile sandbox;
- no database, daemon, web UI, container requirement, RAG, OMC, model tools, or repo mutation.

See `docs/` for the exact contract.

## Build provenance

The first candidate was bootstrapped through a temporary Langflow/OpenRouter specialist campaign,
then integrated and verified locally. See:

- `docs/BUILD_CAMPAIGN.md` for the method retrospective and failure-shape findings;
- `docs/V0_BUILD_REPORT.md` for the first candidate's concrete validation record.
