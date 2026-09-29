# Design repair record — seal v1 → v2

Trigger: fresh independent design falsification by GPT-6 Luna against design seal v1
(`7ecbb502bd65dcc2ad1595b1dd709db3eff0bc5d5e079b00f8e77c29ee6d952f`).
Report: `reports/review/GPT6_LUNA_DESIGN_FALSIFICATION.md`
(sha256 `16524cfa1ad4bbdbfaa051b10908591b5b76c016a3dd6031f0d5567d3817b8fe`).

Seal integrity was independently recomputed as PASS (19/19 entries). The falsifier returned
5 PASS attack-surface dispositions, 8 repairable design defects (F-01…F-08) and 1 human
semantic fork (F-09). This record maps every finding to its repair. **No repair required
leaving the accepted design fence.**

Falsification is preserved as evidence; nothing in the report was rewritten.

---

## F-01 — Rerun approval did not bind dependency bytes / run context

**Repair.** Introduced an explicit `OperationSnapshot` for every operation
(`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §2.2) binding `target_run_id`, operation, stage,
attempt number, requested model, provider route, the exact frozen system/user message, and —
for synthesis rerun — each selected dependency attempt number plus the SHA-256 of its output
bytes and the exact rendered synthesis user message. Dispatch uses the frozen message; the
engine re-verifies selection and bytes immediately before dispatch and refuses with
`ApprovalInvalidatedError` on any difference (`REGENERATION_AND_STALE_SYNTHESIS.md` §3, §5;
`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §4.6).
**Status:** repaired; inside fence (`models.py`, `runner.py`).

## F-02 — Approval was not one-shot

**Repair.** `ApprovalRecord` gains `approval_id` and a bound `target`
(`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §3). Consent is consumed engine-side exactly once by
exclusive-creating `<run>/approvals/<approval_id>.json` before the first `request_sent`; an
existing marker raises `ApprovalInvalidatedError` (`§3.1`). Independent durable guards:
`create_run_tree` refuses an existing run id, `persist_stage_attempt` refuses to overwrite an
existing attempt, and the approval binds the target run/attempt. Applies to UI and non-UI
callers and survives restart.
**Status:** repaired; inside fence (`models.py`, `runner.py`, `storage.py`).

## F-03 — Approved provider route was not tied to the dispatching provider object

**Repair.** `PreflightSnapshot.provider_routes` freezes the route (`kind`, `base_url`,
`api_key_env_name`, key source). Before dispatch the engine asserts the concrete provider
instance's `base_url` (and `api_key_env`, and class/module kind) equals the frozen route and
that the provider key set matches, refusing otherwise
(`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §4.7). Read-only use of existing public properties;
`src/bots5/providers/**` remains zero-diff.
**Status:** repaired; inside fence (`runner.py`).

## F-04 — Provider errors could be terminalized as certain failure

**Repair.** Added a post-dispatch classification rule (D-9 in
`DESKTOP_SURFACE_AND_LIFECYCLE.md`). Any failure after `request_sent` is
`provider_side_outcome_unknown=True` unless it is a definitive non-acceptance (HTTP 4xx
except 408/429, pre-network credential/config rejection, or explicitly marked
`ProviderError.definitive_rejection`). Transport errors, 5xx/408/429, timeouts and
unclassified errors are ambiguous → unknown, never auto-retried. `ProviderResponseError`
(received-but-unusable) is distinct. `ProviderError.definitive_rejection` (default `False`)
is added in `errors.py`; existing adapters raising plain `ProviderError` stay conservatively
ambiguous, so no adapter change is needed.
**Status:** repaired; inside fence (`errors.py`, `runner.py`).

## F-05 — Operator cancellation mislabelled as a run timeout at stage level

**Repair.** `_execute_stage`'s `CancelledError` handler now writes a neutral marker
(`error_type="cancelled_pending"`); the outer terminalization reclassifies it — `run_job`'s
`TimeoutError` branch to `run_timed_out`, the new `CancelledError` branch to the HSF-4
cancellation type — preserving `provider_side_outcome_unknown`. Genuine timeouts keep their
label (`DESKTOP_SURFACE_AND_LIFECYCLE.md` §5.2).
**Status:** repaired; inside fence (`runner.py`).

## F-06 — v2 synthesis with missing provenance could read as "current"

**Repair.** The predicate now yields three outcomes (FRESH / STALE / UNVERIFIABLE) plus
LEGACY_UNVERIFIED for v1. Absent or malformed provenance on a **v2** attempt is
UNVERIFIABLE with an integrity warning, never "current"; missing bytes or digest mismatch is
STALE with an integrity warning (`REGENERATION_AND_STALE_SYNTHESIS.md` §4.2–4.3;
`DESKTOP_SURFACE_AND_LIFECYCLE.md` §4.1).
**Status:** repaired; inside fence (`storage.py`, `usage.py` readers).

## F-07 — Selection change did not update selected cost

**Repair.** `selected_spend`/`aggregate` are now **derived at read time** from
`per_attempt` + `selection.json`; the `usage.json` copy is a cache. The pre-synthesis cost
gate uses the derived value. Write ordering (selection first, then cache refresh) and
crash reconstruction are specified; `cumulative_spend` is unaffected
(`CAMPAIGN_EVIDENCE_EVOLUTION.md` §4).
**Status:** repaired; inside fence (`usage.py`, `storage.py`).

## F-08 — Validation gates permitted forbidden behavior / under-covered obligations

**Repair.** `VALIDATION_PLAN.md` T0 was rewritten: the desktop-cancellation selector now
requires a **terminal** durable record (the crash case keeps its own separate test); T0.5
adds bidirectional reselection, v1-missing vs v2-missing provenance, missing bytes and
digest mismatch; T0.2 adds prompt/contract bytes, provider route, dependency selection and
output target invalidators; T0.3 adds consumed-approval replay; T0.7 adds derived-selected-cost
and provider-uncertainty classification.
**Status:** repaired; inside fence (test files already in fence).

## F-09 — Pricing branch D was presented as an OPv1-compliant fallback

**Repair.** `HUMAN_SEMANTIC_FORK_REGISTER.md` HSF-1 and
`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §5 now state that branch A (operator-entered
**currently advertised** rates with recorded source/time/route and a fixed conservative
bound basis) is the only branch compliant by default; branch D **does not satisfy** OPv1 §4
and is available only under an explicit Mick waiver/supersession. Paid approval UI is not
implemented until Mick decides.
**Status:** repaired as documentation; the fork itself remains **open for Mick**.

---

## Repair outcome

- Repairable defects repaired: **8/8**.
- Human fork: **HSF-1 remains open** (correctly, as a Mick decision), now framed without
  misrepresenting D as compliant.
- Fence expansion required: **none**.
- Design seal v1 is preserved unchanged; a successor seal v2 covers the repaired design.
- Next required step: fresh MiMo final design oracle against seal v2.
