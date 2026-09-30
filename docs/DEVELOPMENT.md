# Development

## Scope

This repository now contains two related but distinct surfaces:

1. the closed manifest-driven campaign harness (V0/V0.2); and
2. the native Linux desktop product, implemented and landed through Linux v0.1
   Phase 9 closure (Slices A–E) and Phase 10 campaign desktop integration. Phase 11 —
   product finishing and standalone packaging — is next in the accepted sequence and is not
   authorized.

Do not apply old V0 non-goals to the desktop product without checking `LINUX_V0_1_DESIGN.md` and the
current roadmap.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The supported Python range is `>=3.12,<3.15`.

Ordinary deterministic test runs must not spend money or contact a provider. Live/provider acceptance
remains explicit and opt-in. Provider credential environment variables must not be printed or persisted
into repository/test evidence.

## Layout

The legacy campaign harness keeps validation/rendering/usage logic separate from provider, persistence,
and runner I/O. Its manifest runner remains a bounded campaign execution surface.

The Linux desktop is layered around:

- domain/core semantics;
- application commands, queries, workflows, authority and context policy;
- infrastructure adapters for SQLite, rooted filesystem/attachments, providers, secrets and diagnostics;
- a thin native Qt/PySide6 desktop client;
- bootstrap/composition code that wires concrete implementations.

Dependencies should continue pointing inward toward B.O.T.S. semantics. Desktop/UI code must not bypass
the core to mutate authoritative persistence.

## Provider work

V0.2 already contains OpenRouter plus a built-in non-streaming local OpenAI-compatible campaign provider.
The Linux desktop also has a B.O.T.S.-owned generation-backend contract and an explicit legacy Phase 3
`local_openai` compatibility route.

Provider changes must preserve:

- explicit provider selection/configuration;
- non-secret persisted configuration;
- credential indirection through the owned secret/configuration boundary;
- truthful completion/cancellation/remote-outcome semantics;
- no invisible retry once acceptance or spend is uncertain;
- deterministic zero-network tests for ordinary validation.

Do not add plugin discovery merely to add another provider.

## Persistence and authority changes

Linux desktop persistence changes are consequential. Before changing the rooted SQLite, attachment,
startup/recovery, authority, grant, or durability boundaries, read:

- `LINUX_V0_1_DESIGN.md`;
- `UNIFIED_AUTHORITY_EFFECT_INVENTORY.md`;
- `LINUX_V0_1_PHASE6_CLOSURE_REPORT.md`;
- the relevant phase implementation report.

The landed Phase 6 rule is fail-closed: authoritative corruption or uncertain consequential outcomes may
not be silently converted into success. Forward effects require valid authority ownership; release-only
cleanup may settle already-owned resources but must not mint fresh forward authority.

Schema changes require explicit migrations, matching validation/recovery tests, and preservation of the
single-authority data-root contract.

Phase 7 owns additive migration `0010_phase7_search_navigation`; migrations
0001–0009 remain immutable. Its routine query validity checks must stay bounded:
source revision, index state/version, checkpoint/generation/cursor identity, and
bounded authoritative joins. Whole-index integrity and cardinality work belongs at
migration, rebuild, explicit diagnostic, or corruption-recovery boundaries.

Use the rooted Python 3.14/native-VFS test environment for Phase 7 persistence
tests. The bounded subsystem gate is:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest -q \
  tests/test_phase7_core_contracts.py tests/test_phase7_desktop.py \
  tests/test_phase7_migration_authority_faults.py \
  tests/test_phase7_search_navigation.py
```

This deterministic gate has no provider, credential, network, semantic-search, or Phase 8 path.
Phase 7 was accepted, committed, and pushed at `6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`; its
supplied closure was **1,065 passed, 1 expected provider skip, 0 failed**, with no provider/network
activity. Phase 8 is now landed at `f72e0ea6e10694972f3b3455d973651629de448a`; its separate final
T4 gate recorded **1082 passed, 1 skipped, 3124 warnings in 3946.05s (1:05:46)**, exit `0`.
Phase 9 is closed and Phase 10 is landed. The complete repository suite remains a separate
pre-commit closure gate; CI v1 runs it authoritatively (see §Continuous integration).

Phase 8 inspection semantics belong to the core: use `BotsApplication.inspect_chat()` and the typed
`InspectionProjection`; Qt is presentation only. The additive migration is
`0011_phase8_inspector_state` after `0010_phase7_search_navigation`. Preserve request-time provenance,
metadata-only attachment inspection, exact active/historical branch identity, and honest unavailable
or unsupported states. Phase 8 does not itself implement the Phase 9 import/export, backup,
verification, and restore surfaces; those landed in Phase 9, and Phase 10 added the campaign desktop
surface over the headless engine.

## Continuous integration

CI v1 (`.github/workflows/t4.yml`, with correctness logic in `scripts/ci/`) is the authoritative T4
gate. It is dispatch-only: a workflow dispatch names the exact expected commit SHA and fails closed
if the checkout differs. It shards the canonical pytest inventory across standard GitHub-hosted Linux
runners, reconciles the raw per-shard evidence mechanically, and returns one aggregate result bound
to the candidate SHA.

- The population floor and the permitted-skip allow-list are policy and are read from the default
  branch (`tools/scripts/ci/*`), never from the commit under test; a trusted run records
  `trusted_tools: "true"`. If trusted tooling is unavailable, the run fails closed rather than
  judging a candidate with the candidate's own verifier.
- `scripts/ci/t4_baseline_inventory.txt` is the reviewed node-ID population floor. The gate fails if
  any reviewed test is missing; adding tests is allowed, and removing one requires a deliberate
  baseline update. Do not treat any particular current population count as a permanent future count.
- The workflow is documented in `scripts/ci/README.md`; the authoritative run identity, counts, and
  the limitations of the CI evidence are recorded in `docs/CI_V1.md`.

CI does not change the deterministic-test rules above: ordinary automated tests must not contact a
provider or spend money.

## Campaign manifest schema

Externally visible campaign-manifest schema changes require matching validation tests and `JOB_SPEC.md`
updates. Preserve closed-object behavior and deterministic rendering unless a deliberate versioned
contract change says otherwise.

## Manual/live acceptance

Run deterministic tests first. Any real provider/local-model acceptance is a separate explicit action.
For example, the legacy campaign OpenRouter smoke may incur cost:

```bash
export OPENROUTER_API_KEY='...'
bots5 run examples/example-job.json
```

Linux desktop live acceptance should use the phase-specific documented route and must not be folded into
ordinary pytest or automatic CI by accident.
