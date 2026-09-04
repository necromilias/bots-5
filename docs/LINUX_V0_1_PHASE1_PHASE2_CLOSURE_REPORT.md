# B.O.T.S. Linux v0.1 Phase 1 + Phase 2 Closure Report

Date: 2026-09-04

Classification: **IMPLEMENTED AND VALIDATED; HUMAN CLOSURE PENDING**

## Purpose and status

This report records the bounded Linux v0.1 Phase 1 and Phase 2 implementation candidate, its
validation evidence, the resolution of previously discovered blockers, and the boundary for final
human review.

The candidate implements Phase 1, the native walking skeleton, and Phase 2, conversation truth with
immutable lineage/revisions and deterministic fake generation. The final independent GPT-5.6 Sol
with xhigh reasoning audit returned **PASS**. Manual operator smoke testing additionally passed for
native launch, chat creation, fake generation, normal close, reopen, and message persistence across
restart.

This is an implementation and validation status record, not a commit or landing approval. The
candidate remains uncommitted and unstaged pending human review.

## Authority and candidate identity

- Checkout: `/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1`
- Branch: `main`
- Baseline `HEAD`: `664265a603f8175eccfdaee68bdfe0ae68ddd6c8`
- Baseline tree: `24f086d3aac48d8d8e13f6ec52de920a1ec55ffb`
- Local `origin/main`: same SHA; divergence `0/0` observed without fetch
- Before this documentation pass: 46 candidate paths, consisting of 2 modified tracked files and
  44 untracked implementation/test files; no staged or unmerged paths

The accepted design remains `docs/LINUX_V0_1_DESIGN.md`. This report does not advance the roadmap
past Phase 2, alter future scope, or replace the separate human closure and commit gates.

## Implemented scope

### Phase 1: native walking skeleton

- Qt 6/PySide6 Widgets desktop entry point with qasync integration;
- one authoritative headless core behind a thin desktop client;
- platform/XDG data-root resolution and single-authority locking;
- SQLite authoritative state through SQLAlchemy Core and Alembic;
- bounded ordered core event delivery and lifecycle-aware execution;
- deterministic `FakeStreamingBackend` generation;
- durable chats, messages, generation attempts, and restart-safe application state.

### Phase 2: conversation truth

- immutable message identity and append-only lineage;
- explicit parent, lineage, revision, supersedes, active-head, and chat-revision invariants;
- edit and sibling-regeneration workflows with historical-branch reconstruction;
- first-class generation attempts with immutable request snapshots;
- explicit terminal state and finish semantics, including truncated, failed, incomplete, and aborted
  outcomes with partial text retained where applicable;
- current-head validation, connection-local lifecycle guards, atomic pair finalization, restart
  reconciliation, migration backfill, recovery-point retry handling, and bounded shutdown;
- minimal transcript/list/composer Qt presentation over core commands and queries.

The existing campaign implementation remains outside this change. `cli.py`, `runner.py`,
`storage.py`, `manifest.py`, the provider implementations, and the `Provider.complete()` seam were
preserved byte-for-byte relative to `HEAD`.

## Validation evidence

The final independent audit ran on the declared supported runtime:

- Python `3.12.13`;
- PySide6 `6.11.2`;
- qasync `0.28.0`;
- pytest-qt `4.5.0`;
- pytest-asyncio `0.26.0`.

Python 3.13 was unavailable and is not claimed as validated. No package installation was performed
for this closure record.

### Deterministic acceptance suites

Full supported offscreen suite:

```bash
env -u OPENROUTER_API_KEY QT_QPA_PLATFORM=offscreen \
PYTHONDONTWRITEBYTECODE=1 XDG_CACHE_HOME=/tmp/bots5-r11-xdg-cache-01a06a60 \
PYTHONPATH=src .venv/bin/python -m pytest -o addopts='' -p no:cacheprovider \
--basetemp=/tmp/bots5-r11-full-01a06a60 tests
```

Exit `0`; **163/163 passed**.

Legacy preservation suite:

```bash
env -u OPENROUTER_API_KEY PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
.venv/bin/python -m pytest -o addopts='' -p no:cacheprovider \
--basetemp=/tmp/bots5-r11-legacy-01a06a60 \
tests/test_provider.py tests/test_storage_events.py tests/test_runner.py \
tests/test_rendering.py tests/test_prompts.py tests/test_manifest.py \
tests/test_local_provider.py tests/test_cli_views.py tests/test_audit_blockers.py
```

