# Fresh MiMo final Phase 10 design oracle (seal v2)

## 0. Recomputed seal identity and seal-integrity statement

- Recomputed `sha256(seals/design/DESIGN_SEAL_v2.json)` =
  **`2a57410dc1da155aab7fcbf480a244b999eac3525a9c7737e40adc703fdee0e4`** — matches the
  expected identity exactly.
- The adjacent `DESIGN_SEAL_v2.sha256` records the same value; file size 4369 bytes.
- Recomputed `sha256(seals/design/DESIGN_SEAL_v1.json)` =
  **`7ecbb502bd65dcc2ad1595b1dd709db3eff0bc5d5e079b00f8e77c29ee6d952f`** — matches the
  declared superseded seal id; v1 is preserved unchanged (3787 bytes).
- Seal v2 declares supersession correctly (`seal_version: 2`,
  `supersedes.seal = DESIGN_SEAL_v1.json`, `supersedes.seal_id = 7ecbb502…952f`,
  `DESIGN_SEAL_v2.json:120-125`) and names `reports/design/DESIGN_REPAIR_RECORD_v1_to_v2.md`
  as its repair record (`:112`).
- Every seal-listed entry was independently rehashed with byte counts: **21/21 match,
  0 missing, 0 mismatched** (12 `design_artifacts`, 8 `referenced_inputs` including the
  repair record, 1 `review_input` = the GPT-6 Luna falsification).
- Baseline identity: live `git rev-parse HEAD` =
  `0756904481ae884bb9e864e8e1e11fc4a27a72ff`, live tree =
  `7fe879035a01a87339dfe6319a435fa0e84c94bd`, both equal to the seal pins
  (`DESIGN_SEAL_v2.json:2-3`) and to `parcel/BASELINE.json`. `git status` shows only
  untracked workspace material (`work/`, audit temps); the tracked tree is clean. Seal
  `orgmem_ref` `cc0c3c80…` matches `BASELINE.json` (internal consistency only; the OrgMem
  repository itself was not fetched — evidence limitation).

**Seal integrity: PASS.** No blocking seal/parcel/baseline identity finding. The
falsification report and the repair ledger are real, sealed, and byte-verified.

## Overall verdict

**REPAIRABLE_DESIGN_DEFECT.**

Seal integrity passes; the eight falsification defects F-01…F-08 are genuinely repaired in
substance (details in §4); no forbidden scope, fence expansion, dependency change, or
second truth was introduced. However, the sealed v2 design still contains one unrepaired
instance of the exact F-09 defect class (M-1), one mechanism gap in the central staleness
obligation that the F-06 repair exposed (M-2), one false source claim in the F-03 repair
that is unimplementable as literally specified against the zero-diff provider guard (M-3),
and three smaller specification gaps (M-4, M-5, M-6). All six are mechanically repairable
inside the existing 9-modify/8-add fence and require a v3 reseal. Separately, HSF-1
remains a genuine human fork Mick must adjudicate before any paid-approval UI.

This is a design-evidence finding only. No product code was changed, no test was executed
beyond read-only `--collect-only` probes, no provider/API call was made.

---

## 1. Review identity, method, evidence boundary

- Model: `xiaomi/mimo-v2.6-flash`. Requested effort `provider_maximum_if_exposed`; the
  harness exposes no per-child effort parameter, so **effective effort: not exposed** (no
  silent substitution).
- Read-only exact-seal review: seal chain, all 12 sealed design artifacts, the
  falsification, the repair record, parcel authority (`CONTRACT.md`, `SCOPE.json`,
  `BASELINE.json`, `FINDINGS.md`, `VALIDATION.md`, `SOURCES.md`), `MODEL_ROUTING_EVIDENCE.md`.
- Live pinned source verified byte-level: `runner.py`, `storage.py`, `models.py`,
  `events.py`, `usage.py`, `cli.py`, `paths.py`, `manifest.py`, `errors.py`,
  `rendering.py`, `providers/openrouter.py`, `providers/openai_compatible.py`,
  `providers/base.py`, `desktop/window.py`, `bootstrap/desktop.py`, plus doc citations in
  `docs/OPERATING_PROCEDURE_V1.md` and `docs/DEVELOPMENT.md`.
- Zero-provider probes (repository `.venv`, socket-blocked by `tests/conftest.py`):
  `pytest --collect-only` on the plan's reused selectors and baseline selection.
- Not done: no test execution, no network, no mutation of repository/parcel/design/seal;
  the only file written is this report.

