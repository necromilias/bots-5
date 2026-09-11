# Development

## Scope

This repository now contains two related but distinct surfaces:

1. the closed manifest-driven campaign harness (V0/V0.2); and
2. the native Linux desktop product, implemented and landed through Linux v0.1 Phase 6.

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
