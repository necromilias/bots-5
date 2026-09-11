# Architecture

## Repository architecture

B.O.T.S. now contains two related execution surfaces:

1. the closed V0/V0.2 manifest-driven campaign harness; and
2. the native Linux v0.1 desktop product, landed through Phase 6.

The campaign harness remains a bounded deterministic worker orchestrator. The desktop adds durable
conversation state, native UI, streaming generation, SQLite-backed application persistence,
content-addressed attachments, and a stronger authority/durability boundary.

## Campaign harness responsibilities

The human/planner decides the desired campaign and writes or reviews the job manifest. B.O.T.S. validates
the manifest, reads only declared text, schedules a bounded worker phase, persists each stage, applies
synthesis gates, and records observable state.

Models are bounded workers. They return text; they do not own topology, permissions, budgets,
persistence, or consequential actions.

### V0/V0.2 topology

```text
validated job
  -> explicit UTF-8 inputs
  -> independent workers (asyncio, bounded by max_parallelism)
  -> complete worker phase
  -> optional synthesis over declared succeeded-and-complete dependencies
  -> persisted result/state
```

There is no autonomous delegation loop and no worker-to-worker dependency in V0.

V0.2 ships `OpenRouterProvider` and one built-in non-streaming `OpenAICompatibleProvider`. The runner
selects the provider mapping declared by each stage. Schema-v2 provider configuration contains only
non-secret endpoint/configuration material; resolved credentials are not persisted.

## Linux v0.1 landed architecture

The accepted desktop contract is `LINUX_V0_1_DESIGN.md`. Phases 1 through 6 are now landed; Phase 7
(search and exact navigation) is next.

Linux v0.1 runs as one native Qt/PySide6 desktop process containing one authoritative, separable,
headless-testable B.O.T.S. core. Multiple windows are clients/views over the same authority.

Clients mutate state through core commands, obtain authoritative state through queries, and receive
changes through events. UI code does not directly mutate persistence.

### Conversation and generation model

Chats are durable containers over immutable historical message lineage. Edits/regeneration create new
nodes or sibling attempts rather than rewriting history. Generation attempts and immutable request
snapshots are first-class provenance.

The desktop generation backend contract is B.O.T.S.-owned rather than OpenAI-defined. Adapters normalize
provider/local-engine protocols into typed B.O.T.S. output/lifecycle semantics while preserving
backend-specific capability data where relevant.

A stream ending does not automatically mean successful completion. Cancellation, usage/cost, remote
outcome, and partial persistence are recorded with their actual certainty. Requests are not invisibly
retried once external acceptance or spend is uncertain.

### Persistence and data-root authority

SQLite is authoritative mutable application state behind the core-owned store. Attachment payloads are
filesystem-backed and content-addressed. Persisted authoritative state outranks in-memory/rendered state.

Phase 6 landed one unified data-root authority/effect-grant protocol. `DataRootAuthority` is the root
admission and invalidation coordinator for public application commands, EventBus delivery, SQLite/store
work, attachments/GC, startup/migration/recovery, durability fences, native VFS outcome handoff, and
terminal teardown.

Forward work requires the exact live authority/grant/resource ownership. Invalidation immediately closes
new admission and revokes the discovering grant. Unrelated already-admitted work may settle only within
its existing ownership. Cleanup after revocation may release known resources but cannot create fresh
forward work.

Rooted database resources retain consequential child cursor/statement state until native settlement or
classification; the parent resource may not be released first. Native/SQLite/filesystem uncertainty is
handed into the common coordinator rather than silently rewritten as success.

The finite participation inventory is `UNIFIED_AUTHORITY_EFFECT_INVENTORY.md`. Phase 6 closure is recorded
in `LINUX_V0_1_PHASE6_CLOSURE_REPORT.md`.

### Context and attachments

Phase 6 owns deterministic, inspectable context construction and persistent reusable attachments.
Attachment originals are content-addressed with SHA-256; metadata/provenance remain separate durable
records. Canonical payload validation and persisted representation semantics are authority-owned facts.
Detected authoritative corruption invalidates through the common coordinator before forward use.

The legacy Phase 3 `local_openai` desktop compatibility route remains explicitly Phase 6 disabled and
cannot be treated as Phase 6 planning/accounting/provenance execution.

### Concurrency and events

The core owns asynchronous work. Event publication/delivery is an authority-participating effect rather
than a detached notification side channel. Multiple windows share the same state/authority; closing one
window does not imply core shutdown while other clients remain.

### Secrets

The desktop uses a core-owned SecretStore abstraction. Linux Secret Service via keyring/secretstorage is
the interactive credential store; environment variables remain an explicit supported source for
headless/deployment use. Plaintext fallback is not implicit.

## Trust boundaries

The operator and explicit configuration are trusted. Model output is untrusted data/proposal. External
providers/local endpoints are service boundaries. B.O.T.S. does not claim a hostile same-UID local
sandbox.

The architectural invariant is:

**Intelligence is not capability. Capability is not authority. Authority is not execution location.**

Generated requests do not acquire consequential capability merely because a model emitted them.

## Determinism

The campaign schema, prompt loading, dependency ordering, rendering, state vocabulary, and artifact
locations are explicit. Linux desktop context construction, request snapshots, lineage, authority state,
and persistence boundaries are likewise explicit and inspectable. Model generation remains
nondeterministic.

## Package boundaries

Use a technology-agnostic domain layer; application/core layer for commands, queries, workflows, policy,
context, configuration/capability resolution, authority and orchestration; infrastructure adapters for
persistence/backends/search/secrets/files/diagnostics; thin native clients; and explicit bootstrap/
composition wiring.

Dependencies point inward toward B.O.T.S. semantics.

## Legacy campaign non-goals

The following remain non-goals of the **campaign harness itself**: autonomous planning/delegation, model
shell/filesystem/Git authority, RAG, plugin discovery, retries, or distributed execution.

Do not read that list as a repo-wide statement that the Linux desktop lacks a database or GUI; those are
landed desktop components.

## Deferred beyond Linux v0.1

Unless separately promoted by a concrete blocker, Android, Code/Git, persistent daemon/remote clients,
MCP/general tool frameworks, scheduling/automation, remote execution nodes, RAG/semantic search, and
mode-governed capability frameworks remain future work.
