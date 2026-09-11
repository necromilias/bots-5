# B.O.T.S. 5

B.O.T.S. is a local-first AI harness and native Linux desktop project built around explicit authority,
inspectable state, durable evidence, and human-controlled consequential actions.

## Current status

The original campaign harness baseline is closed and preserved. V0.2 adds the built-in
`local_openai` provider while preserving the bounded manifest-driven execution model.

Linux v0.1 is now implemented and landed through **Phase 6**:

1. native walking skeleton;
2. conversation truth, immutable lineage/revisions, and deterministic fake generation;
3. real generation backend, streaming, cancellation, checkpointing, and local-model acceptance;
4. concurrency, multi-window workspace, shutdown, and crash reconciliation;
5. provider/model usability, secrets, catalogue/capability discovery, and settings;
6. deterministic context construction, content-addressed attachments, unified data-root authority,
   rooted SQLite/native durability handling, and fail-closed effect ownership.

The landed Phase 6 implementation is commit
`20847c7a49e26679d0d3dfe99798a2c211bec436`. Its final reserved acceptance gates were
**616 passed** for the complete Phase 6 + authority/grant suite and **956 passed, 1 expected skip** for
the complete repository suite, with no failures and no provider contacted. See
`docs/LINUX_V0_1_PHASE6_CLOSURE_REPORT.md` for the final closure and landing record.

**Phase 7, search and exact navigation, is next.**

The build-facing Linux v0.1 contract remains `docs/LINUX_V0_1_DESIGN.md`. The cumulative Phase 6
implementation history remains in `docs/LINUX_V0_1_PHASE6_IMPLEMENTATION_REPORT.md`; its earlier
candidate-status statements are historical evidence and are superseded by the Phase 6 closure report.

The provisional UI authority remains `docs/LINUX_V0_1_UI_UX_DRAFT_1.md`; it has not been promoted to
final design authority.

## Campaign harness

The original B.O.T.S. harness executes fixed, reviewable multi-model jobs:

`strict JSON manifest -> explicit text inputs -> bounded parallel workers -> optional synthesis -> durable run artifacts`

It deliberately does not give models autonomous authority. Models cannot spawn workers, invoke a shell,
discover repository context, mutate Git, perform RAG, or invent execution topology. The harness owns
validation, scheduling, persistence, limits, and state.

V0.1 compiles each model system message from a fixed harness-owned execution boundary plus a validated
six-section worker contract. Declared source and worker outputs remain untrusted data in user-message
blocks. See `docs/WORKER_CONTRACTS.md`.

V0.2 adds schema-v2 and the built-in non-streaming `local_openai` provider. Schema v1 remains
OpenRouter-only; schema v2 can route workers and synthesis to OpenRouter or an operator-supplied local
OpenAI-compatible HTTP/HTTPS endpoint.

## Linux desktop authority boundary

Linux v0.1 uses one authoritative native core shared by desktop windows. Persisted authoritative state
outranks in-memory presentation state. Runtime data-root effects are governed by one
`DataRootAuthority` and effect-grant protocol across public application commands, SQLite/store work,
EventBus delivery, attachments/GC, startup/migration/recovery, native VFS outcome handoff, and terminal
teardown.

Invalidation closes new admission immediately and revokes the discovering grant. Already-admitted
unrelated work may settle only within its existing ownership. Database resources, including rooted
child cursors, remain accounted for until consequential native state has settled or been classified.
Unknown or integrity-threatening outcomes fail closed rather than being rewritten as success.

See `docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md` for the finite participation inventory.

## Local Phase 3 compatibility route

For opt-in local Phase 3 desktop testing, supply the backend, endpoint, and model explicitly. This
`local_openai` route is an explicit legacy compatibility mode: it is Phase 6 disabled, makes no Phase 6
planning/accounting/provenance claim, and does not permit selecting attachments.

```bash
bots5-desktop --backend local_openai \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen3-8B \
  --api-key-env LOCAL_QWEN_API_KEY
```

The fake backend remains the default. The opt-in acceptance probe is `tests/test_phase3_local_qwen.py`;
it skips unless a local endpoint and model are explicitly provided through its documented environment
variables. It never selects OpenRouter.

## Install

Requires Python `>=3.12,<3.15`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

For real OpenRouter execution of the campaign harness:

```bash
export OPENROUTER_API_KEY='...'
```

The key is read only at runtime and is not written to job or run artifacts.

For a local-only schema-v2 campaign job, set `providers.local_openai.base_url`. Authentication is
optional; when `api_key_env` is present, only that named environment variable is read. A local-only job
does not require `OPENROUTER_API_KEY`.

## Validate and run campaign jobs

```bash
bots5 validate examples/example-job.json
bots5 validate examples/example-job-v2-local-openai.json
bots5 run examples/example-job.json
```

Validation performs no API calls and creates no run directory.

For normal paid-operation preflight, worker selection, completion review, evidence retention, and human
acceptance, see `docs/OPERATING_PROCEDURE_V1.md`. For local-provider operation, see
`docs/OPERATING_PROCEDURE_V2.md`.

With the default run location (`./.bots5/runs`):

```bash
bots5 status RUN_ID
bots5 inspect RUN_ID STAGE_ID
```

For custom run directories:

```bash
bots5 status RUN_ID --runs-dir PATH
bots5 inspect RUN_ID STAGE_ID --runs-dir PATH
```

These inspection commands are disk-only and make no API calls.

## Legacy campaign constraints

The following describe the campaign harness, not the native desktop product:

- strict JSON, closed objects, no coercion;
- mandatory ordered `TASK`, `ALLOWED`, `FORBIDDEN`, `EVIDENCE`, `OUTPUT`, and `STOP CONDITION` sections;
- bounded parallel worker execution;
- no autonomous delegation loop;
- no model shell/filesystem/Git/RAG/plugin authority;
- non-streaming V0.2 campaign-provider completions;
- no retries;
- exact-known/unknown provider cost semantics;
- local operator trust model, not a hostile sandbox.

The Linux desktop adds SQLite, native UI, streaming generation, attachments, wider lifecycle authority,
and recovery semantics under the accepted Linux design; do not infer desktop capability from the old V0
non-goal lists.

## Deferred beyond Linux v0.1

Unless a concrete blocker separately promotes bounded work, Android, Code/Git, persistent daemon/remote
clients, MCP/general tool frameworks, scheduling/automation, remote execution nodes, RAG/semantic search,
and operating-mode capability frameworks remain deferred.

## Build provenance

Key records include:

- `docs/BUILD_CAMPAIGN.md` — original build method retrospective;
- `docs/V0_CLOSURE_REPORT.md` — final V0 closure;
- `docs/V0_2_DESIGN_CAMPAIGN_REPORT.md` — V0.2 provider design and implementation closure;
- `docs/LINUX_V0_1_PHASE1_PHASE2_CLOSURE_REPORT.md` — early desktop closure;
- `docs/LINUX_V0_1_PHASE3_IMPLEMENTATION_REPORT.md` through
  `docs/LINUX_V0_1_PHASE6_IMPLEMENTATION_REPORT.md` — cumulative phase implementation evidence;
- `docs/LINUX_V0_1_PHASE6_CLOSURE_REPORT.md` — current Phase 6 closure and landing authority;
- `docs/LINUX_V0_1_DESIGN.md` — accepted Linux v0.1 build-facing contract.
