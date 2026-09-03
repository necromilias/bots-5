# Linux v0.1 accepted design

Status: accepted target design; implementation not yet implied.

This document is the build-facing technical contract for the first native Linux B.O.T.S. desktop
application. Organisational Memory owns the full decision rationale and supersession history. This
repository remains authoritative for what is actually implemented.

Current implemented V0/V0.2 behaviour remains unchanged until separately approved code changes land.

## Product target

Linux v0.1 is a real native Linux desktop application. Web UI, PWA, Electron, browser wrappers,
Flatpak, and Snap are not accepted substitutes/distribution requirements for this milestone.

The first useful workload is persistent multi-chat with:

- multiple independent native windows;
- persistent multi-turn chats and fast switching;
- concurrent streaming generations that survive chat/window switching;
- lossless message editing and sibling regeneration through immutable lineage;
- explicit user/model lifecycle states and stop preserving partial output as aborted;
- folders, pin, archive, sorting, duplicate chat, and durable drafts;
- raw Markdown as canonical text with native rich rendering and syntax-highlighted code;
- persistent reusable attachments with content-based deduplication;
- provider/model switching with request attribution;
- automatic, provenance-bearing capability discovery plus manual override;
- editable chat instructions and copy/snapshot instruction profiles;
- global/in-chat literal search with exact branch-aware navigation;
- deterministic, inspectable context construction with no hidden truncation/summarisation;
- governed operator-reviewed summarisation;
- per-request/chat inspection and provenance;
- readable transcript export plus versioned full-fidelity archive import/export;
- explicit whole-chat destructive deletion with confirmation and loss preview;
- a thin operational desktop surface over the existing campaign engine.

Dark theme, scalable UI/text, configurable chat font, keyboard-first command palette/keybindings,
workspace restoration, and generation attention state are v0.1 requirements.

Future multi-chat tiling in one window should remain architecturally affordable but is not required.

## Domain and authority invariants

The core, not the UI or models, owns B.O.T.S. semantics.

A chat is a durable container over immutable historical message lineage. Edits/regenerations create new
nodes or sibling attempts. Individual deletion is lossless/reconstructable; whole-chat deletion is the
ordinary physically destructive chat operation.

Generation/request attempts are separate first-class records with immutable request snapshots.

Persisted authoritative state outranks in-memory state. Rendered UI, search indexes, and high-frequency
progress events are derived or transient.

The dedicated context builder owns exactly what is sent to a model. Equivalent state/settings should
produce equivalent context. Inspection records inclusions/exclusions and reasons.

The architectural invariant is:

**Intelligence is not capability. Capability is not authority. Authority is not execution location.**

Provider APIs, local inference engines, future MCP/plugins/CLIs/native actions/remote nodes, and other
external ecosystems may supply capabilities, but once integrated they operate under B.O.T.S. authority,
provenance, lifecycle, and failure semantics.

Generated tool/action requests are proposals/output only unless a separate B.O.T.S. authority layer
actually grants execution scope. Future operating modes may govern capability envelopes, but no mode
system is implemented solely by Linux v0.1.

## Core/client architecture

Linux v0.1 runs as one process containing one authoritative core and the Qt desktop.

The core remains deliberately separable and headless-testable. Multiple windows are clients/views over
one core state. A future network/daemon boundary may be added in front of the same semantics, but no
persistent daemon is required for v0.1.

Clients use commands for mutation, queries for authoritative state, and events for changes that already
occurred. Clients never directly mutate persistence.

The core owns asynchronous work through a central execution manager. Abort targets individual
operations. Meaningful progress is durable; presentation updates may be transient.

Future Android/remote clients must talk to the authority rather than opening its database.

## Persistence and filesystem

Use SQLite as the authoritative mutable application-state database, local to the authority host.

Use SQLAlchemy 2 Core behind an AppStateStore boundary and Alembic for explicit schema migrations.
Enable SQLite foreign keys explicitly and configure WAL/concurrency deliberately.

Do not place the live authoritative SQLite database on Samba/NFS for multi-host access.

Attachment payloads are filesystem-backed and content-addressed with SHA-256. Attachment records remain
separate database objects so one payload may preserve many filenames/provenance/references.

Zero durable references permit garbage collection. Historical lossless lineage counts as a reference.
Provider-specific transformed files are derived representations and never replace the original.

Use platformdirs/XDG locations for durable data, bootstrap configuration, diagnostic state, and cache.
Allow deliberate storage-root overrides while preserving a discoverable complete backup boundary.

## Identity and time

