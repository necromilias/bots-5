# GLM feasibility + exact mutation fence — Phase 10 campaign desktop integration

Model: z-ai/glm-5.3-flash · Baseline HEAD `0756904481ae884bb9e864e8e1e11fc4a27a72ff` (verified via `git rev-parse`, tracked tree clean) · Design-only, read-only, no provider calls, no dependency changes.

Companion machine-readable fence: `GLM_PROPOSED_FENCE.json` (16 paths: 8 modify, 8 add; plus `unchanged_guards`).

## 1. Exact tracked path set

### Modify (7 code paths)

| Path | Why in fence | Why adjacent paths stay out |
|---|---|---|
| `src/bots5/models.py` | Additive `StageRecord` fields (`attempt_number`, `consumed_dependencies`, `dependency_digests`) with defaults; new frozen `PreflightSnapshot`/`ApprovalRecord`. Defaults keep v1 JSON loadable. | Domain models outside StageRecord untouched; no `domain/` change needed. |
| `src/bots5/storage.py` | Attempt-aware persist, `selection.json` load/persist, v2-aware readers. Legacy `load_run_view`/`load_stage_view` v1 behavior unchanged. Flat `<id>.att<N>.{json,md}` basenames keep the containment predicate (`storage.py:196-198`: `candidate.parent == stages/`) **intact** — verified live. | `paths.py` unchanged: `STAGE_ID_RE` already admits these basenames; `locate_run_dir` untouched. |
| `src/bots5/runner.py` | Preflight digest assertion immediately before first provider request (TOCTOU at `runner.py:265-267`); `CancelledError` terminalization (`:465` catches `Exception` only, verified); dependency provenance; `regenerate_worker`/`rerun_synthesis`. | `manifest.py` unchanged: zero-spend validation reuses `load_job` + `validate_referenced_files` verbatim (COMMAND §1.1); digest computed by runner/bridge over the same bytes, so no manifest schema change ⇒ no `JOB_SPEC.md` obligation (DEVELOPMENT.md contract). |
| `src/bots5/events.py` | Additive `EVENT_TYPES` extension (`attempt_selected`, `synthesis_stale`, `run_cancelled`); existing 11 kinds untouched. Versioned evidence decision per CONTRACT:88-92. | No reader shim exists (nothing reads `events.jsonl` programmatically — Qwen §6); no consumer to migrate. |
| `src/bots5/usage.py` | Dual-accounting `usage_document` (cumulative_spend + selected_spend). No fabricated token-dollar progress. | `usage.aggregate_cost` consumers (synthesis gate) keep their selected-pipeline semantics. |
| `src/bots5/cli.py` | Headless parity: `--attempt` on `inspect`, attempt/stale markers in `status`, `rerun-synthesis` verb. v1 run output shape + exit codes unchanged (T0.9/T2.1). | `cli.py` `validate`/`run` verbs unchanged; no new `--runs-dir` surface. |
| `src/bots5/desktop/window.py` | Narrow wiring: attach the campaign dock at the existing `MainWindow` composition seam. | `session.py`, `phase9*.py`, `bridge.py`, `widgets.py`, `theme.py`, `profile.py` untouched — Phase 9 surfaces keep their sealed behavior. |
| `src/bots5/bootstrap/desktop.py` | Narrow wiring: compose the campaign bridge in `build_runtime`/`serve`. Authority acquisition order and `_close_driver` staging precedence untouched (verified: close ordering `store→authority→application→workspace→windows`). | `core/application.py` stays **zero-diff**: it is 2775 lines of Phase 1–9 authority machinery and today has *no edge into the campaign graph* (Qwen §14); routing the bridge through `BotsApplication` would cross that authority boundary. |

### Add (9 paths)

- `src/bots5/core/campaign.py` — the one bridge: bounded filesystem polling projection, preflight/approval orchestration, background task ownership. The desktop observes the engine through this seam and never becomes a second engine (MiniMax §6: polling is the sanctioned pattern; no campaign event bus, no SQLite shadow state).
- `src/bots5/desktop/campaign_dock.py` — dock widget: load, zero-spend validation view, preflight/approval modal, truthful progress, attempt switcher, expandable output viewer.
- `tests/test_phase10_backward_compat.py` — **must land and pass before any storage/models mutation** (STEP §10).
- `tests/test_phase10_desktop_preflight.py`, `test_phase10_desktop_projection.py`, `test_phase10_desktop_lifecycle.py`, `test_phase10_evidence_regeneration.py`, `tests/test_phase10_cross_cutting.py` — the STEP §8 required new surfaces, mapped to T0/T3 gates.

