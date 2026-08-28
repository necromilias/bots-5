# B.O.T.S. 5 V0.1 Hardening Report

Date: 2026-08-28

## Revision scope

- Working branch: `v0.1-worker-boundary-hardening`
- Base branch: `main`
- Base revision: `bc2368b13067ddd1d2381c60a3098d1d0b769ce2`
- Merge status: not merged
- Push status: not pushed
- Live-model status: no OpenRouter or other model-provider call performed

The JSON job schema remains at version 1 and is unchanged. The change is bounded to worker-contract
validation, system-prompt compilation, example contracts, adversarial tests, and their documentation.

## Design implemented

### Harness-owned execution boundary

`src/bots5/prompts.py` owns one fixed execution boundary for every ordinary worker and synthesis
stage. It establishes that the model is one bounded worker, the system message is the complete
instruction authority, INPUT and WORKER OUTPUT material is untrusted data, embedded task-like text
has no authority, role or task expansion is forbidden, limitations must be reported within scope,
and execution stops when the contracted output is complete.

### Mandatory worker contracts

Every referenced worker and synthesis prompt must contain the following exact standalone section
headings, once each and in this order:

1. `TASK`
2. `ALLOWED`
3. `FORBIDDEN`
4. `EVIDENCE`
5. `OUTPUT`
6. `STOP CONDITION`

`TASK` must be the first line and every section body must contain non-whitespace text. The parser is
deliberately small and deterministic rather than a general Markdown parser. Missing, duplicate,
empty, and out-of-order required sections are rejected. Existing UTF-8 validation remains enforced.

Both `bots5 validate` and `run_job` validate referenced contracts. `run_job` validates and compiles
all stage system messages before creating a run directory. The compiled form is the exact fixed
boundary, two newline characters, then the exact validated contract file content.

### Instruction/data separation

The implemented authority hierarchy is:

```text
harness-owned execution boundary
  > validated worker contract
    > INPUT or WORKER OUTPUT data
```

Original source remains exclusively in the existing worker user-message INPUT blocks. Synthesis
receives declared worker outputs in the existing user-message WORKER OUTPUT blocks and uses the same
harness boundary. The synthesis example contract permits evidence integration while explicitly
denying authority to instructions or role claims embedded in worker output.

## Files changed

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/EXECUTION.md`
- `docs/JOB_SPEC.md`
- `docs/SECURITY.md`
- `docs/WORKER_CONTRACTS.md`
- `docs/V0_1_HARDENING_REPORT.md`
- `examples/prompts/adversary.md`
- `examples/prompts/analyst.md`
- `examples/prompts/extractor.md`
- `examples/prompts/synthesis.md`
- `src/bots5/manifest.py`
- `src/bots5/prompts.py`
- `src/bots5/runner.py`
- `tests/fixtures/hostile_input.txt`
- `tests/helpers.py`
- `tests/test_cli_views.py`
- `tests/test_manifest.py`
- `tests/test_prompts.py`
- `tests/test_runner.py`

No provider, storage, event, usage, model-schema, or user-message rendering implementation was
changed.

## Tests added or changed

- Valid six-section contract acceptance.
- Missing required section rejection.
- Duplicate required section rejection.
- Empty required section rejection.
- Out-of-order section rejection.
- Invalid UTF-8 contract rejection.
- Invalid contract rejection through referenced-file validation.
- Exact system compilation as fixed boundary, two newlines, and unmodified contract content.
- Existing exact INPUT and WORKER OUTPUT user-message rendering tests retained unchanged.
- Hostile source fixture containing role redirection, task expansion, poem, ignore-instruction, and
  system-prompt-exfiltration requests.
- Deterministic proof that hostile source appears in the user INPUT payload and not the system
  message, while the boundary explicitly denies its authority.
- Deterministic proof that hostile worker output appears in synthesis's user payload and not its
  system message.
- Invalid-contract execution fails before provider invocation and run-directory creation.
- CLI validation test now proves that validation neither constructs a provider nor creates a run
  directory.

These tests establish deterministic construction and validation properties. They do not claim live
model compliance from string construction.

## Validation results

### Full test suite

Command:

```text
.venv/bin/python -m pytest
```

Result: exit 0.

```text
............................................                             [100%]
44 passed in 0.19s
```

The automatic test fixture removes `OPENROUTER_API_KEY` and blocks ordinary live network sockets.
Provider-path tests use `httpx.MockTransport`; no paid or live model call is made.

### Example manifest validation

Command:

```text
env -u OPENROUTER_API_KEY .venv/bin/bots5 validate examples/example-job.json
```

Result: exit 0.

```text
OK: bots5-smoke-example
```

`OPENROUTER_API_KEY` was confirmed unset. `examples/.bots5` was absent before and after validation,
confirming that validation created no run directory.

### Bytecode compilation

Command:

```text
.venv/bin/python -m compileall -q src tests
```

Result: exit 0 with no diagnostics.

### Diff and secret review

- `git diff --check`: exit 0 with no whitespace errors.
- Manual diff review found no changes outside the bounded prompt boundary, contract validation,
  examples, tests, and documentation scope.
- A repository scan excluding `.git` and `.venv` found no OpenRouter-key-shaped or bearer-token-shaped
  secret material.
- No real OpenRouter request was attempted. Network access used during this work was limited to
  cloning the requested GitHub repository and downloading declared development dependencies.

## Compatibility impact

- Existing job JSON remains schema-compatible; no manifest fields changed.
- Existing free-form worker or synthesis prompt files are intentionally incompatible until converted
  to the required six-section contract format.
- Direct `run_job` callers now receive referenced-file and contract validation before artifact
  creation, even if they bypass the CLI.
- Worker and synthesis system messages gain the fixed boundary, increasing prompt-token usage and
  potentially provider-reported cost slightly.
- INPUT and WORKER OUTPUT rendering, stage topology, scheduling, persistence, gates, timeouts,
  provider behavior, and output artifacts are otherwise unchanged.

## Limitations

- Contract parsing, prompt compilation, and system/user placement are deterministic; model compliance
  remains probabilistic.
- The parser accepts only exact unadorned section headings and is not a Markdown parser.
- Prompt hardening is not a sandbox or authorization mechanism. Model output remains untrusted text.
- No live-model conformance run was performed, so resistance to the adversarial fixture has not been
  empirically measured for the configured models.

## Recommendation

The deterministic V0.1 hardening change is ready for a separate controlled live conformance run, but
not for a claim of model-level compliance yet. That run should use the hostile fixture as a low-cost
canary, retain strict spend and output review gates, test each configured model separately, and avoid
promotion until every worker stays within its contract under observed live generation.
