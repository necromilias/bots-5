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
