# Parcel-v2 v0.3 consistency checks

Authority: OrgMem `procedures/candidates/CODEX_PROMPT_GENERATION_POLICY.md` v0.3
(git blob `bd1f8f246cfe3f319f54f7c55f0cb97c2e7153ca`) and
`procedures/candidates/CODEX_PROMPT_LINT_RULES.md`
(git blob `626dfb48a88e7cd422f9166fbcb897190e48ccab`), both extracted from the pinned OrgMem
commit `cc0c3c80348b1d798def65988102e0d8ad966730` into the workspace-local bare repo
`work/_orgmem-verify` (no external clone mutation) and re-verified against their pinned git
blob hashes.

Scope: `codex-context-parcel-v2.json` and `parcel-v2/*`. Run before the first implementation
worker launch. Layers follow `CODEX_PROMPT_LINT_RULES.md` §"five distinct layers".

---

## Layer 1 — Authority resolution

| Consequential permission | Granted-authority reference | Result |
|---|---|---|
| Product mutation (fence-limited) | `parcel-v2/ADJUDICATION.md` verbatim grant; fence `design/MUTATION_FENCE.json` | PASS |
| Design acceptance | Mick adjudication, seal v4 identity quoted verbatim | PASS |
| Network/provider/credential access | **Not granted.** Tests must use deterministic fakes; no provider canary | PASS (denied) |
| OrgMem mutation | Not granted | PASS (denied) |
| Staging / commit / push / ref mutation | Not granted | PASS (denied) |
| Scope expansion | Not granted; stop-for-Mick rule recorded in `SCOPE.json` and `ADJUDICATION.json` | PASS |
| Product-policy choice (HSF-1…HSF-5) | Mick adjudication — no inference | PASS |
| Budget amendment (MiMo 3→4) | Mick adjudication, with a reservation constraint | PASS |
| Accepted caveats (O-1…O-4) | Mick adjudication explicitly accepts them as non-blocking errata | PASS (not fabricated) |

No `Missing authority provenance`, `Fabricated accepted caveat`, or `Human decision silently
inferred` error.

## Layer 2 — Structural validation

- Required parcel files present and hashed: 11/11, plus `PARCEL_V2_MANIFEST.sha256`.
- Manifest self-check and `codex-context-parcel-v2.json` self-check: **0 mismatches**.
- `worker limit ambiguity`: total child launch limit (32) and max parallel children (4) are
  distinct and explicit — PASS.
- `replacement/reset loophole`: `budget_resets_on_resume=false`,
  `budget_resets_on_restart=false`, continuation records used=11/remaining=21 and states
  explicitly that no cap resets — PASS.
- `overlapping counters undefined`: `overlapping_counter_rule` added (family launches are
  the only child counter; no separate effort counter) — PASS.
- `supervisor budget treatment undefined`: `supervisor_budget_treatment` added — PASS.
- `repair wave undefined`: `repair_wave_definition` added verbatim from the lint rules —
  PASS.
- `budget exhaustion permits new work`: `budget_exhaustion_rule` added — PASS.
- `immutable target with mutation`: `product_target.immutable_target=false` with
  `mutation_permission=fence_limited`; parcel `immutable_after_launch=true` refers to the
  **parcel**, not the product — disambiguated — PASS.
- `commit without Mick` / `push without Mick` /
  `product acceptance inferred from technical completion`: all false, stop rule explicit —
  PASS.
- `migration change without migration validation`: HSF-2 = 2a adds no migration; the fence
  keeps `db/migrations/**` untouched — PASS.
- `full suite without trigger`: the single final T4 has an approved trigger (Linux v0.1
  milestone closure) — PASS.

## Layer 3 — Semantic governance review

- **Routing**: every family has an explicit provider/model id and explicit requested effort;
  roles and not-roles recorded; `silent_substitution=false`; the reserved fourth MiMo launch
  is scoped to the final implementation oracle only — PASS.
- **Blast radius**: fence is 9 modify / 8 add with zero-diff guards, including
  `providers/**`, `core/application.py`, `paths.py`, `manifest.py`, `evidence/**`,
  `pyproject.toml`, `db/migrations/**` — PASS.
- **Repair authority**: bounded to 8 waves inside the fence; recurrence of a semantic defect
  class requires different-family diagnosis — PASS.
- **Evidence**: candidate seal must be materialized **before** independent review
  (`VALIDATION.md`); reviews against unsealed bytes are historical only — PASS.
- **Stop conditions**: genuine human boundary, exhausted authority/budget, or sealed
  pre-commit boundary — PASS.

## Layer 4 — Context-delivery consistency

- Delivery mode: parcel-based. Every worker prompt will reference the parcel and an
  applicable design artifact rather than replaying supervisor history — PASS.
- Parcel immutability: `immutable_after_launch=true`; any change to task-facing parcel files
  requires a new parcel identity — PASS.
- Prompt consistency will be re-checked **per launch** against the rendered prompt: no
  omitted prohibition, no broadened mutation/model/parallelism authority, no changed budget,
  no changed validation obligation, no unrecorded acceptance/commit/push implication, no
  open-ended "continue until fixed" language — to be asserted in each launch record.
- Secret material: parcel contains credential **names and sources** only, never values —
  PASS.

## Layer 5 — Runtime/configuration preflight

- Supervisor model `deepseek/deepseek-v4.1-flash` — configured.
- Requested supervisor effort `provider_maximum_if_exposed` — **not applied**: the harness
  exposes no per-child effort control (`workflow.agent` rejects `effort`). Recorded as
  unverified/not-applied, never as confirmed — PASS (honest).
- Design-family routes verified selectable in `logs/PREFLIGHT_VERIFICATION.md`; all
  implementation-route families are in that selectable set except Jamba
  (`ai21/jamba-large-1.7` MISSING), which is optional and normally unused — PASS.
- Sandbox/permission: workspace-write with approval-ask; no provider/credential network
  access required by any authorized action — PASS.

## Outstanding warnings

- The requested reasoning effort cannot be configured or observed; recorded as **not
  applied** rather than fabricated (`Unsupported configured settings represented as
  verified` avoided). Conscious acceptance: this is a harness limitation, unchanged from the
  design phase, and does not alter any authorized action.
- Jamba is not currently routable; it is optional and normally unused, so no launch depends
  on it. If a Jamba final review were later judged justified, unavailability would be
  recorded rather than substituted.

## Verdict

**No ERROR remains.** Every material WARN is either resolved or consciously accepted inside
existing authority. Parcel-v2 is complete, immutable-for-launch, and consistent with the
adjudicated record. Implementation may proceed.
