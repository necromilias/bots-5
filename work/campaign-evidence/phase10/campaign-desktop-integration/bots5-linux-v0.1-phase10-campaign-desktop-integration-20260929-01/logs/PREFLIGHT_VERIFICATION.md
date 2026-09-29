# Phase 10 preflight verification

Date: 2026-09-29 (session `session-6cec5655-74be-429c-891f-259af09e3df8`).
Supervisor: DeepSeek V4.1 Flash (`deepseek/deepseek-v4.1-flash`), role retained
supervisor/orchestrator (never independent reviewer).

## PACK_MANIFEST.json

- Command: recomputed sha256 of every listed file against `PACK_MANIFEST.json`.
- Result: **PASS** — `file_count` declared 38, entries 38, 0 missing, 0 mismatched.
- `manifest_excludes_itself: true` honored (manifest not in its own list).

## B.O.T.S. baseline (repository `necromilias/bots-5`)

Working tree: `/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1`.

| Item | Expected | Actual | Result |
|---|---|---|---|
| HEAD | `0756904481ae884bb9e864e8e1e11fc4a27a72ff` | same | PASS |
| HEAD^ (parent) | `7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7` | same | PASS |
| HEAD^{tree} | `7fe879035a01a87339dfe6319a435fa0e84c94bd` | same | PASS |
| origin/main | `0756904481ae884bb9e864e8e1e11fc4a27a72ff` | same | PASS |
| HEAD subject | — | `Implement Phase 9 Slice E desktop import queue and restore handoff` | info |

### Index / tracked status

- `git diff --cached --name-only`: empty (nothing staged).
- `git diff HEAD --stat`: empty (no tracked modifications vs HEAD).
- Untracked entries: `.audit-tmp4/`, `.verifyrun/`, `.verifyrun2/`,
  `AUDIT-2026-09-28-evidence-only-review.md`, `work/`.
- No reset/rebase/clean performed. All untracked material preserved.

### Critical B.O.T.S. blob pins (recomputed via `git rev-parse HEAD:<path>`)

All 17 pins match exactly:

`src/bots5/manifest.py`, `src/bots5/models.py`, `src/bots5/runner.py`,
`src/bots5/storage.py`, `src/bots5/events.py`, `src/bots5/cli.py`,
`src/bots5/desktop/window.py`, `src/bots5/desktop/widgets.py`,
`src/bots5/desktop/session.py`, `src/bots5/bootstrap/desktop.py`,
`src/bots5/core/application.py`, `docs/LINUX_V0_1_DESIGN.md`,
`docs/OPERATING_PROCEDURE_V1.md`, `docs/OPERATING_PROCEDURE_V2.md`,
`docs/WORKER_CONTRACTS.md`, `docs/LINUX_V0_1_PHASE9_SLICE_E_CLOSURE_REPORT.md`,
`pyproject.toml`.

## OrgMem baseline (repository `necromilias/organisational-memory`)

- Local checkout `/home/mick/Projects/organisational-memory` was **stale**
  (`HEAD=7150faae868da2bf37f491975f3e787067d750f4`, "Record Phase 9 Slice B closure");
  the pinned commit was absent from that clone.
- Read-only verification route: `git ls-remote` confirmed remote `refs/heads/main`
  = `cc0c3c80348b1d798def65988102e0d8ad966730`. The pinned commit was fetched into a
  **workspace-local bare repo** (`work/_orgmem-verify`, untracked) — no mutation of
  the external clone, no push, no OrgMem content change.
- Pinned commit verified: `cc0c3c8 Record Phase 9 closure`.
- All 9 critical OrgMem blob pins recomputed against
  `cc0c3c8:<path>` and match exactly:
  `core/authority.md`, `core/approval-boundaries.md`,
  `procedures/candidates/CODEX_PROMPT_GENERATION_POLICY.md`,
  `procedures/candidates/CODEX_PROMPT_LINT_RULES.md`,
  `projects/bots-5/decisions/0004-linux-v0.1-product-baseline.md`,
  `projects/bots-5/decisions/0005-linux-v0.1-architecture-baseline.md`,
  `projects/bots-5/decisions/0007-linux-v0.1-implementation-sequence.md`,
  `projects/bots-5/decisions/0012-linux-v0.1-phase9-closure-and-landing.md`,
  `projects/bots-5/ACTIVE.md`.

## Model / effort availability (recorded before first child launch)

- Harness: DSH `0.1.5-rc.3`; provider route `openrouter` (settings `llm-pi-ai`).
- Supervisor configured/effective model: `deepseek/deepseek-v4.1-flash`
  (agent-default-model), matching the campaign requirement. **No substitution.**
- Requested supervisor effort: `provider_maximum_if_exposed`. The DSH child-launch
  hooks (`subagent`, `workflow.agent`) expose **no per-child reasoning-effort
  parameter**; the workflow hook explicitly rejects `effort`. Effective effort for
  child launches is therefore the provider/harness default, not an explicitly pinned
  level. Recorded honestly as a capability limitation, not a substitution.
- Route selectability (read-only OpenRouter models index, no completion/spend):
  all design-route models SELECTABLE —
  `qwen/qwen3.8-flash`, `google/gemini-3.8-flash`, `minimax/minimax-m2.7`,
  `cohere/command-a-plus`, `stepfun/step-3.7-flash`, `z-ai/glm-5.3-flash`,
  `openai/gpt-6-luna`, `xiaomi/mimo-v2.6-flash`, plus helper
  `qwen/qwen3-coder-next` and supervisor `deepseek/deepseek-v4.1-flash`.
- `ai21/jamba-large-1.7` (optional final implementation reviewer, nominally
  unscheduled) was **not present** on the route index. This does not block the
  design phase; it is recorded as a limitation should a final Jamba review ever be
  contemplated.
- Excluded families `upstage/solar-pro4` and `mistralai/mistral-small-2603` are
  present on the route but remain at **max_launches 0** per parcel.

## Governance state at preflight

- Parcel-v1 = design only. No tracked product/test/doc mutation performed.
- No dependencies, no external B.O.T.S. product provider canary, no Phase 11/12, no
  staging/commit/push/ref mutation, no OrgMem mutation.
- Campaign budget at start: 32 total child launches, max 4 parallel, 8 repair waves.

## Verdict

**PREFLIGHT PASS.** Baseline, pins, governance and required model routes verified;
campaign authorized to begin design specialist execution.
