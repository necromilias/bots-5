# B.O.T.S. 5

**V0 CLOSED.** Final zero-spend closure validation passed against baseline
`13e3ac463c44d66e57d4443027f0cc9dfe9b93a5`: 65 tests passed, compileall passed, all three
checked-in example manifests validated, and `git diff --check` passed with `OPENROUTER_API_KEY`
unset and no provider/API calls. See `docs/V0_CLOSURE_REPORT.md`.

B.O.T.S. 5 V0 is a small local Python CLI that executes a fixed, reviewable multi-model job:
strict JSON manifest -> explicit text inputs -> bounded parallel workers -> optional synthesis ->
durable run artifacts.

It is deliberately not an autonomous agent platform. Models cannot spawn workers, invoke a shell,
discover repository context, mutate Git, perform RAG, or invent execution topology. The harness owns
validation, scheduling, persistence, limits, and state.

V0.1 compiles every model system message from a fixed harness-owned execution boundary plus a
validated six-section worker contract. Declared source and worker outputs remain untrusted data in
user-message blocks. See `docs/WORKER_CONTRACTS.md` for the exact authority and contract rules.

V0.2 adds a schema-v2, built-in non-streaming `local_openai` provider. Schema v1 remains unchanged
and OpenRouter-only; schema v2 can route each worker and synthesis stage to OpenRouter or an
operator-supplied local OpenAI-compatible HTTP/HTTPS endpoint.

## Linux v0.1 desktop direction

The Linux v0.1 native desktop product, architecture, implementation-technology baseline, and phased
construction sequence have been accepted. See `docs/LINUX_V0_1_DESIGN.md` for the build-facing target.

That document is target design, not an implementation-status claim. The current CLI/runtime behaviour
and dependencies described below remain the implemented state until separately approved desktop changes
land.

Linux v0.1 implementation remains separately gated. Future Android, Code/Git, daemon/remote-client,
MCP/tool frameworks, scheduling, RAG, and operating-mode capability systems remain deferred unless a
concrete v0.1 blocker separately promotes them.

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

For a local-only schema-v2 job, set its `providers.local_openai.base_url`. Authentication is optional;
when `api_key_env` is present, only that named environment variable is read. A local-only job does
not require `OPENROUTER_API_KEY`.

## Validate first

```bash
bots5 validate examples/example-job.json
bots5 validate examples/example-job-v2-local-openai.json
```

Validation performs no API calls and creates no run directory.

## Run intentionally

```bash
bots5 run examples/example-job.json
```

The example writes beneath `examples/.bots5/runs/` because its `runs_dir` is job-relative.

For normal paid-operation preflight, worker selection, completion review, evidence retention, and
human acceptance, see `docs/OPERATING_PROCEDURE_V1.md`. For local-provider operation, see
`docs/OPERATING_PROCEDURE_V2.md`.

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

A relative CLI `--runs-dir` is interpreted from the current working directory, unlike manifest
`output.runs_dir`, which is resolved relative to the job file. Both inspection commands are disk-only
and make no API calls.

## V0 constraints

- strict JSON, closed objects, no coercion;
- mandatory ordered `TASK`, `ALLOWED`, `FORBIDDEN`, `EVIDENCE`, `OUTPUT`, and `STOP CONDITION`
  sections in every worker and synthesis contract;
- IDs must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`;
- temperature range is 0 through 2 inclusive;
- schema v1 accepts only OpenRouter; schema v2 accepts OpenRouter and `local_openai`;
- local provider endpoints are explicit HTTP/HTTPS API bases; B.O.T.S. appends
  `/chat/completions` and never discovers endpoints or models;
- local authentication is optional and environment-indirected through `api_key_env`; resolved
  secret values are never persisted;
- non-streaming chat completions;
- no retries;
- exact provider-reported cost when supplied; a stage skipped before any request may carry a
  harness-known zero because no provider cost was incurred;
- the cost threshold is only a pre-synthesis gate over the known worker subtotal, not a hard
  whole-run budget or fail-closed unknown-cost control;
- local operator trust model, not a hostile sandbox;
- no database, daemon, web UI, container requirement, RAG, OMC, model tools, or repo mutation.

See `docs/` for the exact contract.

## Build provenance

The first candidate was bootstrapped through a temporary Langflow/OpenRouter specialist campaign,
then integrated and verified locally. See:

- `docs/BUILD_CAMPAIGN.md` for the method retrospective and failure-shape findings;
- `docs/V0_BUILD_REPORT.md` for the first candidate's concrete validation record;
- `docs/V0_1_HARDENING_REPORT.md` for deterministic V0.1 worker-boundary hardening;
- `docs/V0_1_LIVE_CONFORMANCE_REPORT.md` for the first live conformance canary and truncation
  finding;
- `docs/V0_1_FINAL_CONFORMANCE_REPORT.md` for final completion-aware live conformance closure;
- `docs/FIRST_USEFUL_CAMPAIGN_REPORT.md` for the first ordinary useful-work campaign and the human
  adjudication of its findings;
- `examples/opv1-controlled-failure/` for the controlled failure-path campaign assets and observed
  outcome;
- `docs/OPV1_FREEZE_REPORT.md` for the freeze decision and supporting evidence;
- `docs/OPERATING_PROCEDURE_V1.md` for the frozen normal operating procedure;
- `docs/V0_CLOSURE_REPORT.md` for the final V0 closure validation and decision;
- `docs/V0_2_DESIGN_CAMPAIGN_REPORT.md` for the V0.2 provider-design swarm, synthesis recovery,
  reasoning-response normalization defect, live repair proof, and implementation closure;
- `docs/OPERATING_PROCEDURE_V2.md` for schema-v2 local-provider preflight and review.