## 2. Findings (each: location, obligation, severity, repairability, blocking)

### M-1 — Main design still presents pricing branch D as acceptable without the OPv1 waiver (F-09 defect class not fully repaired)

- **Location:** `design/PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:152` (§6 fork table, HSF-1 row,
  supervisor-recommendation column): "Operator-entered rates (A); unknown-cost+approval
  (D) acceptable". No revision note exists anywhere in that document.
- **Contradicts:** `design/HUMAN_SEMANTIC_FORK_REGISTER.md:36-40` ("D is **not** an
  equivalent compliant branch… only under an explicit Mick waiver/supersession") and
  `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md:197-201` ("**D does not satisfy** the OPv1
  dollar-bound requirement"). Both were repaired; the main design's fork table — the
  table Mick reads first — was not. The falsification explicitly required that HSF-1's
  options "must not present D as compliant without the explicit waiver"
  (`GPT6_LUNA_DESIGN_FALSIFICATION.md:125`); the sealed v2 main design still does.
- **Obligation:** CONTRACT §"Cost truth" (`CONTRACT.md:62-74`); FINDINGS F-06/F-13;
  SCOPE `human_stop_conditions[4]`.
- **Severity:** Medium (documentation framing, in the primary artifact) — the same defect
  class that blocked v1.
- **Mechanically repairable:** Yes — rewrite the cell to match the register (A
  recommended; D available only under an explicit recorded OPv1 §4 waiver) and add the
  standard revision note; then reseal.
- **Blocks Mick's design acceptance:** **Yes** — acceptance is acceptance of exact sealed
  bytes, and this cell misframes the open fork in the headline artifact.

### M-2 — No specified mechanism records synthesis provenance for the initial (full) run; under the repaired three-outcome rule every first-generation v2 synthesis reads UNVERIFIABLE

- **Location:** `design/REGENERATION_AND_STALE_SYNTHESIS.md:81-99` (§3) states
  "**Every** synthesis attempt permanently records what it consumed" but specifies the
  only concrete mechanism as "Recorded **at preflight time** from the then-current
  `selection.json`, bound into the `OperationSnapshot`/approval". For a full run that
  mechanism is inapplicable: `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md:88,96`
  explicitly makes `dependency_attempts`/`dependency_digests` **empty for full_run**, and
  at full-run preflight (a) no `selection.json` exists yet and (b) worker output bytes do
  not exist yet, so the digests cannot be computed (`CAMPAIGN_EVIDENCE_EVOLUTION.md:58-60`
  confirms `selection.json` is normally absent on an initial run). No alternative
  recording point (e.g., at synthesis dispatch inside `run_job`, which has the bytes —
  `runner.py:404-415`) is specified anywhere in the twelve sealed artifacts.
- **Consequence:** with the F-06 repair (`REGENERATION_AND_STALE_SYNTHESIS.md:132-148`),
  a v2 synthesis attempt with absent provenance is **UNVERIFIABLE — never FRESH, never
  STALE**. Implemented per the only stated mechanism, every initial run's synthesis would
  carry a permanent integrity warning, and after a worker regeneration + reselection the
  first-generation synthesis would still read UNVERIFIABLE instead of mechanically STALE.
  That defeats CONTRACT obligation 10 ("a changed selected worker attempt marks dependent
  synthesis stale", `CONTRACT.md:33`) for first-generation runs. This gap was masked in
  v1 (absent provenance was displayed as "current"); F-06's honest three-outcome repair
  exposed it — a **new hole opened by the repairs**.
- **Obligation:** CONTRACT item 10 and item 11; FINDINGS F-14; main design §1 rows 10-11
  (`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:55-56`).
- **Severity:** High.
- **Mechanically repairable:** Yes, inside the fence — specify that the full-run path
  persists `consumed_dependencies = {dep: 1}` (default selection) and
  `dependency_digests = sha256(exact bytes rendered into the frozen synthesis message)`
  on the synthesis attempt record at synthesis dispatch/completion in `run_job`
  (`runner.py`), keep approval binding as-is (same-run outputs need no pre-dispatch byte
  binding), and add a T0.5 selector asserting an initial-run synthesis is FRESH and turns
  STALE on reselection. Fence impact: none (`runner.py`, `models.py`, `storage.py` are
  already modified).
- **Blocks Mick's design acceptance:** **Yes** — obligation 10's mechanism is under-specified
  and internally contradictory as sealed.

### M-3 — F-03 repair cites an existing provider property that does not exist on the primary provider

- **Location:** `reports/design/DESIGN_REPAIR_RECORD_v1_to_v2.md:42-47` ("asserts the
  concrete provider instance's `base_url` (and `api_key_env`…)… Read-only use of
  **existing public properties**; `src/bots5/providers/**` remains zero-diff") and
  `design/PREFLIGHT_APPROVAL_STATE_MACHINE.md:168-169` ("when the frozen route declares
  an `api_key_env_name`, the instance's `api_key_env` equals it"); echoed by
  `design/MUTATION_FENCE.json:173` and by the frozen-route example
  `"openrouter": {"base_url": …, "api_key_env": "OPENROUTER_API_KEY"}`
  (`CAMPAIGN_EVIDENCE_EVOLUTION.md:89`).
- **Verified source fact:** `src/bots5/providers/openai_compatible.py:74-76` exposes an
  `api_key_env` property, but `src/bots5/providers/openrouter.py` contains **zero**
  occurrences of `api_key_env`; `OpenRouterProvider` exposes only `base_url`
  (`openrouter.py:61-62`), taking the key as a value the CLI/bridge reads from the
  environment (`cli.py:90-94`). Also, the job manifest declares **no** openrouter route
  at all (`manifest.py:53-56` — `providers` config keys are `local_openai` only), so the
  frozen openrouter route comes from bridge/constructor defaults.
- **Consequence:** assertion 7 as literally specified either (a) refuses **every**
  openrouter dispatch (`getattr(provider, "api_key_env") is None ≠ "OPENROUTER_API_KEY"`),
  breaking the primary product path fail-closed, or (b) must be implemented as an
  unspecified kind-specific check, deviating from the sealed text and leaving the
  openrouter key-source binding unverified. The repair record's central factual claim
  ("existing public properties") is false for the primary provider, and adding the
  property is forbidden by the `providers/**` zero-diff guard.
- **Obligation:** CONTRACT §"Preflight / approval integrity"
  (`CONTRACT.md:49-60`, provider/route change invalidates approval); falsification F-03
  repair requirement.
- **Severity:** Medium.
- **Mechanically repairable:** Yes, inside the fence — restate assertion 7 kind-specifically
  (e.g., `base_url` + class/module kind always; `api_key_env` equality only where the
  instance exposes it; for openrouter bind env-var name + bridge construction from the
  frozen route), correct the repair record's property claim, then reseal. No `providers/**`
  change needed.
- **Blocks Mick's design acceptance:** **Yes** — the sealed F-03 repair is not implementable
  as written against the live provider bytes.

### M-4 — The F-05 neutral `cancelled_pending` marker can leak as durable state via the generic exception path

- **Location:** `design/DESKTOP_SURFACE_AND_LIFECYCLE.md:169-196` (§5.2) has the stage
  handler write `error_type="cancelled_pending"` and says the **outer** branches
  reclassify it — `TimeoutError` branch and the new `CancelledError` branch. The third
  outer path, the existing generic `except Exception` (`src/bots5/runner.py:465-480`),
  is not covered: it cancels remaining tasks and calls
  `_best_effort_internal_failure`, which sweeps **only `QUEUED`/`RUNNING`** records
  (`runner.py:68-77`). A record already written as `FAILED` + `cancelled_pending` by the
  cancelled stage task is left untouched and persists durably with a marker the design
  declares transitional ("The cause is determined by the outer terminalization").
- **Reachability:** any `StorageError`/unexpected exception escaping the pipeline while a
  sibling stage task is cancelled produces exactly this state.
- **Obligation:** CONTRACT §"Lifecycle truth" (`CONTRACT.md:76-87`, truthful typed
  outcomes); D-7 (`DESKTOP_SURFACE_AND_LIFECYCLE.md:119`).
- **Severity:** Low-Medium (state is `FAILED` with preserved uncertainty — not a false
  success — but an unspecified durable label outside the design's outcome vocabulary).
- **Mechanically repairable:** Yes, inside `runner.py` — reclassify/sweep
  `cancelled_pending` records in the generic failure path too, or document the label as a
  permitted durable outcome with defined display mapping.
- **Blocks acceptance:** Not individually; fix in the same reseal wave.

### M-5 — Headless regeneration claim and the headless approval path for new verbs are inconsistent/unspecified

- **Location:** `design/PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:58` (obligation 13 mechanism:
  "regeneration/rerun exposed headlessly") versus `MUTATION_FENCE.json:66-70` (cli.py
  why: "inspect --attempt, attempt/stale markers in status, **rerun-synthesis verb**" —
  no regenerate verb), `CAMPAIGN_EVIDENCE_EVOLUTION.md:194-199` (§6.4 verbs: status,
  inspect, rerun-synthesis — no regenerate verb), and `IMPLEMENTATION_SEQUENCE.md:26-28`
  (M0.4 — same three). Additionally, neither the fence nor §6.4 specifies how a headless
  caller obtains the mandatory `ApprovalRecord` for regeneration/rerun
  (`REGENERATION_AND_STALE_SYNTHESIS.md:37-45,167-179` require approval; the approval
  constructor is specified only as a `CampaignBridge` responsibility,
  `DESKTOP_SURFACE_AND_LIFECYCLE.md:33-35`).
- **Obligation:** CONTRACT items 4 and 11/13.
- **Severity:** Low (CONTRACT 13 is still met at engine level —
  `runner.regenerate_worker`/`rerun_synthesis` are headless entry points — but the main
  design's stated mechanism over-promises relative to its own fence and sequence).
- **Mechanically repairable:** Yes — either specify a `bots5 regenerate` verb plus the
  headless approval input form in §6.4/fence, or soften row 13 to "engine entry points
  headless; `rerun-synthesis` CLI parity".
- **Blocks acceptance:** No; fix in the same reseal wave.

### M-6 — Regeneration eligibility of v1 runs is unspecified and can produce a directory the design itself calls malformed

- **Location:** `design/REGENERATION_AND_STALE_SYNTHESIS.md:30-45` (the seven
  regeneration preconditions contain **no** evidence-version check) versus
  `CAMPAIGN_EVIDENCE_EVOLUTION.md:206` ("Version 1 directories are only ever read") and
  `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:106-112` (attempt resolution keyed on
  `evidence_version >= 2`; marker absent ⇒ v1 reader). Regenerating inside a loaded v1 run
  would write `<id>.att<N>.*` into a directory whose `run.json` still lacks
  `evidence_version`, producing a mixed directory: v1 readers would silently ignore the
  sibling attempts and selection/staleness machinery would be inert — diverging from the
  operator's explicit act while violating §7's read-only claim for v1 directories.
- **Obligation:** Contract central problem (`CONTRACT.md:41-47`, old-run readability),
  FINDINGS F-01/F-10; append-only preservation.
- **Severity:** Medium.
- **Mechanically repairable:** Yes — add preconditions 0 to regeneration/rerun
  ("run must be `evidence_version >= 2`; otherwise typed refusal, nothing written") plus
  one T0 selector. Consistent with the design's own fail-closed stance; no fork.
- **Blocks acceptance:** **Yes** — without it a literal implementation can create evidence
  layouts the sealed design elsewhere forbids.

### Cosmetic notes (non-blocking)

- `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:103` and `SPECIALIST_DISAGREEMENTS.md:51` name a
  symbol `STAGE_ID_RE`; the live symbol is `SAFE_ID_RE` (`paths.py:10-11`). The cited
  lines and the permitted `.`/`-` fact are correct; only the name is wrong.
- `preflight.json` schema example (`CAMPAIGN_EVIDENCE_EVOLUTION.md:79-99`) reuses the key
  `schema_version` for both job schema and document schema (`schema_version_job` appears
  alongside); harmless but should be disambiguated when M-3's route block is restated.

## 3. Authority fidelity — CONTRACT obligations 1–13 and forbidden scope

Verified against live bytes; each mechanism exists or is inside the fence:

| # | Mechanism | Verified |
|---|---|---|
| 1 | `manifest.load_job` via bridge, explicit path (HSF-3) | `manifest.py:349-352` real |
| 2 | same validator pair as `bots5 validate`; no provider, no runs dir | `cli.py:73-77` side-effect-free; `tests/test_cli_views.py:14-23` real; T0.1 |
| 3 | `PreflightSnapshot` in memory; `preflight.json` only on approved start | state machine §1/§7 coherent with `runner.py:259-271` ordering |
| 4 | digest+scope+target assertions **before** `create_run_tree` and before first `provider.complete`; refusal creates nothing | feasible at `runner.py:259→271`; one-shot marker §3.1; `create_run_tree` refusal verified `storage.py:109-110` |
| 5 | bounded polling (250/1000 ms) over `run.json`/`stages/*`/`usage.json`; known subtotal + explicit unknown | files and shapes real (`storage.py:128-159`, `usage.py:36-69`); no fabricated cost |
| 6 | attempt-aware readers + `result.md` mirror; CLI `inspect` shape kept | `cli.py:143-165`, fence `cli.py` contract "v1 outputs and exit codes unchanged" |
| 7 | projection maps durable states exactly; `running` without hosted task = interrupted/uncertain | `DESKTOP_SURFACE…` §4.1; no resume path exists (verified: no resume code in `runner.py`) |
| 8 | selection durable; `New Job` clears context only; restart = HSF-2 (correctly a fork, F-15) | register + `VALIDATION.md:63-65` citation real |
| 9 | flat `<id>.att<N>` append-only, exclusive create, explicit model change, route locked, never automatic | preconditions §2; containment preserved (`storage.py:195-198` verified); M-6 caveat |
| 10 | pure staleness predicate over durable evidence | predicate sound; **M-2 gap for initial runs** |
| 11 | `rerun_synthesis` explicit; new attempt; selection only on success; earlier evidence untouched | §5 coherent; cost gate uses derived selected cost (F-07) |
| 12 | `evidence_version` marker; v1 readers byte-identical; golden fixtures gate-first | `IMPLEMENTATION_SEQUENCE.md:7-21`; 76 tracked `evidence/**` files verified via `git ls-files` |
| 13 | CLI verbs/exit codes unchanged; new parity verbs; `core/application.py` zero-diff | fence `unchanged_guards` ✓; **M-5 wording caveat** |

Forbidden scope: every CONTRACT exclusion (`CONTRACT.md:94-109`) is explicitly excluded in
`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:162-168` and `MUTATION_FENCE.json:155-166`. No
dependency change, no manifest schema change (so no `JOB_SPEC.md` obligation —
`docs/DEVELOPMENT.md` campaign-manifest section verified), no provider streaming, no
daemon, no campaign authoring, no migration unless HSF-2 2b, no pricing registry, no
Phase 11/12. The fence is exactly 9 modify + 8 add (`MUTATION_FENCE.json:19-141`) and is
sufficient for all repairs including M-1…M-6 (no expansion required).

## 4. Implementability and named source facts (checked against live bytes)

All falsification/repair-record source citations were checked; correct results:

- `runner.py:253-271` — `run_job(job, providers, *, run_id=None)`; reads prompts
  (`264-266`) and inputs (`267`) **after** validation (`259`) and **before**
  `create_run_tree` (`271`) ✓ (TOCTOU and "refusal creates no run dir" both real).
- `runner.py:231-250` — provider mapping validated for presence + callable `complete`
  only; no route check ✓ (F-03 premise true).
- `runner.py:404-415` — synthesis input built from current in-memory worker outputs ✓
  (F-01 premise true).
- `runner.py:201-215` — cancellation handler writes `error_type="run_timed_out"` and
  re-raises ✓ (F-05 premise true); `runner.py:428-464` timeout branch, `runner.py:465`
  `except Exception` only ✓.
- `runner.py:169-179` — `ProviderError` → stage `failed`, `provider_side_outcome_unknown`
  untouched (stays `False`, `models.py:110`) ✓ (F-04 premise true); timeout branch at
  `157-168` already sets unknown=True.
- `errors.py` — `ProviderError`, `ProviderHttpError(status_code)` (`:28-31`),
  `ProviderResponseError`, `ProviderTimeoutError` all exist; `ApprovalInvalidatedError`
  and `definitive_rejection` do not (fence adds them in `errors.py` ✓).
- Both adapters raise `ProviderError("provider_transport_error: …")` after attempting the
  request (`openrouter.py:208-227`, `openai_compatible.py:229-248`) and
  `ProviderHttpError` on non-2xx ✓ — D-9's classification is implementable with zero
  adapter diff ✓, except the `api_key_env` claim (**M-3**).
- `storage.py:109-110` refuses existing run id ✓; containment predicate at `:195-198`
  requires output parent == `stages/` ✓ (flat layout keeps it unchanged — D-2 adjudication
  correct); `_atomic_write` refuses symlinks ✓.
- `events.py:13-25` — exactly 11 `EVENT_TYPES`; writer fail-closed at `:40-42` ✓
  (additive kinds in fence ✓).
- `models.py:10-14` — `RunState = running|succeeded|failed|timed_out` ✓ (HSF-4 premise
  true); `StageRecord.to_dict` serializes nested failure incl.
  `provider_side_outcome_unknown` ✓ (v2 additive fields with defaults keep v1 loadable ✓).
- `usage.py` — `aggregate_cost`/`usage_document` real; `aggregate`/`stages` keys exist as
  the F-07 derivation rule requires ✓; `usage.py` is inside the fence ✓.
- `paths.py:10-18` — ID regex permits `.`/`-` ✓ (symbol misname, cosmetic);
  `locate_run_dir` requires operator-supplied id (HSF-3 premise ✓).
- `manifest.py` — side-effect-free load/validate ✓; no pricing anywhere in `src/bots5`
  (grep: zero hits) ✓ (HSF-1 premise true); `job_to_dict` persists no secret, only
  `api_key_env` name ✓ (`manifest.py:425-431`; corroborating test at
  `tests/test_runner.py:373-400` real).
- `window.py` dock composition seam and Phase 9 import-queue dock exist ✓;
  `bootstrap/desktop.py:275-345` `_close_driver` with precedence table and authority
  released last ✓ — the "one more bounded stage" insertion is implementable ✓.
- `docs/OPERATING_PROCEDURE_V1.md` §4 (advertised-rate observation + conservative upper
  bound from prompt size, stage count, output ceilings), the `result.md` non-proof line,
  and the runs-dir/cwd asymmetry section all match the design's citations ✓;
  migration head `0012_phase9_archive_import` exists ✓.
- `core/application.py` is 2775+ lines of Phase 1–9 machinery kept zero-diff — consistent
  with fence and with obligation 13 ✓.

**Fence conclusion:** the design is implementable inside 9 modify / 8 add against the
live code, subject to restating assertion 7 per M-3 (still zero `providers/**` diff) and
adding the M-2/M-6 clauses (already-fenced files only).

## 5. Verification of the eight repaired defects (F-01…F-08)

| F | Genuinely repaired? | New contradiction / hole opened? |
|---|---|---|
| F-01 operation-snapshot binding | **Yes** — `OperationSnapshot` (PREFLIGHT §2.2) binds run/stage/attempt/model/route/frozen messages and, for rerun, each dependency attempt + output SHA-256 + exact rendered message; dispatch is frozen, re-verified, refuse-on-diff (§4.6; REGEN §3/§5) | None in the binding itself. Adjacent: full-run provenance recording unspecified (**M-2**) — binding emptiness for full_run is defensible (same-run outputs), but recording must be stated |
| F-02 one-shot approval | **Yes** — `approval_id` + engine-side exclusive-create `<run>/approvals/<id>.json` before first `request_sent`, applies to non-UI callers and across restarts (§3.1), with independent durable guards; `storage.py:109-110` verified real, `persist_stage_attempt` exclusive-create is fenced | None found. Replay via deleted marker still fails closed through attempt/run namespace refusal |
| F-03 provider-route-object binding | **Yes in intent** — §4.7 asserts key set, `base_url`, `api_key_env`, kind before dispatch; bridge resolves providers from frozen route | **M-3**: the `api_key_env` check cites a property that does not exist on `OpenRouterProvider`; as written it breaks or weakens the main path |
| F-04 provider-error uncertainty | **Yes** — D-9: post-dispatch failures are ambiguous (unknown) unless definitive non-acceptance (4xx except 408/429, pre-network config rejection, explicit `definitive_rejection`); `ProviderResponseError` distinct; default fail-safe unknown; never auto-retried; `errors.py` fenced, adapters untouched | None; classification verified implementable against real exception types/strings |
| F-05 cancellation labelling | **Yes in mechanism** — neutral `cancelled_pending` stage marker; `TimeoutError` branch reclassifies to `run_timed_out`, new `CancelledError` branch to HSF-4 type; both preserve `provider_side_outcome_unknown`; genuine timeouts keep their label | **M-4**: the third outer path (generic `except Exception`) can leave `cancelled_pending` durable |
| F-06 three-outcome staleness | **Yes** — FRESH / STALE / UNVERIFIABLE + LEGACY_UNVERIFIED only for `evidence_version` 1; malformed maps → UNVERIFIABLE; digest/byte anomalies → STALE + integrity warning (§4.2-4.3; display table `DESKTOP_SURFACE…:152-155`) | **M-2**: the stricter honesty rule exposes that initial-run provenance is never specified to be written — every first-generation v2 synthesis would read UNVERIFIABLE |
| F-07 derived selected cost | **Yes** — `selected_spend`/`aggregate` derived at read time from `per_attempt` + `selection.json`; stored copy is a cache; write ordering + crash reconstruction specified; pre-synthesis gate uses derived value; `cumulative_spend` untouched (`CAMPAIGN_EVIDENCE_EVOLUTION.md:136-155`) | None; `usage.py`/`storage.py` fenced; legacy `aggregate`/`stages` keys preserved (verified against `usage.py:57-69`) |
| F-08 validation coverage | **Yes** — T0 rewritten: desktop-cancel selector now requires a **terminal** record (crash case keeps its own test); T0.5 has 8 selectors incl. bidirectional reselection, v1-vs-v2 missing provenance, missing bytes, digest mismatch, post-approval selection/bytes change; T0.2 adds prompt/contract bytes, provider route, dependency selection, output target, swapped bytes; T0.3 adds replay; T0.7 adds derived cost + uncertainty classification (`VALIDATION_PLAN.md:66-75`) | None; note M-2 suggests one more selector (initial-run FRESH→STALE), and M-6 suggests a v1-run refusal selector |

F-09 (pricing branch D) was correctly classified as a human fork and the register/state
machine were repaired — but **M-1** shows the repair missed the main design's fork table.

## 6. Lifecycle / evidence semantics

- **Append-only preservation:** attempts are new files with exclusive-create; `persist_stage`
  legacy signature retained; `os.replace` targets only new names; `evidence/**` untouched
  (fence guard); v1 directories read-only (M-6 caveat) ✓.
- **Mechanical staleness:** pure function of `selection.json` + recorded provenance +
  current output digests; bidirectional; no UI/DB participation ✓ (M-2 caveat for initial
  runs).
- **Old-run readability:** marker-selected readers, byte-identical v1 path, golden-fixture
  gate **before** any storage/models mutation (`IMPLEMENTATION_SEQUENCE.md:7-21`), T2.4
  mandatory on schema change ✓.
- **No second truth:** filesystem authoritative; no event bus, no SQLite campaign table;
  poller read-only; events narrative-only ✓ (D-1..D-8 verified against authority).
- **Truthful uncertainty:** D-4 inference for `running`+`started_at`; D-9 ambiguous-vs-
  definitive classification; interrupted/uncertain for hostedless `running`; never "did
  nothing" ✓.
- **No resume/retry leakage:** no resume code path exists; forbidden-operations list is
  fail-closed; regeneration/rerun each require fresh validation+snapshot+approval;
  one-shot consumption is engine-side (not a UI property) ✓.
- **Shutdown:** engine-side terminalization before event-loop teardown; close driver
  verifies terminality from disk and displays interrupted if the write failed; OS kill
  remains truthfully `running` ✓.

## 7. Validation non-vacuity

- **Reused existing selectors: 22/22 verified present** by exact `def` match in the live
  tree (`test_runner.py` 11, `test_cli_views.py` 5, `test_desktop_draft1.py` 1,
  `test_phase4.py` 1, `test_phase9_desktop_slice_e.py` 3, `test_phase1_core.py` 1) —
  matching `VALIDATION_PLAN.md:24-50`.
- **Zero-provider collect probe:** `pytest --collect-only` over the plan's baseline
  campaign selection (9 files) collected **115** tests — matching the design's stated
  baseline count (`VALIDATION_PLAN.md:52-55`); collection non-zero, exit 0. The
  historical "115 passed" claim was **not** re-executed (evidence limitation, §9).
- **Planned selectors:** every new selector names one of the six test files that exist in
  the fence's `add` list; rule 0.1 (collected-count proof), 0.2 (selector valid only after
  implementation), 0.3 (no count-scraping reruns), 0.4 (serial Qt/T4), 0.5 (fakes + socket
  block + `OPENROUTER_API_KEY` deletion — all three verified real in
  `conftest.py`/`helpers.py`) are stated and consistent with `parcel/VALIDATION.md` and
  the CONTRACT anti-vacuity rules ✓.
- F-08's specific gaps (forbidden-state acceptance, missing invalidators) are closed in
  the rewritten T0; remaining selector suggestions are noted under M-2/M-6.
- Candidate-seal governance (seal-before-review, final T4 on exact seal, post-T4 byte
  verification, unsealed review = historical only) matches `VALIDATION.md:83-107` ✓.

## 8. Human semantic forks HSF-1…HSF-5

- **HSF-1 pricing authority — genuine fork, correctly identified, framing defect M-1.**
  Verified: no pricing code anywhere in `src/bots5` (grep zero hits); historical
  `preflight.json` pricing blocks are human-produced; OPv1 §4 (verified in
  `docs/OPERATING_PROCEDURE_V1.md`) genuinely requires advertised-rate observation **and**
  a conservative dollar upper bound. The register now states A is the only branch
  OPv1-compliant by default and D requires an explicit Mick waiver/supersession — correct
  — but the main design's table still says "(D) acceptable" (**M-1**). The OPv1
  dollar-bound question is correctly framed: "unknown + approval" does **not** satisfy
  OPv1 absent an explicit waiver; branches B/C are correctly excluded (fence/zero-network).
  Paid approval UI is gated on Mick's decision. **Mick must adjudicate.**
- **HSF-2 restart persistence — genuine** (F-15; app-DB pointer would cross migration
  authority; `VALIDATION.md` warning citation verified). Recommended 2a. **Mick must
  adjudicate** (fence impact: migration only under 2b).
- **HSF-3 run discovery — genuine but low-blocking** (no enumeration seam exists in
  `src/`; `locate_run_dir` requires an id). Recommended 3a. **Mick may adjudicate or
  accept the recommendation.**
- **HSF-4 cancellation vocabulary — genuine** (`RunState` has no `CANCELLED`;
  obligation 7 names four outcomes, not cancellation). Mechanism fixed regardless;
  **Mick must adjudicate** — it gates full T0.6/T0.8 validation.
- **HSF-5 provider change — correctly scoped as optional-expansion with the default
  settled** (route locked; model string changeable matches CONTRACT item 9 and F-04).
  **Mick may confirm 5a.**

No additional hidden product fork was found; approval-substrate (D-1) and attempt-layout
(D-2) reclassifications as engineering choices are justified by the CONTRACT's own fork
criterion and by the verified containment predicate.

## 9. Evidence limitations (not product/design defects)

- No tests were executed; only read-only `--collect-only` probes (zero-provider,
  socket-blocked). Test pass counts, Qt behavior, and anything about unimplemented
  Phase 10 code are **not claimed** by this oracle.
- The historical "115 passed" baseline figure is design-phase evidence; this oracle
  verified collection = 115, not execution.
- OrgMem blob pins were checked for internal consistency (seal ↔ `BASELINE.json`) but the
  OrgMem repository was not fetched; no creator-benchmark claims were verified (routing
  evidence only).
- No provider/API calls, no dependency installation, no repository/parcel/design/seal
  mutation; the only file written is this report.

## 10. What blocks acceptance, and exactly what Mick must decide

**Blocking findings (all mechanically repairable, all inside the existing fence, all
require a seal v3 reseal):** M-1 (main design still calls branch D "acceptable"),
M-2 (initial-run synthesis provenance recording unspecified — obligation 10's predicate
can never return FRESH/STALE on first-generation runs), M-3 (F-03's `api_key_env` check
cites a property absent on `OpenRouterProvider` — unimplementable as written), M-6
(regeneration of v1 runs unspecified — can create forbidden mixed directories).
**Non-blocking but fix in the same wave:** M-4 (`cancelled_pending` durable leak via the
generic exception path), M-5 (headless-regeneration claim vs fence/sequence; headless
approval path unspecified), plus the cosmetic `STAGE_ID_RE` name.

**Mick's required adjudications (human decisions, not mechanical):**

1. **HSF-1 pricing branch:** authorize A (operator-entered *currently advertised* rates
   with recorded source/time/route and the fixed conservative bound basis), or explicitly
   waive/supersede OPv1 §4's dollar bound to authorize D. Paid approval UI must not be
   built until this is decided.
2. **HSF-2:** filesystem-only restart behavior (2a) vs app-DB pointer with migration (2b).
3. **HSF-3:** explicit path/id only (3a) vs read-only run browsing (3b, fence addition).
4. **HSF-4:** add `RunState.CANCELLED` (4a) vs reuse `FAILED` + `error_type="cancelled"`
   (4b); gates T0.6/T0.8 wait on this.
5. **HSF-5:** confirm provider route locked (5a, default) vs explicit provider switch (5b).
6. **Gate decision:** whether to accept design acceptance **after** the supervisor applies
   M-1…M-6 as a mechanical repair + reseal v3 (no fence change, no scope change), or to
   return for a further falsification round first.

Nothing in this report requires scope expansion, a dependency change, or a new human
fork beyond HSF-1…HSF-5.
