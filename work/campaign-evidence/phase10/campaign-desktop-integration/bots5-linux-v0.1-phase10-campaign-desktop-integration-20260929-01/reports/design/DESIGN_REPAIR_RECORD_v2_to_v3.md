# Design repair record — seal v2 → v3

Trigger: fresh MiMo final design oracle against design seal v2
(`2a57410dc1da155aab7fcbf480a244b999eac3525a9c7737e40adc703fdee0e4`).
Report: `reports/oracle/MIMO_FINAL_DESIGN_ORACLE.md`
(sha256 `8dfab3b48aa0aff430b231b23d065ff7bc0220518a2954d5fb869b4d3a8cb21c`).

Oracle result: seal integrity PASS (21/21 entries, v1 preserved, supersession declared);
verdict **REPAIRABLE_DESIGN_DEFECT** with four blocking findings (M-1, M-2, M-3, M-6) and
three smaller ones (M-4, M-5, cosmetic). The oracle confirmed F-01…F-08 were genuinely
repaired and that the fence (9 modify / 8 add) remains sufficient. This record maps each
new finding to its repair. **No repair required leaving the accepted design fence.**

The oracle and the earlier falsification are preserved unchanged as evidence. The v1→v2
repair record is likewise preserved unchanged (it is cited by line in both reviews); where
it contained one false source claim (M-3), that claim is corrected below and in the design
text rather than by rewriting the sealed historical ledger.

---

## M-1 — Main design still framed pricing branch D as acceptable

**Repair.** `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md` §6 HSF-1 row now states branch A is the
only branch compliant by default and that D requires an **explicit Mick waiver of the OPv1
§4 dollar bound**, matching `HUMAN_SEMANTIC_FORK_REGISTER.md` and
`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §5. A revision-history note was added to the main
design.
**Status:** repaired; documentation only.

## M-2 — Initial full-run synthesis provenance was unspecified

**Repair.** `REGENERATION_AND_STALE_SYNTHESIS.md` §3.1 now specifies that, for a full run,
`consumed_dependencies` (default selection = attempt 1) and `dependency_digests`
(sha256 of the exact bytes rendered into the synthesis message) are recorded **at synthesis
dispatch inside `run_job`** (`runner.py:404-415`), before `request_sent`. Approval binding
for `full_run` remains empty for dependencies because the bytes are produced by the same run
under the same approval. Consequence: an initial-run synthesis is FRESH at creation and
turns STALE on regeneration + reselection, so obligation 10 fires for first-generation runs.
Added the T0.5 selector
`test_initial_run_synthesis_is_fresh_and_becomes_stale_after_regeneration_and_reselection`.
**Status:** repaired; inside fence (`runner.py`, `models.py`, `storage.py`).

## M-3 — F-03's `api_key_env` assertion cited a property absent on `OpenRouterProvider`

**Repair.** `PREFLIGHT_APPROVAL_STATE_MACHINE.md` §4.7 restates assertion 7 kind-specifically:
always assert provider key set + `base_url` + class/module kind; assert `api_key_env`
equality **only where the instance exposes it** (`OpenAICompatibleProvider`,
`openai_compatible.py:81-83`); for providers without such a property (openrouter,
`openrouter.py:61-62`), bind the key source **by construction** from the frozen route and
document that independent re-derivation would require a forbidden provider-contract change.
Corrected the false "existing public properties" claim from the v1→v2 ledger; fixed the
`preflight.json` route example to a `kind`/`api_key_env_name`/`key_source` block. Added a
provider-object swap selector.
**Status:** repaired; inside fence (`runner.py`, `errors.py`); `providers/**` still zero-diff.

## M-4 — `cancelled_pending` could persist via the generic exception path

**Repair.** `DESKTOP_SURFACE_AND_LIFECYCLE.md` §5.2 now extends the generic
`except Exception` / `_best_effort_internal_failure` path (`runner.py:465-480`,
`runner.py:56-98`) to reclassify any `cancelled_pending` record to `internal_error`
(preserving `provider_side_outcome_unknown`), so `cancelled_pending` is never a permitted
durable outcome. Added the T0.6 selector
`test_cancelled_pending_is_never_a_permitted_durable_outcome`.
**Status:** repaired; inside fence (`runner.py`).

## M-5 — Headless regeneration claim vs fence/sequence; headless approval path unspecified

**Repair.** `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md` row 13, `CAMPAIGN_EVIDENCE_EVOLUTION.md`
§6.4, `MUTATION_FENCE.json` (`cli.py`), and `IMPLEMENTATION_SEQUENCE.md` M0.4 now agree:
CLI exposes `inspect --attempt`, status attempt/stale markers, and **both**
`regenerate` and `rerun-synthesis` verbs. Headless consent form specified: these verbs are
**preflight-only** unless `--approve --actor <label>` or `--approval <path>` is supplied;
the engine entry points remain directly usable as library calls. Added T0.11 selectors.
**Status:** repaired; inside fence (`cli.py` already fenced).

## M-6 — Regeneration eligibility of v1 runs was unspecified

**Repair.** `REGENERATION_AND_STALE_SYNTHESIS.md` §2 and §5 now carry precondition 0: the
target run must declare `evidence_version >= 2`; a v1 run is read-only and
regeneration/rerun is refused with a typed reason, writing nothing (avoiding the mixed
layout the design elsewhere forbids). Added T0.4/T0.5 v1-refusal selectors.
**Status:** repaired; inside fence (`runner.py`, `storage.py`).

## Cosmetic repairs

- `STAGE_ID_RE` → `SAFE_ID_RE` (`src/bots5/paths.py:10`) in
  `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md` §2.2 and `SPECIALIST_DISAGREEMENTS.md` D-3.
- `preflight.json` schema example disambiguated (`doc_schema_version` vs
  `job_schema_version`).

---

## Repair outcome

- Findings repaired this wave: **M-1…M-6 + 2 cosmetic = 8/8**.
- Fence expansion required: **none** (9 modify / 8 add unchanged).
- Design seal v2 is preserved unchanged; a successor seal v3 covers the repaired design.
- Human forks HSF-1…HSF-5 remain open for Mick, with HSF-1 correctly framed.
- Next required step: fresh MiMo final design oracle against seal v3 (MiMo budget: 1 of 3
  launches used in design).
