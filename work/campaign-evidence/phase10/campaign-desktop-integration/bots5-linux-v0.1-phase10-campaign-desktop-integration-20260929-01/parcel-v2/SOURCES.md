# Authoritative sources and precedence

## Precedence

1. Mick's current explicit instructions/adjudications.
2. Canonical OrgMem authority at the pinned commit.
3. Exact B.O.T.S. implementation/runtime behavior at the pinned implementation commit.
4. Current normative B.O.T.S. technical documentation where consistent with 1–3.
5. Historical reports/campaign evidence as evidence only.
6. Model creator claims/benchmarks as routing evidence only, never B.O.T.S. product authority.

If a lower-precedence source conflicts with a higher-precedence source, preserve the
conflict and follow the higher source. If implementation contradicts intended
authority in a way that creates a product choice, classify a HUMAN_SEMANTIC_FORK.

## Organisational Memory

Repository: `necromilias/organisational-memory`
Ref: `cc0c3c80348b1d798def65988102e0d8ad966730`

Required:
- `core/authority.md`
- `core/approval-boundaries.md`
- `procedures/candidates/CODEX_PROMPT_GENERATION_POLICY.md`
- `procedures/candidates/CODEX_PROMPT_LINT_RULES.md`
- `projects/bots-5/ACTIVE.md`
- `projects/bots-5/README.md`
- `projects/bots-5/decisions/0004-linux-v0.1-product-baseline.md`
- `projects/bots-5/decisions/0005-linux-v0.1-architecture-baseline.md`
- `projects/bots-5/decisions/0006-linux-v0.1-implementation-technology-baseline.md`
- `projects/bots-5/decisions/0007-linux-v0.1-implementation-sequence.md`
- `projects/bots-5/decisions/0012-linux-v0.1-phase9-closure-and-landing.md`

The exact critical blob pins are in `BASELINE.json`.

## B.O.T.S.

Repository: `necromilias/bots-5`
Ref: `0756904481ae884bb9e864e8e1e11fc4a27a72ff`

Mandatory live inspection:
- `src/bots5/manifest.py`
- `src/bots5/models.py`
- `src/bots5/runner.py`
- `src/bots5/storage.py`
- `src/bots5/events.py`
- `src/bots5/usage.py`
- `src/bots5/cli.py`
- `src/bots5/providers/base.py`
- `src/bots5/providers/openrouter.py`
- `src/bots5/providers/openai_compatible.py`
- `src/bots5/bootstrap/desktop.py`
- `src/bots5/core/application.py`
- `src/bots5/desktop/session.py`
- `src/bots5/desktop/window.py`
- `src/bots5/desktop/widgets.py`
- Phase 9 desktop modules where shell/lifecycle reuse is relevant
- existing campaign/desktop/provider/runner/storage tests
- `docs/OPERATING_PROCEDURE_V1.md`
- `docs/OPERATING_PROCEDURE_V2.md`
- `docs/WORKER_CONTRACTS.md`
- `docs/LINUX_V0_1_DESIGN.md`
- `docs/LINUX_V0_1_UI_UX_DRAFT_1.md`
- `docs/LINUX_V0_1_PHASE9_SLICE_E_CLOSURE_REPORT.md`
- `docs/ARCHITECTURE.md`
- `docs/ROADMAP.md`
- `README.md`
- `pyproject.toml`

Do not rely on README summaries alone.

## Model routing sources

See `MODEL_ROUTING_EVIDENCE.md`. Creator pages are routing evidence only.
