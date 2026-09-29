# Implementation sequence (post-gate, inside the accepted fence)

This sequence is **not authorized** by parcel-v1. It becomes executable only after Mick
accepts the exact sealed design, grants implementation authority, and an immutable
successor parcel-v2 + retained continuation are created.

Hard ordering constraint (GLM risk 1, Step §10): **backward-compatibility fixtures land and
pass before any storage/models mutation.** Today no automated proof of old-run
compatibility exists; changing the schema first would make regressions undetectable.

## Milestone T0 — engine truth (no desktop)

0. **M0.0 — backward-compatibility fixtures (gate-first).**
   Add `tests/test_phase10_backward_compat.py` with golden V0/V0.2 fixture run directories
   (derived from the retained `evidence/**` shapes and/or synthesised to the exact v1
   writer shape) proving `load_run_view`, `load_stage_view`, `locate_run_dir` and CLI
   `status`/`inspect` behave identically. **This milestone must pass before M0.1.**
1. **M0.1 — models + storage attempt semantics.** `StageRecord` additive fields;
   `PreflightSnapshot`/`ApprovalRecord`; `FileSnapshot`; `attempt_number`;
   `persist_stage_attempt`; `selection.json` load/persist; v2-aware readers; selection and
   staleness reconstruction helpers. Legacy behavior unchanged.
2. **M0.2 — events + usage.** Additive `EVENT_TYPES`; dual-accounting `usage_document`.
3. **M0.3 — runner.** Snapshot assertion + zero-reread dispatch closure;
   `CancelledError` terminalization; synthesis dependency provenance;
   `regenerate_worker`; `rerun_synthesis`; `preflight.json` persistence.
4. **M0.4 — CLI parity.** `inspect --attempt`, status attempt/stale markers,
   `regenerate` and `rerun-synthesis` verbs with the headless consent form
   (`--approve --actor` / `--approval`, preflight-only without it).

Gates: T0.1–T0.5, T0.9 (plus T0.6/T0.8 only after the HSF-4 branch is adjudicated).

## Milestone T1 — bridge

5. **M1.0 — `core/campaign.py`.** `CampaignBridge` + immutable `CampaignProjection`;
   zero-spend validation; preflight/approval construction; hosted task ownership; bounded
   poller; explicit selection command; close/drain.

Gates: T1.1–T1.4 (storage/events/runner subsystem + bridge).

## Milestone T2 — desktop surface

6. **M2.0 — `desktop/campaign_dock.py`.** Load/validate/preflight/approve/progress/result/
   attempts/staleness/New Job presentation over the sealed projection.
7. **M2.1 — `desktop/window.py` attach.**
8. **M2.2 — `bootstrap/desktop.py` compose + close-driver campaign stage.**

Gates: T2.1–T2.4 (CLI compatibility, provider/unknown-cost semantics, Phase 9 shutdown
neighbors, historical readers).

## Milestone T3 — cross-cutting validation

9. Filesystem durability/reconstruction; asyncio shutdown; provider-side outcome
   uncertainty; desktop/core authority boundaries. Run only where evidence justifies it.

Gates: T3.1–T3.4. T4 is **not** part of this milestone.

## Milestone T4 — final exact sealed candidate

10. Freeze the candidate; materialize the implementation candidate manifest + seal
    **before** any independent implementation review.
11. Fresh independent review (Qwen3.8 broad falsification; MiniMax lifecycle where material;
    Step proof-sufficiency where material); classify; repair inside the fence; focused
    revalidate; reseal.
12. Run the **one final complete-repository T4** (serial) against the exact final seal.
13. Verify the candidate is byte-identical after T4. If any product byte changed, that T4
    is superseded — reseal and rerun the applicable final validation.
14. Optional Jamba final reviewer (max one, normally unused); fresh MiMo final
    implementation oracle against the exact seal + validation evidence.
15. Reconcile evidence; write `reports/COMMIT_BOUNDARY_REPORT.md`; STOP at the pre-commit
    boundary.

## Rules that apply throughout

- Work inside `design/MUTATION_FENCE.json` only. Any need to leave it stops for Mick.
- Every gate proves non-zero collection **before** it is claimed; never rerun a passing
  suite just to scrape a count; keep serial gates serial (no concurrent pytest processes).
- Same semantic defect class recurring ⇒ different-family diagnosis before another mutation.
- A review performed against unsealed bytes is historical evidence only.
- Repair waves are bounded (parcel budget: 8 waves; 32 total child launches).
