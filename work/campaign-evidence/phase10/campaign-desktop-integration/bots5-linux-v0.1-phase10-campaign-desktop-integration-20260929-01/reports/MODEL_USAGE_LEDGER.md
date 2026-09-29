# Phase 10 design-phase model routing and usage ledger

Campaign: `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01`
Harness: DSH `0.1.5-rc.3`, provider route `openrouter` via `llm-pi-ai` settings.
Supervisor: `deepseek/deepseek-v4.1-flash`.

## Routing truth

- Configured supervisor model: `deepseek/deepseek-v4.1-flash`.
- Requested supervisor effort: `provider_maximum_if_exposed`.
- **Effective supervisor effort: not applied.** The harness exposes no per-child (and no
  per-request) reasoning-effort control; `workflow.agent()` explicitly rejects an `effort`
  option. This is recorded as a harness limitation, **not** silently substituted.
- Child model override is available only through `workflow.agent(prompt, {provider, model})`.
  The `subagent` tool exposes no model parameter, so every model-specific child in this
  campaign was launched through `workflow.agent` with an explicit `provider`/`model`.
  No child was ever launched without an explicit model override.
- Maximum 4 concurrent children; `parallel()` used only where phases were independent.

## Launch accounting (design phase)

Budget total: 32 child launches. Design phase used **11**.

| # | Model | Purpose | Outcome |
|---|---|---|---|
| 1 | `qwen/qwen3.8-flash` | Campaign archaeology (first, per ordered protocol) | `QWEN_CAMPAIGN_ARCHAEOLOGY.md`, 924 lines |
| 2 | `google/gemini-3.8-flash` | Evidence/regeneration architecture | `GEMINI_EVIDENCE_REGEN_ARCHITECTURE.md`, 612 lines |
| 3 | `minimax/minimax-m2.7` | Lifecycle failure forensics | `MINIMAX_LIFECYCLE_FORENSICS.md`, 569 lines |
| 4 | `cohere/command-a-plus` | Operator-authority UX | `COMMAND_OPERATOR_AUTHORITY_UX.md`, 140 lines |
| 5 | `stepfun/step-3.7-flash` | Validation architecture | `STEP_VALIDATION_ARCHITECTURE.md`, 766 lines |
| 6 | `z-ai/glm-5.3-flash` | Feasibility/fence (broad request) | **null / no report** — matches the documented MODEL_ROUTING_EVIDENCE Slice D failure mode |
| 7 | `z-ai/glm-5.3-flash` | Feasibility/fence (narrow, bounded retry, early-write) | `GLM_FEASIBILITY_AND_MUTATION_FENCE.md` + `GLM_PROPOSED_FENCE.json` |
| 8 | `openai/gpt-6-luna` | Fresh independent design falsification vs seal v1 | `GPT6_LUNA_DESIGN_FALSIFICATION.md` — 8 repairable defects + 1 human fork |
| 9 | `xiaomi/mimo-v2.6-flash` | Fresh final design oracle vs seal v2 | `MIMO_FINAL_DESIGN_ORACLE.md` — REPAIRABLE_DESIGN_DEFECT (M-1…M-6) |
| 10 | `xiaomi/mimo-v2.6-flash` | Fresh final design oracle vs seal v3 | `MIMO_FINAL_DESIGN_ORACLE_v3.md` — PASS_WITH_LIMITATIONS (N-1…N-6, non-blocking) |
| 11 | `xiaomi/mimo-v2.6-flash` | Fresh final design oracle vs seal v4 | `MIMO_FINAL_DESIGN_ORACLE_v4.md` — PASS_WITH_LIMITATIONS (O-1…O-4, non-blocking) |

## Per-family budget status at the design gate

