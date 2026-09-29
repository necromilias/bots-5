# Retained continuation — Phase 10 implementation campaign

## Identity

| | |
|---|---|
| Campaign id | `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01` |
| Continuation id | `...-continuation-01-implementation` |
| Predecessor parcel | `...-parcel-v1` (design-only) — `codex-context-parcel.json`, superseded |
| Active parcel | `...-parcel-v2` — `codex-context-parcel-v2.json`, immutable after launch |
| Parcel-v2 manifest | `parcel-v2/PARCEL_V2_MANIFEST.json`, sha256 `45fd91f3f383620e4da7575c7a5ba92abf9298ed47c72cf7eb30a315c854391b` |
| Design of record | `seals/design/DESIGN_SEAL_v4.json`, seal id `9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1` (frozen, not mutated) |
| Mutation fence | `design/MUTATION_FENCE.json`, sha256 `602e6d5d3a88eb893d11d064dfc28c70e8f692c52cb823cd0c8b2711907cf452` (9 modify, 8 add, unchanged) |
| Baseline | HEAD `0756904481ae884bb9e864e8e1e11fc4a27a72ff`, parent `7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7`, tree `7fe879035a01a87339dfe6319a435fa0e84c94bd` |
| OrgMem authority | `cc0c3c80348b1d798def65988102e0d8ad966730` |

## Design phase outcome

Ordered design execution completed: Qwen archaeology → parallel Gemini/MiniMax/Command/Step
specialists → GLM feasibility/fence → integrated sealed design → fresh GPT-6 Luna
falsification → two bounded repair+reseal rounds → three fresh MiMo oracles. Final oracle
verdict on seal v4: **PASS_WITH_LIMITATIONS, no blocking findings**, with residuals
O-1…O-4 accepted by Mick as non-blocking errata.

## Human gate — satisfied

Mick accepted design seal v4 as the implementation design and granted implementation
authority inside the unchanged fence. Verbatim adjudication: `parcel-v2/ADJUDICATION.md`.
Structured: `parcel-v2/ADJUDICATION.json`. Forks: HSF-1 → A, HSF-2 → 2a, HSF-3 → 3a,
HSF-4 → 4a, HSF-5 → 5a. Errata: `parcel-v2/NORMALIZED_CORRECTIONS.md`.

## Budget state at continuation start

- Total child launch limit 32; used 11; remaining 21.
- Per family: Qwen3.8 1/7, Gemini 1/4, MiniMax 1/4, Command 1/2, Step 1/4, GLM 2/8,
  GPT-6 Luna 1/4, **MiMo 3/4 (fourth reserved for the final implementation oracle)**,
  Qwen3-Coder-Next 0/4, Jamba 0/1, Solar 0, Mistral 0.
- Bounded repair waves 8; used 0.
- No reset of any cap by this continuation (`budget_resets_on_resume=false`).

## Current phase and next actions

Phase: **implementation authorized**. Sequence (`design/IMPLEMENTATION_SEQUENCE.md`,
`parcel-v2/VALIDATION.md`): M0.0 backward-compat fixtures (gate-first) → M0.1 models/storage →
M0.2 events/usage → M0.3 runner → M0.4 CLI → M1.0 bridge → M2.x desktop → T3 cross-cutting →
candidate freeze + seal → independent review → bounded repair/reseal → final serial T4 →
reseal verification → reserved MiMo implementation oracle → `reports/COMMIT_BOUNDARY_REPORT.md`
→ STOP at the pre-commit boundary.

## Authority boundaries (unchanged)

No staging, commit, push, ref mutation, OrgMem mutation, dependency change, provider/API
canary, Phase 11/12 work, or mutation outside the fence. Stop for Mick if the fence,
accepted product semantics, dependency boundary, or any adjudication must change.

## Retained evidence index

- Design: `design/` (12 artifacts, sealed under v4); seals: `seals/design/` (v1–v4).
- Specialists: `reports/design/QWEN_*`, `GEMINI_*`, `MINIMAX_*`, `COMMAND_*`, `STEP_*`,
  `GLM_*`; repair ledgers `reports/design/DESIGN_REPAIR_RECORD_v1_to_v2.md`,
  `_v2_to_v3.md`, `_v3_to_v4.md`.
- Falsification: `reports/review/GPT6_LUNA_DESIGN_FALSIFICATION.md`.
- Oracles: `reports/oracle/MIMO_FINAL_DESIGN_ORACLE.md`, `_v3.md`, `_v4.md`.
- Preflight: `logs/PREFLIGHT_VERIFICATION.md`; usage: `reports/MODEL_USAGE_LEDGER.md`.
