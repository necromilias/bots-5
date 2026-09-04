# Roadmap

## Closed V0 baseline

B.O.T.S. 5 V0 is closed. Final zero-spend closure validation passed against
`13e3ac463c44d66e57d4443027f0cc9dfe9b93a5`; see `docs/V0_CLOSURE_REPORT.md`.

The closed baseline includes:

- strict JSON manifest;
- explicit UTF-8 inputs/prompts;
- bounded parallel worker phase;
- optional synthesis;
- OpenRouter provider boundary;
- per-stage and overall timeout handling;
- durable run/stage/event/usage artifacts;
- disk-only status and inspect;
- exact-known/unknown cost semantics;
- zero-spend unit tests.

## V0.1 status

- worker-boundary hardening is implemented;
- completion telemetry is implemented;
- final live worker-boundary conformance is satisfied;
- the first genuinely useful low-stakes repository-audit campaign is recorded in
  `docs/FIRST_USEFUL_CAMPAIGN_REPORT.md`;
- the revised useful campaign completed normally and was judged substantively useful;
- its accepted findings were adjudicated and the blocking CLI, terminal-persistence, and runs-dir
  symlink defects were corrected and locally validated;
- the controlled failure-path campaign is preserved under `examples/opv1-controlled-failure/` and
  exercised terminal provider-response failure, dependency blocking, skipped-stage zero-cost
  accounting, partial aggregate cost, and durable inspection without exposing a blocking defect;
- the exact `dependency_incomplete` semantics are additionally covered by deterministic tests and
  prior real truncation evidence;
- `docs/OPERATING_PROCEDURE_V1.md` is the frozen normal operating procedure;
- the OPv1 freeze decision and evidence are recorded in `docs/OPV1_FREEZE_REPORT.md`.

## V0.2 — local OpenAI-compatible provider

Implemented, deterministically validated, and live-proven against a local OpenAI-compatible endpoint.
The reviewed implementation candidate was fast-forwarded onto `main`; the reviewed implementation
landing tree is `96673dd0bfc28a89a74ee0745bdab05eb1163da1`, and V0.2 is now the landed repository state.

The V0.2 objective is a narrow generic local OpenAI-compatible provider that coexists with OpenRouter
while preserving the closed V0 execution model, existing `CompletionRequest -> CompletionResult`
seam, non-streaming operation, completion semantics, persistence, timeouts, cost accounting,
synthesis gating, and failure behavior.

The V0.2 design swarm produced six normally completed specialist worker outputs. Synthesis recovery
then exposed and repaired an OpenRouter normalization defect for reasoning-only incomplete responses
where `message.content` is null with a trustworthy non-stop finish reason. That repair is implemented
at `2fbb2a591c95f84247888a050a8af4086acaac29`, covered by deterministic tests, and live-proven by a
24-token reasoning-exhaustion canary.

A final Kimi K3 synthesis completed normally, after which the accepted implementation specification
corrected its response-normalization, validated-provider-config, and runner-API contradictions. The
implementation adds schema v2, explicit per-stage provider mapping, one built-in non-streaming local
OpenAI-compatible provider, and zero-network deterministic coverage. Schema v1 and the frozen OPv1
document remain unchanged.

See `docs/V0_2_DESIGN_CAMPAIGN_REPORT.md` for the campaign evidence and implementation closure.

## Linux v0.1 — native desktop design accepted

Linux v0.1 product, architecture, implementation-technology, and construction-sequence decisions have
been human-adjudicated and accepted. See `docs/LINUX_V0_1_DESIGN.md` for the build-facing contract and
`docs/LINUX_V0_1_PHASE1_PHASE2_CLOSURE_REPORT.md` for the current implementation and validation record.

### Implemented candidate status

Phase 1 and Phase 2 are closed. Phase 3 is implemented in the current unstaged, uncommitted candidate
and has passed deterministic streaming, migration, persistence/restart, cancellation, offscreen
Qt/qasync, and legacy campaign regression validation. Fresh Sol/xhigh review found and drove bounded
repairs for persistence-authority edge cases; fresh Round 21 returned `PASS` with no concrete closure
blockers, and human closure is approved.
A separate operator live acceptance also
completed a real local Qwen generation, exercised explicit mid-stream cancellation to `ABORTED`,
preserved partial output across restart, and used no paid or OpenRouter request. The initial wrong
endpoint failures were expected operator/configuration failures and were preserved as failed attempts.

This live evidence is recorded separately from automated validation and independent adversarial review;
the Phase 3-specific deterministic matrix was `244 passed, 1 skipped`. The current combined candidate
matrix, including the bounded Draft 1 UI shell, is `254 passed, 1 skipped`. The retained Phase 2
recovery-artifact interaction remains a separate human-adjudication boundary.

The bounded Draft 1 native UI shell is implemented and operator-accepted as **good enough for now** after
fresh independent Sol/xhigh `PASS`. Its implementation and acceptance record is
`docs/LINUX_V0_1_UI_DRAFT_1_IMPLEMENTATION_ACCEPTANCE_REPORT.md`. The provisional
`docs/LINUX_V0_1_UI_UX_DRAFT_1.md` remains provisional/tentative first-draft authority and has not been
promoted into final design authority.

The candidate is prepared for human commit approval but is not staged, committed, pushed, or otherwise
landed. Phase 4 remains the next future phase and has not been implemented.

The accepted target is a real native Linux desktop application centred on persistent multi-chat with
lossless lineage, concurrent streaming generation, deterministic context construction, reusable
attachments, search, provenance/inspection, backup/recovery, and a thin operational campaign surface.

The implementation is intentionally sequenced as validated vertical slices: walking skeleton,
conversation truth, real generation, concurrency/workspace, provider/model usability,
context/attachments, search, inspection, recovery/import-export, campaign integration, product
finishing/packaging, and a final torture run.

The current candidate claim is limited to the Phase 1 through Phase 3 scope recorded in the closure and
implementation reports. The remaining listed desktop capabilities are target design, not implemented.
Later implementation edits, provider/API calls, commits, and landing remain separately approved
consequences.

Future Code/Git, Android, persistent daemon/remote clients, MCP/tool frameworks, scheduling, RAG,
mode-governed capability envelopes, and other Future Fuckery remain deferred unless a concrete Linux
v0.1 blocker separately promotes a bounded requirement.

## Later candidates

- campaign stage-output reuse/resume after partial campaign failure, now supported by observed
  operational pain but explicitly outside V0.2;
- planner-generated manifests;
- richer but still explicit DAG semantics;
- deliberate retry policy where idempotence is understood;
- stronger filesystem/tool capability model.

## Deferred defect

- extremely large JSON integers can escape `_number()` through `float()` overflow instead of a clean
  validation error; low operational consequence and explicitly carried forward beyond V0 closure.

## Speculative

- OMC authority packaging;
- gated patch application;
- distributed execution.

None of the speculative items exist in V0.