| Family | Cap | Used | Remaining |
|---|---|---|---|
| Qwen3.8 Flash | 7 | 1 | 6 |
| Gemini 3.8 Flash | 4 | 1 | 3 |
| MiniMax M2.7 | 4 | 1 | 3 |
| Command A+ | 2 | 1 | 1 |
| Step 3.7 Flash | 4 | 1 | 3 |
| GLM 5.3 Flash | 8 | 2 | 6 |
| GPT-6 Luna | 4 | 1 | 3 |
| **MiMo V2.6 Flash** | **3** | **3** | **0 (exhausted)** |
| Qwen3-Coder-Next | 4 | 0 | 4 |
| Jamba | 1 | 0 | 1 (optional final implementation reviewer only) |
| Solar Pro 4 | 0 | 0 | not authorized |
| Mistral | 0 | 0 | not authorized |

## Consequence Mick must weigh

The MiMo family cap (3) is **exhausted** by the design phase. The design protocol assigned
the implementation-phase final oracle to a fresh MiMo launch; that capability is no longer
available inside the current parcel's budget. Options at the gate:

1. Authorize a bounded budget amendment for one further MiMo launch at the
   implementation-phase final-oracle step; or
2. assign the implementation-phase final oracle to a different **independently authorized**
   family that has remaining budget (e.g. GPT-6 Luna, cap 4, 3 remaining); or
3. accept a different explicit substitute that Mick names.

The supervisor will not silently substitute a model.

---

# Implementation-phase launch accounting (appended; the design-phase table above is unchanged)

Budget authority: `parcel-v2/BUDGET.json`. Total child launches 32; per-family caps are the only
child counters; a launch that fails still consumes its family allocation
(`replacement_counts_as_launch: true`).

| # | Family | Purpose | Outcome |
|---|---|---|---|
| 12 | GLM 5.3 Flash | M0.0 backward-compatibility fixtures | 15 tests, pass |
| 13 | GLM 5.3 Flash | M0.1a models | additive fields + snapshots |
| 14 | GLM 5.3 Flash | M0.1b storage | attempt/selection/approval persistence |
| 15 | GLM 5.3 Flash | M0.2 errors/events/usage | typed refusal, 7 event kinds, dual accounting |
| 16 | GLM 5.3 Flash | M0.3a runner v2 path + cancellation + provenance | landed |
| 17 | GLM 5.3 Flash | M0.3b regeneration and synthesis rerun | landed |
| 18 | GLM 5.3 Flash | M0.4 CLI parity | landed |
| 19 | GLM 5.3 Flash | M1.0 Qt-free campaign bridge | landed |
| 20 | GLM 5.3 Flash | M2.0 desktop dock + attach (broad, 3 files) | **null / delivered nothing** |
| 21 | Qwen3 Coder Next | M2.0a campaign dock widget | 897-line dock, landed |
| 22 | Qwen3 Coder Next | M2.0b dock attach + runtime wiring | landed |
| 23 | GLM 5.3 Flash | T1a regeneration + preflight test contracts | 23 tests |
| 24 | Qwen3 Coder Next | T1b projection + lifecycle + cross-cutting tests | 33 tests |
| 25 | Qwen3.8 Flash | Broad implementation falsification | **null / delivered nothing** |
| 26 | Qwen3.8 Flash | Narrow falsification retry (early-write) | **null, but left a partial report file** |
| 27 | GPT-6 Luna | Completed falsification areas A2-A5 | 5 confirmed findings |
| 28 | GPT-6 Luna | Repair wave 1 (engine truth) | landed |
| 29 | GPT-6 Luna | Repair wave 2 (HSF-1 Branch A pricing) | landed |
| 30 | MiniMax M2.7 | Post-repair targeted lifecycle review | all findings CLOSED |
| 31 | MiMo V2.6 Flash | Reserved fresh final implementation oracle | see oracle report |

## Per-family budget status at the pre-commit boundary