Use UUIDv7 for durable domain identity behind a B.O.T.S.-owned ID factory.

Use timezone-aware UTC timestamps for authoritative event times and a monotonic clock for elapsed-time
measurement/timeouts.

Do not use timestamps or UUID order as the sole source of causal ordering. Use explicit revision/sequence
numbers where stale writes or ordering matter.

Application version, database migration revision, archive-format version, and future protocol/schema
versions are independent.

## Desktop stack

Use Qt 6 through PySide6 with Qt Widgets as the primary UI.

Use qasync at the desktop boundary to integrate Qt and the asyncio core. Keep the integration adapter
replaceable.

Use Pydantic v2 for consequential boundary/interchange validation, not for every trusted internal object.

Use a B.O.T.S.-owned asyncio pub/sub event bus, primarily bounded per-subscriber queues. Qt signals may
adapt events to the UI, but core code does not depend on Qt.

Raw Markdown remains canonical. Parse with markdown-it-py with raw HTML disabled for untrusted content,
use Pygments for fenced-code analysis/highlighting, and render with native Qt rich-text/document tools.
Do not use Qt WebEngine.

## Secrets

Use a core-owned SecretStore abstraction.

The Linux desktop uses Linux Secret Service through Python keyring for interactive provider credentials.
Environment-variable credentials remain an explicit supported source for headless/deployment use.

Never silently fall back to plaintext credential storage. Database/client/log/export state contains only
non-secret configuration, references, and credential status.

## Diagnostics and failures

Use structured correlation-aware diagnostic logging, initially structlog over Python logging, with
bounded machine-readable persistent logs. Logs are diagnostic evidence, not authoritative application
state.

Use structured B.O.T.S.-level failures across subsystem boundaries. Distinguish expected operational
failure, invalid/stale operation, internal bug/invariant violation, and integrity failure. Preserve
sanitized underlying cause for diagnosis.

Lifecycle state and failure detail are separate.

Impossible or integrity-threatening state fails loudly rather than being silently massaged into a
plausible shape.

## Generation backend contract

B.O.T.S. owns a generation-capability contract rather than an OpenAI-defined internal model.

Adapters translate immutable B.O.T.S. requests to backend-native protocols and normalize streams/results
back into typed B.O.T.S. output events, metadata, and terminal outcomes.

OpenAI-compatible implementations may share substantial adapter code, but backend-specific controls and
capabilities remain available through optional namespaced extensions.

The event envelope must not permanently assume all assistant output is one string. Text is required now;
future typed output may include structured data, reasoning where exposed, citations, action requests,
media references, or other capability-specific parts.

A stream ending does not automatically mean complete. Cancellation, cost/usage, and remote outcome are
reported with actual certainty.

Generation requests are never invisibly retried once external acceptance or spend is uncertain.

## Capability discovery and configuration

Capability discovery is automatic by default, inspectable always, and manually overrideable.

Resolve capabilities per backend/provider connection plus model. Manual override wins; confirmed
endpoint behaviour outranks provider metadata; provider metadata outranks trusted registry metadata;
heuristics are lower; unknown remains valid.

Runtime contradictions are evidence, not automatic permanent overrides unless the failure specifically
establishes the capability fact.

Configuration precedence is category-specific. Ordinary generation settings inherit broad-to-specific;
capability data, credentials, bootstrap/storage configuration, and future authority policy remain owned
by their respective subsystems.

Consequential effective values retain provenance and freeze into request snapshots.

## Search

Use SQLite FTS5 as a derived full-text index for v0.1. Authoritative structured metadata remains in
normal tables. Search failure/staleness cannot invalidate authoritative writes; the index must be
rebuildable.

Start with predictable Unicode word search. Revisit the backend when measured indexing/query latency,
database size, rebuild time, or write amplification becomes materially annoying.

No semantic search, embeddings, or RAG in v0.1.

## Backup, migration, and recovery

B.O.T.S. maintains one programmatically discoverable backup boundary covering every authoritative state
component and referenced durable payload needed for recovery. Cache is excluded; logs are optional;
secrets are excluded unless an explicit future secret-export mechanism is designed.

Before consequential schema migration, create and verify a recovery point.

Supported forward migrations use Alembic. Refuse databases newer than the running application's
supported schema. Do not use automatic downgrade as the normal recovery strategy; restore the
pre-upgrade backup and use a compatible older application.

Backups carry a manifest and integrity information. Restore validates before replacing live state,
preserves the current installation, stages/verifies restored state where practical, then adopts it.
Independent backup verification must be possible without altering live state.

