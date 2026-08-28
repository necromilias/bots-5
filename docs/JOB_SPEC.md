# V0 Job Specification

V0 uses UTF-8 strict JSON. Every object is closed: unknown fields are rejected. All listed fields are
required. To omit synthesis, set `"synthesis": null`.

## Top level

- `schema_version`: integer, exactly `1`.
- `name`: non-empty string.
- `inputs`: list, may be empty.
- `execution`: object.
- `workers`: list, may be empty.
- `synthesis`: object or `null`.
- `output`: object.

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

The cost field is a pre-synthesis gate only. It is not a hard run budget.

## Worker

Each worker has exactly:

- `id`: safe ID.
- `provider`: `"openrouter"`.
- `model`: non-empty string; never hard-coded in core code.
- `system_prompt_path`: non-empty path string, job-relative when relative.
- `temperature`: finite number from 0 through 2 inclusive.
- `max_output_tokens`: integer >= 1.
- `timeout_seconds`: finite number > 0.

Workers have no dependencies in V0.

## Synthesis

The synthesis object has the same common fields as a worker plus:

- `depends_on`: non-empty list of unique worker IDs.

Every dependency must name an existing worker. Synthesis runs only after the entire worker phase and
only when every declared dependency succeeded.

## Output

`runs_dir` is a non-empty string. Relative values resolve against the job file's parent. The resolved
path may not be filesystem root and, if it already exists, must be a directory. V0 may write only
inside this resolved runs directory. V0 does not claim a hostile sandbox.

## Rendering

Worker system message: exact UTF-8 prompt file.

Worker user message, in manifest input order:

```text
=== INPUT: <label> ===
<exact text>
=== END INPUT: <label> ===
```

Blocks are joined by one newline. No trimming is performed.

Synthesis system message: exact synthesis prompt file.

Synthesis user message, in exact `depends_on` order:

```text
=== WORKER OUTPUT: <worker-id> ===
<exact output>
=== END WORKER OUTPUT: <worker-id> ===
```

Original source inputs are not automatically passed to synthesis.
