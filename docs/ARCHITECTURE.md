# Architecture

## Responsibilities

The human/planner decides the desired campaign and writes or reviews the job manifest. B.O.T.S. 5
is the deterministic harness: it validates the manifest, reads only declared text, schedules a
bounded worker phase, persists each stage, applies synthesis gates, and records observable state.
Models are bounded workers. They return text; they do not own topology, permissions, budgets,
persistence, or consequential actions.

## V0/V0.2 topology

```text
validated job
  -> explicit UTF-8 inputs
  -> independent workers (asyncio, bounded by max_parallelism)
  -> complete worker phase
  -> optional synthesis over declared succeeded-and-complete dependencies
  -> persisted result/state
```

There is no autonomous delegation loop and no worker-to-worker dependency in V0.

## Linux v0.1 Phase 3 generation

The desktop core starts with the fake streaming backend. An explicit `bots5-desktop` selection wires
the B.O.T.S.-owned `openai_compatible_http` streaming backend to either the local OpenAI-compatible
provider identity or the existing OpenRouter provider seam. The request snapshot records the selected
provider, model, normalized non-secret base URL, authentication environment-variable name when used,
and the exact current user message. It never records the resolved credential.

The backend emits a dispatch marker before opening the HTTP stream, normalizes SSE deltas and optional
usage/cost telemetry, and never adds a telemetry-specific alternate request shape. Each delta is
persisted before its `message_delta` event. Terminal state is persisted before its terminal event. A
local cancellation cancels the owned task and HTTP stream, preserves committed partial text, records
`ABORTED`; `remote_outcome_unknown` is true only when dispatch may have occurred, and does not retry.

## Trust boundaries

The operator and manifest are trusted. Model output is untrusted text. OpenRouter and the configured
local OpenAI-compatible endpoint are external service boundaries. The local filesystem rules reduce
accidental writes but are not a hostile sandbox.

Instruction provenance is structural: the harness-owned execution boundary outranks the validated
worker contract, which outranks all INPUT or WORKER OUTPUT data. Data blocks remain in user messages;
they never become part of the system prompt. See `WORKER_CONTRACTS.md`.

## Determinism

The job schema is strict and closed. Input ordering, prompt loading, dependency ordering, message
rendering, state vocabulary, and artifact locations are explicit. Model generation remains
nondeterministic; the shell around it is intended to be predictable.

Contract parsing, boundary compilation, and system/user message separation are deterministic. Model
obedience to those instructions remains probabilistic.

## Provider boundary

`Provider.complete(CompletionRequest) -> CompletionResult` is the only model-service seam. V0.2 ships
`OpenRouterProvider` and one built-in non-streaming `OpenAICompatibleProvider`. The runner receives a
mapping keyed by provider ID and selects the mapping entry named by each declared stage. The provider,
request, and result contracts do not change.

Schema-v2 provider configuration is validated into the `Job` before execution. The local configuration
contains only its HTTP/HTTPS base URL and optional credential-environment-variable name; resolved
credential values are never carried by the job or persisted.

## Concurrency

Workers are scheduled together and use one `asyncio.Semaphore(max_parallelism)`. Each worker persists
its own terminal state immediately. Synthesis is evaluated only after the full worker phase joins.

## Explicit non-goals

No rich DAG, autonomous planning, agent framework, shell/tools, repository mutation, RAG, OMC,
database, daemon, web server, GUI, retries, or distributed execution.

## Accepted Linux v0.1 target architecture

The preceding sections describe currently implemented V0/V0.2 behaviour. They remain authoritative for
what exists until separately approved Linux v0.1 changes land.

The accepted Linux v0.1 target architecture is defined in `LINUX_V0_1_DESIGN.md`. In summary:

- one native Qt/PySide6 desktop process hosts one authoritative, separable/headless-testable B.O.T.S.
  core for v0.1;
- clients use commands, queries, and events rather than mutating persistence directly;
- mutable application state is transactional SQLite behind a core-owned store; attachment payloads and
  campaign evidence remain filesystem-backed;
- chats use immutable message lineage; edits/regeneration create new branches/siblings rather than
  rewriting history;
- generation attempts and immutable request snapshots are first-class provenance;
- one execution manager owns concurrent chat, summary, and campaign work;
- a B.O.T.S.-owned generation-capability contract normalizes provider/local-engine protocols without
  making OpenAI's schema the internal domain or deleting backend-specific capabilities;
- deterministic context construction, derived/rebuildable search, structured failure semantics,
  explicit recovery, and single-authority ownership are core responsibilities;
- multiple windows share one authority; future Android/remote clients may later talk to that authority,
  but no daemon/network service is required solely for Linux v0.1.

The architectural invariant is: intelligence is not capability, capability is not authority, and
authority is not execution location. Model output remains data/proposal until B.O.T.S. authority grants
a separately defined capability and scope.

The accepted target introduces database, GUI, streaming, wider backend, and recovery concepts that are
explicitly absent from the current V0/V0.2 implementation. Do not read this target section as evidence
that those features have already landed.