Exactly one B.O.T.S. core may own an authoritative data root at a time. Multiple windows/future clients
share that authority. Startup locking must recover safely from stale crash state.

## Lifecycle

Startup order is: resolve paths, acquire authority lock, open/validate/migrate persistence, initialise
durable services, reconcile interrupted state, start execution, restore clients/workspace, then become
ready.

Closing one window does not kill the core if other windows remain.

Full application exit with active work requires explicit confirmation, requests operation cancellation,
preserves partial state, records uncertainty honestly, performs bounded graceful shutdown, and then
releases authority.

Once shutdown begins, reject new consequential commands.

## Package boundaries

Use a technology-agnostic domain layer; application/core layer for commands, queries, workflows, policy,
context, configuration/capability resolution, and orchestration; infrastructure adapters for concrete
persistence/backends/search/secrets/files/diagnostics/campaign/import-export; thin clients such as Qt;
and a dedicated bootstrap/composition layer that wires concrete implementations.

Dependencies point inward toward B.O.T.S. semantics.

Domain code does not import Qt. Core workflows do not contain SQLAlchemy queries. Provider/backend
adapters do not manipulate widgets. The desktop does not bypass the core to mutate persistence.

Avoid generic `utils` dumping grounds and abstraction layers that protect no real boundary.

## Testing

Keep pytest as the test foundation.

Use pytest-asyncio for core concurrency, pytest-qt for native UI behaviour, and Hypothesis selectively
for invariants/round trips.

Use real disposable SQLite databases and real migrations in integration tests.

Provider-test tiers are:

1. deterministic fake provider, always safe and zero inference;
2. opt-in local real-model testing through Ollama/llama.cpp with a known model such as Qwen;
3. explicitly invoked external-provider smoke tests only when deliberately approved.

Automated tests must never incur unapproved provider/API spend.

## Packaging

Use pyside6-deploy/Nuitka. Prefer standalone mode as the canonical packaged application because it is
more transparent to diagnose.

AppImage is optional convenience packaging after the standalone build is reliable.

Flatpak and Snap are excluded unless the operator explicitly reverses that decision.

Application data remains external to the application bundle and deployment configuration is
version-controlled.

## Explicit review triggers

- Python 3.12/qasync: when qasync supports Python 3.14 or QtAsyncio becomes the stronger supported
  integration, reassess and preferably move the floor to Python 3.14+ if no other dependency blocks it;
- FTS5: review when measured search/index/rebuild/storage cost becomes materially annoying;
- structlog: review after real operational data; remove if standard logging is sufficient;
- Secret Service: review if packaged desktop integration is unreliable or headless deployment becomes
  dominant;
- Markdown stack: simplify to Qt-native parsing if prototype evidence shows the extra parser layer adds
  no useful structure;
- packaging: change only if the chosen deploy path proves unreliable or real distribution needs justify
  another mechanism.

Temporary compatibility debt must retain an explicit expiry/review condition rather than becoming
unexplained legacy.

## Implementation sequence

Build through validated vertical slices:

1. Phase 0: durable design authority and live-repo reinspection;
2. Phase 1: walking skeleton with native UI, core, persistence, events, and fake streaming backend;
3. Phase 2: conversation truth/lineage/revisions with fake generation;
4. Phase 3: real generation-backend contract, streaming, cancellation, checkpointing, local-model test;
5. Phase 4: concurrency, multi-window workspace, shutdown, crash reconciliation;
6. Phase 5: provider/model usability, secrets, catalogue, capability discovery, settings;
7. Phase 6: deterministic context and content-addressed attachments;
8. Phase 7: search and exact navigation;
9. Phase 8: inspection/provenance UX;
10. Phase 9: import/export, backup, verification, and restore;
11. Phase 10: campaign desktop integration;
12. Phase 11: product finishing and standalone packaging;
13. Phase 12: Linux v0.1 torture run and separate closure adjudication.

Every phase retains inspect -> propose -> approve -> edit -> validate -> separate commit approval ->
separate push/landing approval.

## Explicitly deferred future work

Unless a concrete Linux v0.1 blocker forces a bounded boundary decision, defer Android, Code/Git,
persistent daemon/remote-client implementation, MCP/general tool/plugin framework, scheduler/automation,
remote B.O.T.S. execution nodes, RAG/semantic search, Chat/Research/Work/Blood Oath mode framework,
mode-specialised post-training/LoRA selection, and multi-capability workflow composition.

The architecture intentionally leaves room for these without implementing them now.