Guard set (existing tests that must keep passing, wired into per-path `tests`): `test_storage_events.py`, `test_runner.py`, `test_cli_views.py`, `test_manifest.py`, `test_phase1_desktop.py`, `test_phase7_desktop.py`, `test_phase9_desktop_slice_e.py`, `test_desktop_draft1.py`.

## 2. Milestone sequence (bounded, T0–T3)

- **T0 — engine truth, no desktop.** Order: (1) backward-compat golden fixtures land and pass; (2) `models.py`/`storage.py` attempt semantics + selection; (3) `events.py`/`usage.py`; (4) `runner.py` preflight assertion + cancel terminalization + regeneration/rerun; (5) `cli.py` parity. Gates: STEP T0.1–T0.5, T0.9 (zero-spend validation, preflight binding, fake-provider spend gate, attempt preservation, stale synthesis, headless preservation). T0.6/T0.8 are blocked until the cancellation fork is adjudicated (§5, fork 4).
- **T1 — subsystems.** `core/campaign.py` bridge + storage/events/runner subsystem gates (T1.1–T1.4). Collection counts proven non-zero before every gate.
- **T2 — adjacent surfaces.** `desktop/campaign_dock.py` + `window.py` attach + `bootstrap/desktop.py` compose; CLI compatibility, Phase 9 shutdown neighbors, historical readers (T2.1–T2.4).
- **T3 — cross-cutting.** Filesystem durability/reconstruction, asyncio shutdown, provider-side outcome uncertainty, desktop/core authority boundaries (T3.1–T3.4). T4 is reserved for the final sealed candidate only (out of this design fence).

## 3. Backward-compatibility requirements for historical run directories

1. V0/V0.2 run directories remain loadable with byte-identical reader semantics: `load_run_view`, `load_stage_view`, `locate_run_dir`, CLI `status`/`inspect` output shape and exit codes (T0.9, T2.4).
2. No historical stage output is ever rewritten in place; v1 evidence is treated as `evidence_version` absent = v1. New fields are optional-with-defaults; `selection.json`/`preflight.json` are additive files that v1 readers never consult.
3. `result.md` stays the mirrored output of the currently selected synthesis attempt; for v1 runs it is simply the only attempt.
4. Golden-fixture tests over retained historical runs land **before** storage schema mutation (STEP §10 — today no automated old-run proof exists).
5. New event kinds must not break any future reader; no in-place `events.jsonl` rewrite.

## 4. Migration / dependency / provider-contract expansion that would cross authority

- **Pricing registry of any kind** (operator-entered, static catalogue, provider lookup): a new pricing authority → STOP FOR MICK (H-4). Provider lookup additionally violates the zero-network preflight guarantee.
- **AppStateStore migration head** (only if fork 5 selects app-DB restart persistence): crosses Phase 9 store-migration authority (`0012_phase9_archive_import` head lineage).
- **Dependency changes**: none required (PySide6/qasync already present); any dependency change needs a separately approved blocker (CONTRACT exclusions).
- **Manifest schema change**: deliberately avoided — it would trigger the `JOB_SPEC.md` + validation-test contract in `DEVELOPMENT.md:110-113`.
- **Provider contract expansion** (streaming, new capability): excluded (CONTRACT:100-102); provider route change on regeneration is fork 2.
- **Containment-predicate change** (`storage.py:196-198`): security-relevant; only under fork 3's nested-layout outcome.

## 5. Fork classification and fence under either outcome

