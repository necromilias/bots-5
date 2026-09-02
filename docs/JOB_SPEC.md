# Job Specification

V0 uses UTF-8 strict JSON. Every object is closed: unknown fields are rejected. All listed fields are
required. To omit synthesis, set `"synthesis": null`. Schema v1 is frozen and remains unchanged.
Schema v2 adds the required top-level `providers` object described below.

## Top level

- `schema_version`: integer, exactly `1` or `2`.
- `name`: non-empty string.
- `inputs`: list, may be empty.
- `execution`: object.
- `workers`: list, may be empty.
- `synthesis`: object or `null`.
- `output`: object.
- `providers`: required only for schema v2; a closed provider-configuration object.

No type coercion is performed. JSON `NaN`, `Infinity`, and `-Infinity` are rejected.

## IDs

Worker and synthesis IDs must match:

```text
[A-Za-z0-9][A-Za-z0-9._-]{0,63}
```

Worker IDs must be unique. A synthesis ID may not collide with a worker ID.

## Inputs

Each item has exactly:

- `label`: non-empty unique string.
- `path`: non-empty string.

Relative paths resolve against the job file's parent directory. The target must exist, be a readable
regular file, and decode as UTF-8.

## Execution

- `max_parallelism`: integer >= 1.
- `run_timeout_seconds`: finite number > 0.
- `stop_before_synthesis_if_known_cost_exceeds_usd`: finite non-negative number or `null`.

The cost field is a pre-synthesis gate only. It is not a hard run budget. The gate compares only the
known worker-cost subtotal. Unknown or partial worker cost does not itself block synthesis.

## Worker

Each worker has exactly:

- `id`: safe ID.
- `provider`: `"openrouter"` in schema v1; `"openrouter"` or `"local_openai"` in schema v2.
- `model`: non-empty string; never hard-coded in core code.
- `system_prompt_path`: non-empty path string, job-relative when relative.
- `temperature`: finite number from 0 through 2 inclusive.
- `max_output_tokens`: integer >= 1.
- `timeout_seconds`: finite number > 0.

Workers have no dependencies in V0.

The referenced prompt file must be a valid worker contract as specified in
`WORKER_CONTRACTS.md`.

## Schema-v2 providers

Schema v2 has a required top-level `providers` object. It may contain only the optional
`local_openai` object. OpenRouter has no manifest credentials or endpoint configuration; its
credential remains the fixed `OPENROUTER_API_KEY` environment variable.

`providers.local_openai` has exactly:

- `base_url`: required non-empty HTTP or HTTPS API-base URL, without userinfo, query, fragment, or
  surrounding whitespace. Trailing slashes are normalized away. B.O.T.S. appends
  `/chat/completions` to this base.
- `api_key_env`: optional non-empty environment-variable name, or `null`. When a name is present,
  its value is read only at runtime and used as a Bearer token. The value itself is never part of the
  validated job or any persisted artifact.

Any stage declaring `local_openai` requires `providers.local_openai`. A schema-v2 OpenRouter-only job
may use `"providers": {}`. An explicit `providers.local_openai: null` is invalid; use omission or
`api_key_env: null` to represent unauthenticated local access.

## Synthesis

The synthesis object has the same common fields as a worker plus:

- `depends_on`: non-empty list of unique worker IDs.

Every dependency must name an existing worker. Synthesis runs only after the entire worker phase and
only when every declared dependency succeeded and completed normally.

`depends_on` controls which worker outputs are supplied to synthesis and which workers gate synthesis.
It does not remove any other declared worker from the final whole-run success condition: every
declared worker must still succeed and complete normally for the overall run to succeed.

## Output

`runs_dir` is a non-empty string. Relative values resolve against the job file's parent. The resolved
path may not be filesystem root and, if it already exists, must be a directory. V0 may write only
inside this resolved runs directory. V0 does not claim a hostile sandbox.

This job-relative path rule differs from CLI `status --runs-dir` and `inspect --runs-dir`, where a
relative path is interpreted from the operator's current working directory.

## Rendering

Worker and synthesis system messages are compiled as:

```text
<fixed B.O.T.S. 5 execution boundary>

<exact UTF-8 validated contract file>
```

The boundary text is owned by B.O.T.S. source. The separator is exactly two newline characters.
The contract file content is otherwise preserved, including a final newline when present.

Worker user message, in manifest input order:

```text
=== INPUT: <label> ===
<exact text>
=== END INPUT: <label> ===
```

Blocks are joined by one newline. No trimming is performed.

Synthesis user message, in exact `depends_on` order:

```text
=== WORKER OUTPUT: <worker-id> ===
<exact output>
=== END WORKER OUTPUT: <worker-id> ===
```

Original source inputs are not automatically passed to synthesis.
INPUT and WORKER OUTPUT block contents are untrusted task data. They are never interpolated into the
system message.
