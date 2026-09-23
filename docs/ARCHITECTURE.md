# Architecture

## Repository architecture

B.O.T.S. now contains two related execution surfaces:

1. the closed V0/V0.2 manifest-driven campaign harness; and
2. the native Linux v0.1 desktop product, landed through Phase 9 Slice B; Phase 9 Slice C
   Backup v1 and independent verification is the next bounded sequence boundary.

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

The accepted desktop contract is `LINUX_V0_1_DESIGN.md`. Phases 1 through 8 and Phase 9 Slices A and B
are landed. Phase 9 Slice A — Transcript v0.1 and strict Archive v1 export — landed at
`35b206a404d4cd3e2dd05a5c07ffdc6dd0e1ba40`. Phase 9 Slice B — validated Archive import and durable
import provenance — landed at `9a84d38b6ad2d3968db58f471d53bf85820656b1`. Slice C backup/verification
is next in the accepted sequence.

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

### Search, archive, and exact navigation

Phase 7 keeps SQLite business rows authoritative and treats FTS rows, document-key
mappings, snippets, rank, checkpoint, generation, active-branch annotations, and
resolved locations as derived state. A singleton authoritative source revision is
incremented atomically by every search-visible business transaction. Optional
in-memory receipts may advance only contiguous derived revisions; a lost receipt or
restart leaves a mechanically visible source/checkpoint mismatch and search refuses
service until an explicit deterministic rebuild.

The accepted index is one `unicode61` FTS5 document per authoritative chat, eligible
message, or attachment identity. Query input is compiled as literal terms. Results
are filtered and navigated through bounded authoritative joins: inactive messages
open as a temporary historical leaf without changing the chat head, and disappearing
identities return typed GONE behaviour. Archived chats are excluded by default;
inclusion is explicit. Unreferenced attachments remain absent from user-visible
results. Search has no provider, network, semantic, OCR, or detached-worker path.

### Inspection and provenance

Phase 8 adds a core-owned typed inspection projection over durable request-time facts. The core
interprets legacy, supported, future, and corrupt request snapshots and supplies safe status/field
values; Qt presents that projection without interpreting versioned persistence. Per-message and
chat-level views preserve historical provider/model/settings and Phase 6 context/attachment
provenance, while attachment inspection reads metadata only and never payload bytes. Selected-message
attempts are bound to the resolved active or historical branch, including regenerated shared ancestors;
chat-level history remains unfiltered when no message is selected. Inspector visibility, selected
message, and historical leaf are restored through additive migration `0011_phase8_inspector_state`,
with stale identities falling back safely. Phase 9 Slice B now supplies durable import provenance and
branch-aware continuation evidence while native, never-imported history remains truthfully native.

### Phase 9 Slice A interchange and export

Phase 9 Slice A lands the readable Transcript v0.1 projection and strict one-chat Archive v1 export
boundary. Transcript export can project the active path or full lineage. Archive export preserves the
accepted full-fidelity chat-domain graph and uses the strict Archive v1 reader/container validation
contract, with embedded-payload and external-reference policies for ordinary attachments. Archive export
refuses a running generation rather than claiming a full-fidelity snapshot of unsettled chat truth.

This landed export/interchange boundary remains the compatibility input contract for Slice B. Slice B
does not turn chat interchange into whole-install restore. Backup/verification/restore remain separate
Phase 9 domains.

### Phase 9 Slice B archive import and durable provenance

Slice B lands additive migration `0012_phase9_archive_import`, validated Archive v1 intake, persistent
import operations/queue state, fresh local object identities with durable immediate-source provenance,
truthful broken external-attachment references with SHA-based healing, continuation/history bindings,
and recovery/fail-closed integration with the existing data-root authority.

Archive v1 remains strict and frozen. Strict Archive v2 is owned separately by
`infrastructure/archive_v2.py` and carries the provenance/history additions required for lossless later
export/import round trips. The v2 settings-provenance grammar includes truthful branch provenance rather
than translating it into an older category merely for wire compatibility.

Imported historical attempts remain inert source evidence. Local continuation maps receiving-installation
provider/model/settings state separately, without creating or resurrecting provider configuration or
credentials. Editing an imported user turn is an explicit local derivation: the source turn remains
historical truth and the edit creates branch-aware local continuation rather than rewriting imported
history.

Import queue work is serialized per archive while unrelated native work may proceed. Expensive preflight
work stays outside the consequential authoritative write boundary where practical; after that boundary the
operation settles to a known-safe terminal state. Search remains derived and may catch up after a valid
authoritative import rather than becoming an import-success gate.

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
