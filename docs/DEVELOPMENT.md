# Development

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The test suite removes `OPENROUTER_API_KEY` and blocks live socket connections. Ordinary `pytest`
must never spend money.

## Layout

Pure validation/rendering/usage logic is separated from provider, persistence, and runner I/O.
`runner.py` is the only orchestration module. `providers/` is the only model-service seam.

## Adding a provider

Not a V0 feature. If added later, implement the normalized `Provider` protocol and update the
manifest provider allow-list, tests, docs, and security model together. Do not add plugin discovery
just to add a second provider.

## Changing the schema

Externally visible schema changes require matching validation tests and `JOB_SPEC.md` updates.
Preserve closed-object behavior and deterministic rendering unless a deliberate versioned contract
change says otherwise.

## Manual smoke

After unit tests and validation:

```bash
export OPENROUTER_API_KEY='...'
bots5 run examples/example-job.json
```

This is intentionally manual and may incur OpenRouter cost.