| Family | Cap | Used | Status |
|---|---|---|---|
| Qwen3.8 Flash | 7 | 3 | 4 remaining |
| Gemini 3.8 Flash | 4 | 1 | 3 remaining |
| MiniMax M2.7 | 4 | 2 | 2 remaining |
| Command A+ | 2 | 1 | 1 remaining |
| Step 3.7 Flash | 4 | 1 | 3 remaining |
| **GLM 5.3 Flash** | **8** | **12** | **OVER CAP BY 4 — see below** |
| GPT-6 Luna | 4 | 4 | at cap (compliant) |
| MiMo V2.6 Flash | 4 | 4 | at cap; the fourth launch was the reserved final implementation oracle and was used only for that |
| Qwen3 Coder Next | 4 | 3 | 1 remaining |
| Jamba Large 1.7 | 1 | 0 | unused |
| Solar Pro 4 / Mistral Small 2603 | 0 | 0 | not authorized |

Total child launches: **31 of 32**.

## Recorded budget breach — GLM 5.3 Flash

The GLM 5.3 Flash family cap is 8. This campaign used **12**: the 2 design-phase launches
recorded above plus 10 implementation-phase launches (rows 12-20 and 23).

The cap was reached at the end of M0.3b (design 2 + implementation 6 = 8). The four launches
after that point — M0.4, M1.0, M2.0 (which returned null and delivered nothing), and T1a —
were made without re-checking the per-family cap, so they exceeded the allocation by four.
`replacement_counts_as_launch` means the failed M2.0 launch counts as one of them.

This is a **supervisor accounting failure, not a product defect**. It is recorded here rather
than concealed, and it is reported to Mick at the pre-commit boundary.

Consequences assessed:

- The breach consumed 4 launches that should have been spread across the other authorized
  families. `Qwen3 Coder Next` (1 remaining), `Qwen3.8 Flash` (4 remaining), `MiniMax M2.7`
  (2 remaining), `Step 3.7 Flash` (3 remaining) and `Command A+` (1 remaining) all still hold
  unused allocation, so the breach did not by itself strand the campaign: the remaining
  authorized work fits inside the untouched families and the reserved MiMo oracle.
- No new forward work was launched on the GLM family after the breach was detected, and none
  will be. Per `budget_exhaustion_rule`, no further GLM forward work may be launched.
- The work products produced by the over-cap launches are not invalidated as engineering
  artifacts: every one of them was independently reviewed (the adversarial falsification and
  the lifecycle review both verified the sealed candidate byte-for-byte), and the T3 and final
  T4 gates exercise the result. The breach is a governance deviation to be weighed by Mick,
  not a reason to discard verified work.
- Global launch budget was not exceeded: 31 of 32.

Mick may wish to treat this as requiring an explicit budget amendment or as a recorded
deviation accepted against the delivered evidence; the supervisor takes no position beyond
recording it precisely.

**Routing substitution also recorded:** `Qwen3.8 Flash` holds the role "fresh broad
implementation falsification". Two launches on that route returned null (rows 25 and 26), so
the falsification was completed on `openai/gpt-6-luna` (row 27), whose authorized roles include
"difficult cross-family diagnosis after recurring/conflicting implementation evidence". The
substitution is explicit here and in `logs/IMPLEMENTATION_LOG.md`; both failed Qwen3.8 launches
consumed Qwen3.8 allocation.


## Effort-limitation note

For every launch above, requested effort `provider_maximum_if_exposed` /
`max_if_exposed_else_highest_available` was recorded as **not applied**, because the harness
exposes no per-child effort control. This is stated in every specialist report, both repair
ledgers and every oracle report, and is not a substitution.

## Addendum — final launch (row 32) and corrected totals

The table above stops at row 31 and therefore states 31 of 32. One further launch was made after
that table was written, and no launch followed it. Appended rather than rewritten, per the
append-only ledger discipline.

| # | Family | Task | Outcome |
|---|---|---|---|
| 32 | Qwen3 Coder Next | Repair wave 3 (D-1 inert dock, D-2 dialog, D-3 output, D-4 attempt switcher, D-9 status, widget-level tests) | landed, but **destroyed the whole lifecycle test file** — detected by the supervisor from the falling T4 count and fully reversed without a further launch |

