# Design integration record

Supervisor: DeepSeek V4.1 Flash, configured/effective route `deepseek/deepseek-v4.1-flash`
(retained supervisor/orchestrator, never an independent reviewer). Requested effort
`provider_maximum_if_exposed`; the harness exposes no per-child reasoning-effort parameter,
recorded honestly as not applied.

## Inputs integrated

| Order | Specialist | Model | Report | SHA-256 |
|---|---|---|---|---|
| 1 | Qwen archaeology | `qwen/qwen3.8-flash` | `reports/design/QWEN_CAMPAIGN_ARCHAEOLOGY.md` | `a1a8240bbe8d81e697117bbc3a37df7b8e5e6641b109220aba5df8af5bb94a3d` |
| 2 | Gemini architecture | `google/gemini-3.8-flash` | `reports/design/GEMINI_EVIDENCE_REGEN_ARCHITECTURE.md` | `c90d61c38ca89b0eee6b26b340219019c512f42564f49000d9c7319ccf7c90ba` |
| 3 | MiniMax lifecycle forensics | `minimax/minimax-m2.7` | `reports/design/MINIMAX_LIFECYCLE_FORENSICS.md` | `555c648e08ae01a4f3df566f5141daf430af143b8bc55b188e563581fd232673` |
| 4 | Command A+ operator authority UX | `cohere/command-a-plus` | `reports/design/COMMAND_OPERATOR_AUTHORITY_UX.md` | `724551264ca287e182a84e1c0a7fe3e6837d8c10f5bca6f82b8f2b0de141b2fc` |
| 5 | Step validation architecture | `stepfun/step-3.7-flash` | `reports/design/STEP_VALIDATION_ARCHITECTURE.md` | `152bc8def3c7b7bea082a55d70a3ac1128126bd7b866f5b5a4e820e8d28fd384` |
| 6 | GLM feasibility/fence | `z-ai/glm-5.3-flash` | `reports/design/GLM_FEASIBILITY_AND_MUTATION_FENCE.md` | `b32cc8d5a2d6b3af8c2f87d6548e9aaab824fcdc3618e85127ec58fa6bd4e9fd` |
| 6b | GLM machine fence | `z-ai/glm-5.3-flash` | `reports/design/GLM_PROPOSED_FENCE.json` | `cc8087abff2b5dc83dd827d99ada0e49c8b45c554adf834b9bf36141f8c05faf` |

Launch accounting: 7 child launches used in the design phase (1 Qwen + 4 parallel
specialists + 1 failed broad GLM + 1 narrow GLM retry). Parcel limit: 32.

## Integration operations

1. **Qwen first**, as the shared evidence base; later specialists read it.
2. **Gemini / MiniMax / Command / Step in parallel** against the same pinned baseline and
   the Qwen report.
3. **GLM after** the specialist reports existed, to test the integrated direction against
   the live code and produce the exact fence. The first (broad) GLM request returned null —
   the exact failure mode recorded in `MODEL_ROUTING_EVIDENCE.md` for Slice D. A narrower,
   bounded retry succeeded, also matching the recorded observed behavior.
4. **Supervisor integration**: reconciled one exact design; preserved disagreement in
   `SPECIALIST_DISAGREEMENTS.md`; classified five genuine forks in
   `HUMAN_SEMANTIC_FORK_REGISTER.md`; produced the nine required design artifacts plus this
   record, the disagreement register, the fork register.

## Adopted from each specialist

- **Qwen**: the live engine/evidence reconstruction, the [X] conflict register, the
  `load_stage_view` containment constraint, the absence of any pricing authority and of any
  run-discovery seam, the "no reader for events.jsonl" fact, the old-run compatibility test
  gap, and the `CancelledError` gap.
- **Gemini**: `evidence_version: 2`, flat attempt layout, `selection.json`, dependency
  provenance + mechanical staleness, `PreflightSnapshot`/`ApprovalRecord`, dual cost
  accounting, the 21-state forensic matrix, and the seam inventory.
- **MiniMax**: the adversarial failure matrix, the desktop-must-NEVER rules D-1..D-8, the
  `provider_side_outcome_unknown` inference rule, the cwd asymmetry trap, and the
  cancellation defect mechanics.
- **Command**: operator workflow, the approval/consequence model, the pricing-fork option
  table, and the zero-spend/approval/regeneration UX requirements.
- **Step**: anti-vacuity rules, the T0–T4 gate grammar, the old-run compatibility gap, the
  candidate-seal-before-review discipline, and the one-final-T4 rule. All 22 reused existing
  selectors were verified present in the live tree.
- **GLM**: the exact tracked-path fence (16 paths, refined to 17 by the supervisor adding
  `errors.py`), the milestone decomposition, backward-compatibility ordering, the
  `core/application.py` zero-diff discipline, and the fork/fence matrix.

## Supervisor additions beyond the specialist consensus

1. `src/bots5/errors.py` added to the fence for a typed `ApprovalInvalidatedError`
   (GLM's fence had no home for it).
2. Deterministic, selection-first attempt resolution (D-3) replacing a max-scan fallback.
3. Explicit "Make current" selection instead of auto-select (D-5).
4. Five, and only five, forks escalated; approval-substrate and layout explicitly
   reclassified as mechanically settled with reasoning (D-1, D-2).
5. Documentation-status refresh deliberately excluded from the base fence (F-16; closure
   strategy §5).

## Unresolved at integration

- HSF-1, HSF-2, HSF-3, HSF-4, HSF-5 require Mick's adjudication at the design gate.
- HSF-4 blocks full T0.6/T0.8 validation until decided.
- Jamba (`ai21/jamba-large-1.7`) was not present on the provider route index; it is optional
  and nominally unscheduled, so this does not block design or implementation. Recorded as a
  limitation.
- No per-child reasoning-effort parameter exists in the harness child-launch hooks; the
  requested `provider_maximum_if_exposed` is recorded as not applied for child launches.
