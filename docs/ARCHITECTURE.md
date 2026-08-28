# Architecture

## Responsibilities

The human/planner decides the desired campaign and writes or reviews the job manifest. B.O.T.S. 5
is the deterministic harness: it validates the manifest, reads only declared text, schedules a
bounded worker phase, persists each stage, applies synthesis gates, and records observable state.
Models are bounded workers. They return text; they do not own topology, permissions, budgets,
persistence, or consequential actions.

## V0 topology

```text
validated job
  -> explicit UTF-8 inputs
  -> independent workers (asyncio, bounded by max_parallelism)
  -> complete worker phase
  -> optional synthesis over declared successful dependencies
  -> persisted result/state
```

There is no autonomous delegation loop and no worker-to-worker dependency in V0.

## Trust boundaries

The operator and manifest are trusted. Model output is untrusted text. The OpenRouter provider is an
external service boundary. The local filesystem rules reduce accidental writes but are not a hostile
sandbox.

## Determinism

The job schema is strict and closed. Input ordering, prompt loading, dependency ordering, message
rendering, state vocabulary, and artifact locations are explicit. Model generation remains
nondeterministic; the shell around it is intended to be predictable.

## Provider boundary

`Provider.complete(CompletionRequest) -> CompletionResult` is the only model-service seam. V0 ships
only `OpenRouterProvider`.

## Concurrency

Workers are scheduled together and use one `asyncio.Semaphore(max_parallelism)`. Each worker persists
its own terminal state immediately. Synthesis is evaluated only after the full worker phase joins.

## Local inference migration

A later version can add a local OpenAI-compatible provider behind the same normalized provider
boundary without changing worker/synthesis job semantics.

## Explicit non-goals

No rich DAG, autonomous planning, agent framework, shell/tools, repository mutation, RAG, OMC,
database, daemon, web server, GUI, retries, or distributed execution.