Corrected totals at the sealed pre-commit boundary:

- Total child launches: **32 of 32 (global budget exhausted).**
- `Qwen3 Coder Next`: 4 of 4 — at cap (was shown as 3 of 4 above).
- Repair waves: 3 of 8.
- No further launch of any family is possible or authorized.

The eight destroyed lifecycle tests were recovered without spending a launch, by reconstructing
the pre-clobber source from the DSH session transcripts and merging it with repair wave 3's three
genuine widget-level tests. See `logs/IMPLEMENTATION_LOG.md` and `logs/recovery/`.

Residual consequence, stated plainly: because the global and MiMo budgets are now exhausted, the
repaired candidate (seal v4) has **not** been re-oracled, and no launch remains to do so.

## Addendum 2 — launches 33 to 35 under the amended ceilings

| # | Family | Task | Outcome |
|---|---|---|---|
| 33 | MiMo V2.6 Flash | Reserved fresh final implementation oracle vs candidate v4 | **FAIL** — 1 CRITICAL, 1 HIGH, 4 MEDIUM, 5 LOW; all in-fence repairable. Report: `reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v2.md` |
| 34 | GLM 5.3 Flash | Repair wave 4a: V-6 storage write-ordering fix + fault-injection regression test | landed |
| 35 | GLM 5.3 Flash | Repair wave 4b: V-3, V-4, V-10 test coverage | landed |

Amended ceilings (Mick, 2026-09-30, recorded verbatim in `logs/BUDGET_AMENDMENT_2026-09-30.md`):

- Global child-launch ceiling: **36** (used 35; launch 36 reserved exclusively for the MiMo oracle
  against the repaired candidate v5).
- GLM 5.3 Flash family allowance: **14**, of which all 14 are now used. The two wave-4 launches
  were the last GLM launches authorized, and they were implementation work only.
- MiMo V2.6 Flash: 5 of 6 used; the sixth is reserved for the v5 oracle.

One dispatch attempt for the wave-4 workers initially failed inside the workflow harness before any
child session or work product existed (the script returned un-awaited promises). No child session
was created and no artifact was produced; those two launches are recorded as not consumed, and the
work was re-dispatched as launches 34 and 35. Flagged here for completeness.

Correction: the reserved launch 36 runs against candidate **v6**
(candidate_id `d5c433073332b9a0c0de3eaf7dc93d8550168348f8f0603c0e888515181f2946`, seal sha256
`36078b8838dd1d6266e732e6161ce1bbb7979bb64e9328de4d2957a1cd103197`), not v5. Candidate v5 was
sealed and its T4 started, then killed when a coverage audit found V-5 had no regression guard;
the guard was added, the fenced suite re-run (96 passed), and the candidate resealed. See
`logs/IMPLEMENTATION_LOG.md`, "Candidate v5 superseded by v6".

## Addendum 3 — launch 36 (the last authorized launch)

| # | Family | Task | Outcome |
|---|---|---|---|
| 36 | MiMo V2.6 Flash | Reserved fresh final implementation oracle vs candidate v6 | Ran the full assignment, completed all 9 verification milestones, then **died while writing its report** (five consecutive upstream idle timeouts). Returned null; no oracle-authored report exists. Findings recovered by the supervisor from the run's transcript. |

Final accounting: **36 of 36 child launches used.** MiMo V2.6 Flash 6 of 6; GLM 5.3 Flash 14 of 14.
No launch of any family remains, and none is authorized.

Because the run died before reporting, its evidence is preserved as a clearly-labelled
**supervisor reconstruction** at
`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v3_SUPERVISOR_RECONSTRUCTION.md`, together with the
run's own scratch directory `logs/demo-scratch/.oracle-v3/`. It found **no candidate-changing
defect** and raised two TEST_GAPs (F-2, F-3). The reconstruction is not an oracle-authored document
and is marked as such throughout.