| Fork | Class | Fence if option 1 | Fence if option 2 (or fallback) |
|---|---|---|---|
| Preflight pricing authority | **HUMAN_FORK** (all specialists: Qwen [X]2, GEMINI §5.2, MiniMax H-4, Step §9.1, COMMAND §2.3) | Operator-entered rates: `models.py` (rate fields on ApprovalRecord), `campaign_dock.py` modal, persisted via `storage.py`/`runner.py` preflight.json — no new module, no network. | Unknown-cost + approval: same minus rate fields. Static catalogue would **add** `src/bots5/resources/pricing.json` (fence expansion); live provider lookup crosses authority — excluded absent Mick. |
| Provider change on regeneration | **HUMAN_FORK** (Step §9.3, GEMINI §5.3) | Route immutable: `runner.py` validation only; no provider field in UI. | Explicit provider switch (target configured in `job.providers`): `runner.py` + `campaign_dock.py` selector. Either way `providers/**` unchanged. |
| Attempt path layout + storage containment | **HUMAN_FORK** (MiniMax H-2 + blocked condition C-3) | Flat `<id>.att<N>.{json,md}`: the only candidate that keeps the containment predicate unchanged — `storage.py`/`models.py` as fenced. | Nested `stages/<id>/<att>/`: must modify `load_stage_view`'s security predicate in `storage.py` (+ possible `paths.py` validation) — a consciously larger, security-relevant fence. |
| Cancellation terminal state | **HUMAN_FORK** (Qwen [X]1, MiniMax H-1, Step §9.6) | Path A engine-side: `runner.py` traps `CancelledError` → terminal persist + `run_cancelled` event (`events.py`). | Path B desktop-owned: `core/campaign.py` drives `run_job` to terminal persist before close allows; `runner.py` trap still in fence for crash-parity. Untested T0.6/T0.8 until adjudicated. |
| Restart persistence of loaded campaign | **HUMAN_FORK** (MiniMax H-5, Step §9.4) | Filesystem-only: bridge reloads from run-dir evidence on restart; zero extra paths. | App-DB persistence: AppStateStore migration head + desktop persistence — **fence expansion crossing Phase 9 migration authority**. |
| Run discovery | **HUMAN_FORK** (Qwen [X]5, MiniMax H-3, Step §9.5) | Explicit path/id only: desktop passes operator input to existing `locate_run_dir`; no new seam. | Browse runs dir: new enumeration seam (`paths.py` and/or `core/campaign.py`) — conditional expansion beyond the base fence. |
| Approval binding substrate | **HUMAN_FORK** (Qwen [X]3, MiniMax H-6, Step §9.2) | Engine-side immutable snapshot: `models.py` `PreflightSnapshot` + `runner.py` assert before first spend + `storage.py` preflight.json. | Desktop-side digest + reverification at execution: digest in `core/campaign.py`; `runner.py` assertion remains in the fence either way — a pre-first-provider-request check must live engine-side to be binding. |

No fork is resolved by preference here; all seven remain open for the supervisor/Mick, matching every specialist register.

## 6. Risks and ordering constraints

1. **Storage mutation before fixtures.** Attempt semantics without `tests/test_phase10_backward_compat.py` first makes old-run regression undetectable. Hard ordering: fixtures → storage/models → runner.
2. **Cancellation fork blocks T0.6/T0.8.** Unadjudicated, shutdown-with-active-campaign cannot be validated (STEP §11); engine change without adjudication would cross the fork.
3. **Second-engine / evidence pollution.** Bridge must poll the filesystem on a bounded cadence; no campaign event bus, no desktop SQLite shadow truth; use absolute paths (cwd asymmetry trap, MiniMax §6.3/§7).
4. **TOCTOU closure must be engine-side and immediate.** Today approval would bind to paths only (`runner.py:265-267` re-reads bytes); the digest assert must run immediately before the first provider request, and regeneration/rerun always re-preflight and re-approve.
5. **`core/application.py` zero-diff discipline.** Any incidental edit risks Phase 1–9 authority invariants in a 2775-line module; composition happens only in `bootstrap/desktop.py`.
6. **Cost truth.** Cumulative vs selected accounting must neither double-count nor hide sibling-attempt spend (GEMINI Risk 3); no token-dollar projection without an adjudicated pricing authority.
7. **Containment predicate.** A nested layout silently fails `load_stage_view` at runtime (MiniMax C-3) — fork 3 must be adjudicated before `storage.py` implementation.
8. **Closed event vocabulary.** The additive `EVENT_TYPES` extension is a versioned evidence decision with no existing reader shim to copy; test it additively against v1 event logs.