Exit `0`; **113/113 passed**.

The independent audit also recorded focused green results for close/event shutdown (`6/6`),
recovery interruption (`3/3`), restart (`3/3`), and Qt regressions (`2/2`). Migration entry-state,
foreign-key, guard, corruption/open, terminal-state, ordered-event, and canonical qasync probes all
exited `0`.

### Migration, persistence, and adversarial evidence

Fresh, populated `0001`, populated `0002`, and populated `0003` databases all upgraded to
`0004_integrity_guard_function`. Repeated upgrades were idempotent; SQLite integrity was `ok`,
foreign-key violations were zero, foreign keys were enabled, WAL was active, and the final schema
contained 23 triggers without the obsolete lifecycle-marker table. Failed migration and both
recovery-promotion interruption boundaries were retryable.

The adversarial matrix rejected malformed current-head databases, including self-parent and indirect
lineage cycles, absent or empty heads with inconsistent revisions, user heads, heads with children,
unpaired assistants, contradictory terminal pairs, malformed numbers, and malformed timestamps.
Unarmed raw SQLite DML could not arm the connection-local guard, insert beneath the active head,
independently terminalize a pair, delete immutable messages/attempts, or mutate chat head/revision.

The legitimate send, edit, and regenerate path produced immutable historical messages, active-branch
messages, distinct attempts, canonical request snapshots, chat revision `3`, and ordered events
`1..24`. Terminal behavior was verified for stop, non-stop/truncation, missing terminal, backend
failure, exception, and cancellation.

### Offline campaign preservation checks

All three checked-in manifests validated offline with exit `0`:

```bash
env -u OPENROUTER_API_KEY PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
.venv/bin/python -m bots5.cli validate examples/example-job.json
env -u OPENROUTER_API_KEY PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
.venv/bin/python -m bots5.cli validate examples/example-job-v2-local-openai.json
env -u OPENROUTER_API_KEY PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
.venv/bin/python -m bots5.cli validate examples/opv1-controlled-failure/job.json
```

No provider/API call was made and no campaign run directory was created. The existing CLI, runner,
filesystem persistence, manifest semantics, provider files, and `Provider.complete()` remained
unchanged.

### Manual operator smoke evidence

The supplied operator smoke record reports successful native desktop launch, chat creation, send,
deterministic fake-backend completion, normal close, reopen, and persistence of the prior
chat/messages across restart. This is human smoke evidence; it is distinct from the automated
offscreen/qasync acceptance run and from final human closure of the candidate.

## Previously discovered blockers and final disposition

1. **Supported-runtime Qt/qasync evidence — resolved.** Earlier audits could not import PySide6 and
   qasync in the available Python 3.12 environment. The supported Python 3.12.13 environment later
   contained both dependencies, and the full offscreen suite plus the canonical qasync active-window
   close probe passed. Python 3.13 remains unavailable and is not claimed.
2. **Raw-DML active-head invariant — resolved.** The previously reproducible insertion of a user
   message beneath the active head is blocked by the integrity-guard migration and was rejected in
   the independent raw-DML probe. Legitimate application send/edit/regenerate flows passed.
3. **Incomplete head/revision open-time validation — resolved.** Shared current-state validation
   now rejects malformed head/revision relationships and lineage cycles before the store becomes
   authoritative. The independent corruption/open matrix passed.
4. **Recovery-point first-promotion retry failure — resolved.** Recovery metadata handling now
   cleans up or reconstructs the interrupted temporary state, and both first- and second-promotion
   interruption retry probes passed.

The independent audit reported no unresolved closure finding. No runtime or test change was made by
that audit or by this documentation pass.

## Explicit non-goals and remaining gates

This candidate does not add real providers, API or credential integration, attachments, search,
capability discovery, campaign UI/integration, branch-management UI, fancy/HTML rendering, Git
integration, tools, Phase 3 real-generation work, or any other later roadmap phase.

Phase 3 remains future work and requires its own inspect/propose/approve/edit/validate sequence. This
report records implementation and validation only. Human review, staging, commit, and push remain
separate gates.

## Independent audit disposition

**PASS.** The fresh GPT-5.6 Sol/xhigh read-only audit found no unresolved required closure finding.
It confirmed the supported-runtime acceptance results, migration/recovery behavior, persistence and
restart semantics, bounded events and shutdown, qasync behavior, legacy seam preservation, explicit
non-goals, and absence of commit/push or audit mutation.
