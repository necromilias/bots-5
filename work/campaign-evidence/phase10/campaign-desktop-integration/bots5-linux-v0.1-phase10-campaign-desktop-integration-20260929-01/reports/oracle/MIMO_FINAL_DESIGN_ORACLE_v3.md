# Fresh MiMo final Phase 10 design oracle (seal v3) — second and final design round

## 0. Recomputed seal identity and seal-integrity statement

- Recomputed `sha256(seals/design/DESIGN_SEAL_v3.json)` =
  **`b08d85744065ab6d1b41e4f83912f4f194bc025725f6d95ab4c7b959874f1ded`** — matches the
  expected identity exactly (file size 5111 bytes). The adjacent
  `seals/design/DESIGN_SEAL_v3.sha256` records the same value with the same filename.
- **Every seal-listed entry was independently rehashed with byte counts: 23/23 match,
  0 missing, 0 mismatched** — 12 `design_artifacts`, 9 `referenced_inputs` (including both
  repair records), 1 `review_input` (GPT-6 Luna falsification), 1 `oracle_input`
  (the v2 MiMo oracle, sha `8dfab3b48aa0aff430b231b23d065ff7bc0220518a2954d5fb869b4d3a8cb21c`,
  34247 bytes, matching the file on disk).
- **Chain intact and preserved:** recomputed `sha256(DESIGN_SEAL_v1.json)` =
  `7ecbb502bd65dcc2ad1595b1dd709db3eff0bc5d5e079b00f8e77c29ee6d952f` and
  `sha256(DESIGN_SEAL_v2.json)` =
  `2a57410dc1da155aab7fcbf480a244b999eac3525a9c7737e40adc703fdee0e4` — both equal the
  expected values and both sidecars agree. v3 declares
  `supersedes = {DESIGN_SEAL_v2.json, 2a57410d…dee0e4}` (`DESIGN_SEAL_v3.json:147-150`)
  and lists both predecessors in `seal_chain` (`:135-144`) plus both repair records
  (`:124-127`). v2 in turn declares supersession of v1 (`DESIGN_SEAL_v2.json:121-125`).
- **Baseline identity:** live `git rev-parse HEAD` =
  `0756904481ae884bb9e864e8e1e11fc4a27a72ff`, live tree =
  `7fe879035a01a87339dfe6319a435fa0e84c94bd` — equal to the seal pins
  (`DESIGN_SEAL_v3.json:2-3`), to `MUTATION_FENCE.json:4-6`, and to `parcel/BASELINE.json`.
  `git status` shows only untracked workspace material (`work/`, audit temps) before and
  after this review; the tracked tree is clean. `orgmem_ref` `cc0c3c80…` matches
  `BASELINE.json` (internal consistency only — OrgMem was not fetched; §10).
- **Coverage of the repaired bytes:** diffing the v2 seal map against the v3 seal map shows
  exactly 9 design artifacts changed (`CAMPAIGN_EVIDENCE_EVOLUTION`, `DESKTOP_SURFACE_AND_
  LIFECYCLE`, `IMPLEMENTATION_SEQUENCE`, `MUTATION_FENCE.json`,
  `PHASE10_CAMPAIGN_DESKTOP_DESIGN`, `PREFLIGHT_APPROVAL_STATE_MACHINE`,
  `REGENERATION_AND_STALE_SYNTHESIS`, `SPECIALIST_DISAGREEMENTS`, `VALIDATION_PLAN`) plus
  the new `DESIGN_REPAIR_RECORD_v2_to_v3.md` and the new `oracle_inputs` entry — i.e. the
  seal covers the bytes the v2→v3 ledger claims to have repaired, and nothing outside the
  design-evidence area was touched.

**Seal integrity: PASS.** No blocking seal/parcel/baseline/chain finding.

## Overall verdict

**PASS_WITH_LIMITATIONS.**

Seal integrity passes outright. All six prior-oracle findings M-1…M-6 are genuinely
repaired in the sealed v3 bytes (§2), each repair is implementable inside the unchanged
9-modify/8-add fence, no forbidden scope, dependency change, second truth, or resume/retry
leakage was introduced, CONTRACT obligations 1–13 each retain a sufficient mechanism (§4),
the staleness predicate is mechanically correct for first-generation runs after the M-2
repair (§6), and validation remains non-vacuous and provable (§7). HSF-1…HSF-5 remain
genuine human forks, correctly framed, with HSF-1's OPv1 dollar-bound question stated
honestly in all three places that discuss it (§8).

Limitations: four **non-blocking, mechanically repairable** residual defects (N-1…N-4,
§3) — all documentation/specification level, none undermining an obligation mechanism —
plus two pre-existing wording ambiguities noted for the record (N-5, N-6). None of these
blocks Mick's design acceptance; Mick must decide whether to accept with the errata
recorded or require one further micro-reseal (§11). HSF-1 still blocks *paid-approval UI*,
not design acceptance.

This is a design-evidence finding only. No product code was changed, no test was executed
beyond read-only `--collect-only` probes, no provider/API call was made.

---

## 1. Review identity, method, evidence boundary

- Model: `xiaomi/mimo-v2.6-flash`. Requested effort `provider_maximum_if_exposed`; the
  harness exposes no per-child effort parameter, so **effective effort: not exposed** (no
  silent substitution).
- Read-only exact-seal review: seal chain (v1/v2/v3 + sidecars), all 23 seal-listed
  entries, all 12 sealed design artifacts, both repair records, the v2 oracle, the
  falsification, parcel authority (`CONTRACT.md`, `SCOPE.json`, `BASELINE.json`,
  `FINDINGS.md`, `VALIDATION.md`, `SOURCES.md`), `MODEL_ROUTING_EVIDENCE.md`.
- Live pinned source verified byte-level: `runner.py`, `storage.py`, `models.py`,
  `events.py`, `usage.py`, `cli.py`, `paths.py`, `manifest.py`, `rendering.py`,
  `providers/openrouter.py`, `providers/openai_compatible.py`, `docs/OPERATING_PROCEDURE_V1.md`,
  `docs/DEVELOPMENT.md`, `tests/conftest.py`, `tests/helpers.py`, migration head
  `0012_phase9_archive_import`.
- Zero-provider probes (repository `.venv`, socket-blocked by `tests/conftest.py`):
  `pytest --collect-only` over the plan's baseline campaign selection; exact `def` match
  of all 22 reused selectors; `git ls-files evidence` count.
- Not done: no test execution, no network, no dependency installation, no mutation of
  repository/parcel/design/seal; the only file written under the campaign pack is this
  report. One transient scratch capture of a collect-only stream was written outside the
  pack at `/tmp/collect.txt` (platform temp; not evidence, not referenced by the design).

## 2. Prior findings M-1…M-6: are the repairs real?

Verified against the **sealed v3 bytes** (hashes confirmed in §0), not against the ledger.

### M-1 — main design no longer calls pricing branch D compliant — **REPAIRED**

- `design/PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:160` (§6 fork table, HSF-1 row) now reads:
  "Operator-entered **currently advertised** rates (A) — the only branch compliant by
  default; unknown-cost+approval (D) only under an **explicit Mick waiver of the OPv1 §4
  dollar bound**, not an equivalent branch". The word "acceptable" is gone from the cell;
  a revision-history note was added at `:16-21` naming M-1…M-6 and both ledgers.
- Agreement verified three-way: register `HUMAN_SEMANTIC_FORK_REGISTER.md:34,36-44`
  ("**Does not satisfy** OPv1's dollar-bound requirement… only under an explicit Mick
  waiver"); state machine `PREFLIGHT_APPROVAL_STATE_MACHINE.md:211-215` ("**D does not
  satisfy** the OPv1 dollar-bound requirement… available only if Mick *explicitly waives or
  supersedes*"); main design `:160`. A pack-wide grep for "acceptable"/"(D)" finds no
  remaining compliant-D framing anywhere in `design/`.
- Falsification requirement F-09 (`GPT6_LUNA_DESIGN_FALSIFICATION.md:125`, "must not present
  D as compliant without the explicit waiver") is met.

### M-2 — initial full-run synthesis provenance recorded at dispatch — **REPAIRED**

- `REGENERATION_AND_STALE_SYNTHESIS.md:108-138` (new §3.1) specifies the full-run path:
  selection absent ⇒ attempt 1 per `CAMPAIGN_EVIDENCE_EVOLUTION.md` §2.2 (`:117-119`);
  `dependency_digests = sha256(bytes rendered into that message)` (`:120-121`);
  `consumed_dependencies = {dep: <selected attempt>}` (`:122`); **persisted on the synthesis
  attempt record before `request_sent`** inside `run_job` (`:123-124`), so a crash cannot
  leave a v2 synthesis attempt without provenance.
- The cited source fact is real: `runner.py:405` (`synthesis_user = render_synthesis_…`),
  `runner.py:410` (`user_message=synthesis_user`) — inside the cited window 404-415; the
  attempt record exists at that point because `RUN_STARTED` creates "attempt 1 stages"
  (`PREFLIGHT_APPROVAL_STATE_MACHINE.md:45`) before any dispatch.
- Digest/predicate consistency checked against live bytes: `storage.persist_stage` writes
  `stages/<id>.md` as the bare output text (`storage.py:119-125`), and
  `render_synthesis_user_message` embeds those same texts verbatim between fixed wrappers
  (`rendering.py:11-15`), so `sha256(file bytes) == sha256(bytes rendered)` holds and the
  §4 predicate `H(w,σ(w))` (`REGENERATION…:147-155`) compares equal quantities.
- Approval emptiness for `full_run` is retained and justified (`PREFLIGHT…:98`;
  `REGENERATION…:126-129`) — same-run outputs need no pre-dispatch byte binding.
- Selector added: `VALIDATION_PLAN.md:75`
  `test_initial_run_synthesis_is_fresh_and_becomes_stale_after_regeneration_and_reselection`.
- Consequence verified: initial-run synthesis is FRESH at creation and flips to STALE on
  regeneration + reselection ⇒ **CONTRACT obligation 10 fires for first-generation runs.**

### M-3 — kind-specific provider route validation, implementable with `providers/**` zero-diff — **REPAIRED (mechanism), with one unrepaired echo (N-1)**

- `PREFLIGHT_APPROVAL_STATE_MACHINE.md:165-188` (§4.7) is restated kind-specifically:
  always assert provider key-set equality and `getattr(provider, "base_url", None)` equals
  the frozen `base_url` plus class/module kind (`:171-175`); assert `api_key_env` equality
  **only where the instance exposes it** (`:176-177`); for kinds without such a property
  (openrouter) bind the key source **by construction** via the bridge, with the limitation
  documented as requiring a forbidden provider-contract change to lift (`:178-184`), plus a
  swap test (`:184`).
- Source facts independently confirmed against live bytes:
  - `src/bots5/providers/openrouter.py` has **zero** occurrences of `api_key_env`; its only
    public route property is `base_url` at lines 61-62 — exactly as cited.
  - `src/bots5/providers/openai_compatible.py:78` `base_url`, `:82` `api_key_env` — the
    state machine's `:77-83` and the ledger's `:81-83` citations are both accurate.
  - `cli.py:_build_providers` reads `OPENROUTER_API_KEY` from the environment and passes it
    as a value (`cli.py:90-94`), and the manifest's provider-config keys are `local_openai`
    only (`manifest.py` `PROVIDER_CONFIG_KEYS`) — so construction-based binding for
    openrouter is the only implementable option and the design now says so honestly.
- Consequence: assertion 7 as sealed no longer refuses every openrouter dispatch and no
  longer requires a `providers/**` change. The mechanism is implementable as written.
- Residual: the false "existing public properties" claim survives in one sealed location —
  see **N-1**.

### M-4 — `cancelled_pending` can never persist — **REPAIRED (all three outer paths), with a crash-edge caveat (N-4)**

- `DESKTOP_SURFACE_AND_LIFECYCLE.md:202-209` (new "Generic-failure path (M-4 repair)")
  extends the `except Exception` / `_best_effort_internal_failure` path
  (`runner.py:465-477`, `runner.py:56-98`) to reclassify any `cancelled_pending` record to
  `internal_error`, preserving `provider_side_outcome_unknown` when `started_at` is set, and
  to persist it: "`cancelled_pending` is therefore never a permitted durable outcome".
- Ordering verified in live code: the generic handler cancels the worker tasks and
  **awaits them** (`runner.py:466-470`) *before* calling `_raise_after_best_effort_failure`
  (`:471`), so sibling markers written by the stage-level `except asyncio.CancelledError`
  block (`runner.py:201-215`) land before the extended sweep runs — the repair is
  sequenced correctly. `_best_effort_internal_failure` is shared by all three outer paths
  (initial persist `:294`, final persist `:499`, pipeline `:465`), so one extension covers
  them all.
- The other two paths remain as specified: `TimeoutError` branch reclassifies to
  `run_timed_out` (`DESKTOP…:181-183`), new `CancelledError` branch to the HSF-4 type
  (`:184-196`).
- Selector added: `VALIDATION_PLAN.md:76`
  `::test_cancelled_pending_is_never_a_permitted_durable_outcome`.
- Caveat (N-4): the categorical wording does not survive process death between the marker
  write and outer terminalization, and no display row covers that state.

### M-5 — headless parity verbs + consent form consistent across artifacts — **REPAIRED (one wording gap, N-2)**

Four-way agreement, verified at line level:

| Artifact | Statement |
|---|---|
| `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:65` (obligation 13) | new parity verbs `inspect --attempt`, `status` markers, `rerun-synthesis` **and** `regenerate`, "both preflight-only unless `--approve --actor <label>` is given"; engine entry points usable as library calls |
| `CAMPAIGN_EVIDENCE_EVOLUTION.md:204-215` (§6.4) | `regenerate RUN_ID STAGE_ID --model M`, `rerun-synthesis RUN_ID`; consent via `--approve --actor <label>` **or** `--approval <path>`; without consent they print preflight and exit with no provider request; `validate`/`run` unchanged |
| `MUTATION_FENCE.json:67-71` (`cli.py` modify) | "regenerate and rerun-synthesis verbs (preflight-only unless `--approve --actor/--approval` is supplied)"; contract "v1 outputs and exit codes unchanged" |
| `IMPLEMENTATION_SEQUENCE.md:26-28` (M0.4) | same verbs + "headless consent form (`--approve --actor` / `--approval`, preflight-only without it)" |

Obligation 4 is satisfied for headless callers (consent is an explicit `ApprovalRecord`,
not a prompt), and T0.11 selectors exist (`VALIDATION_PLAN.md:78`) in a fenced test file.
Gap: the main-design cell omits the `--approval <path>` channel (N-2).

### M-6 — v1 runs read-only for regeneration/rerun — **REPAIRED**

- `REGENERATION_AND_STALE_SYNTHESIS.md:37-42` (§2 precondition 0) and `:206-208` (§5
  precondition 0): target run must declare `evidence_version >= 2`; a v1 run is read-only,
  regeneration/rerun refused with a typed reason and **nothing written**, with the mixed-
  layout and v1-invisibility reasoning stated; the operator may instead start a new run.
- Consistent with `CAMPAIGN_EVIDENCE_EVOLUTION.md:220` ("Version 1 directories are only
  ever read") and with main design `:114` attempt resolution keyed on `evidence_version >= 2`.
- Selectors added: `VALIDATION_PLAN.md:74`
  `::test_regeneration_refused_for_evidence_version_1_run_writing_nothing` and `:75`
  `::test_synthesis_rerun_refused_for_evidence_version_1_run_writing_nothing`.
- No other sealed artifact permits writing into a v1 directory (pack-wide `evidence_version`
  grep reviewed).

### Cosmetic repairs from the v2 oracle — verified

- `SAFE_ID_RE` now correct at `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:110` and
  `SPECIALIST_DISAGREEMENTS.md:51` (live symbol confirmed at `paths.py:10`).
- `preflight.json` example disambiguated: `doc_schema_version` vs `job_schema_version`
  (`CAMPAIGN_EVIDENCE_EVOLUTION.md:88-89`), and the route block restated as
  `kind`/`api_key_env_name`/`key_source` (`:96-98`).

**No new contradiction of blocking weight was introduced by the repairs.** Four low-level
residuals were found — see §3.

## 3. New / residual findings (this round)

### N-1 — Sealed `MUTATION_FENCE.json` still carries the false M-3 source claim (unrepaired echo)

- **Location:** `design/MUTATION_FENCE.json:173` (notes):
  "`src/bots5/providers/**` remains a zero-diff guard: provider-object route validation
  reads existing public properties (base_url, api_key_env) without changing provider
  semantics."
- **Why false:** `OpenRouterProvider` exposes no `api_key_env` (verified: zero occurrences
  in `openrouter.py`), so validation cannot read it there; §4.7 now correctly says exactly
  that (`PREFLIGHT…:176-183`). The v2 oracle listed this fence note as one of the echoes of
  the M-3 defect; the v2→v3 ledger corrected the *ledger* claim and the *state machine* but
  not this note, while asserting M-3's echoes were addressed (`DESIGN_REPAIR_RECORD_v2_to_v3.md:43-54`).
  (The v1→v2 ledger's identical claim at `:44` is preserved unchanged — correct, since
  prior findings must not be rewritten.)
- **Obligation:** CONTRACT §"Preflight / approval integrity"; F-03 repair completeness.
- **Severity:** Low. It is a rationale note, not mechanism-bearing text: the normative
  assertion set (§4.7), the T0.4 swap selector, and the `providers/**` zero-diff guard are
  all correct, so nothing becomes unimplementable (unlike the v2 M-3 instance).
- **Mechanically repairable:** yes — one line, inside the fence (design evidence only),
  then reseal.
- **Blocks Mick's design acceptance:** no (see §11 for Mick's choice between errata and a
  micro-reseal).

### N-2 — Main design obligation-13 cell omits the `--approval <path>` consent channel

- **Location:** `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:65`: verbs are "both preflight-only
  unless `--approve --actor <label>` is given". Read literally, an invocation carrying only
  `--approval <path>` must be preflight-only — but `CAMPAIGN_EVIDENCE_EVOLUTION.md:208-213`
  and `MUTATION_FENCE.json:68` both make `--approval <path>` an independent consent channel
  that allows spend. The v2→v3 ledger asserts the four artifacts "now agree"
  (`DESIGN_REPAIR_RECORD_v2_to_v3.md:68-73`); they agree on verbs and preflight-default but
  not on this wording.
- **Obligation:** CONTRACT item 4 (explicit approval before any provider request) and item
  13; F-13.
- **Severity:** Low. No obligation is violated either way — `--approval <path>` carries a
  real `ApprovalRecord`, so the unsafe reading (spend without approval) does not exist; the
  risk is only that a literal implementation *disallows* a consent channel the rest of the
  design specifies.
- **Mechanically repairable:** yes — add "`or --approval <path>`" to the cell (and soften
  the ledger's "now agree" claim), then reseal.
- **Blocks acceptance:** no.

### N-3 — Skipped / never-dispatched v2 synthesis attempts have no provenance and would read UNVERIFIABLE with an integrity warning

- **Location:** `REGENERATION_AND_STALE_SYNTHESIS.md:171-194` (§4.2 table) maps "v2
  attempt, provenance absent or malformed → **UNVERIFIABLE**, integrity warning"; the
  rationale at `:184-187` states a v2 attempt's missing provenance "is an evidence-integrity
  defect". Nothing carves out synthesis attempts that are **never dispatched**: dependency
  failed (`runner.py:347-361`), dependency incomplete (`:366-380`), known-cost threshold
  exceeded (`:385-399`), or not reached before timeout (`:449`) — all set
  `StageState.SKIPPED` with no `consumed_dependencies`/`dependency_digests`, because M-2's
  recording point (dispatch, §3.1 `:123-124`) never occurs. The staleness helper is
  specified unconditionally: "per-stage staleness of the selected synthesis"
  (`CAMPAIGN_EVIDENCE_EVOLUTION.md:197-202`), and the display table has no state gate
  (`DESKTOP_SURFACE_AND_LIFECYCLE.md:156-159`).
- **Consequence:** under a literal implementation, a legitimately skipped synthesis in a v2
  run is reported as an evidence-integrity defect — a warning asserting a defect where none
  exists (conservative direction: it never fabricates freshness, but it is untruthful in the
  other direction and adds noise to already-failed runs).
- **Obligation:** CONTRACT §"Lifecycle truth" (truthful outcomes) and item 11; F-14.
- **Severity:** Low-Medium (display/evidence-semantics gap on a common failure path; no
  evidence is corrupted and no false success/failure can result).
- **Mechanically repairable:** yes — add one §4.2 row ("synthesis attempt in a
  non-dispatched terminal state (SKIPPED) → provenance not applicable; show the skip reason,
  not UNVERIFIABLE"), or specify recording empty provenance at skip time, plus one selector.
  Inside the fence (`storage.py`/projection helpers only).
- **Blocks acceptance:** no.

### N-4 — Categorical "`cancelled_pending` is never a permitted durable outcome" does not hold across process death; sibling relabel loses its own cause

- **Location:** `DESKTOP_SURFACE_AND_LIFECYCLE.md:202-209`.
  1. **Crash window:** the stage-level handler writes `FAILED` + `cancelled_pending` durably
     (`runner.py:201-215`) *before* the outer terminalization reclassifies it. A SIGKILL /
     power loss in that window leaves the label durable forever. The design tolerates the
     analogous durable-`running`-after-crash case and displays it as interrupted
     (`:232-233`), but provides no display row or tolerance rule for a durable
     `cancelled_pending`.
  2. **Relabel semantics:** the M-4 sweep reclassifies a sibling that was genuinely
     *cancelled* to `internal_error` with the run-level failure message
     (`_best_effort_internal_failure`, `runner.py:56-77`). `provider_side_outcome_unknown`
     is preserved (uncertainty truth holds), but the record no longer states the stage's own
     cause; the design frames this as "the true run-level cause" (`:206-207`).
- **Obligation:** CONTRACT §"Lifecycle truth"; D-7; obligation 7.
- **Severity:** Low. Both outcomes are conservative (never a false success, never false
  zero-cost); the crash case is the same class the design already accepts for `running`.
- **Mechanically repairable:** yes — either soften the claim to "never a permitted
  *in-process* durable outcome; a crash may leave it, displayed as interrupted/uncertain
  (add a display row)", or sweep it on the next reader open; optionally use a distinct
  `error_type` for cancelled siblings instead of `internal_error`.
- **Blocks acceptance:** no.

### N-5 (informational, pre-existing wording) — exclusive-create vs state transitions

`PREFLIGHT…:140-142` ("`persist_stage_attempt` refuses to overwrite an existing attempt")
sits beside `REGENERATION…:65-67` ("create … exclusive-create … write state queued →
running → terminal"). Read together they are reconcilable — creation of a *new attempt
target* is exclusive, in-place state transitions of the same attempt are updates, and the
replay guard actually works through attempt-number re-derivation (`REGENERATION…:60-64`),
which is explicitly specified. Flagged only so the implementer does not read the note as
forbidding the queued→running→terminal writes. Present since v1; not introduced by these
repairs; non-blocking.

### N-6 (informational) — openrouter `api_key_env_name` value origin

`PreflightSnapshot.provider_routes` requires `api_key_env_name` per route
(`PREFLIGHT…:66-68`) and the example pins `OPENROUTER_API_KEY` for openrouter
(`CAMPAIGN_EVIDENCE_EVOLUTION.md:96-98`), but the manifest declares no openrouter route
config (verified: `PROVIDER_CONFIG_KEYS = {"local_openai"}`), so the value ultimately comes
from the CLI convention hardcoded at `cli.py:92-94`. The design does not name that origin
explicitly. Non-blocking (the bridge will use the same constant); worth one sentence at
implementation.

## 4. Authority fidelity — CONTRACT obligations 1–13 and forbidden scope

Verified against live bytes; each mechanism exists or is inside the fence.

| # | Mechanism (sealed design) | Verified against live code |
|---|---|---|
| 1 | `manifest.load_job` via bridge, explicit path (HSF-3) | `manifest.load_job`/`validate_referenced_files` side-effect-free; `locate_run_dir` requires an id (`paths.py:52-62`) |
| 2 | same validator pair as `bots5 validate`; no provider, no runs dir | `_cmd_validate` = load + validate only (`cli.py:73-77`); T0.1 |
| 3 | `PreflightSnapshot` in memory; `preflight.json` only on approved start | `PREFLIGHT…:39-55,192-194`; assertions run before `create_run_tree` (`runner.py:259→271`) |
| 4 | identity/scope/one-shot/disk/config/selection/route assertions before first `provider.complete`; refusal creates nothing | `PREFLIGHT…:149-194`; one-shot marker `:129-147`; `create_run_tree` refuses existing id (`storage.py:100-111`) |
| 5 | bounded polling 250/1000 ms over `run.json`/`stages/*`/`usage.json`; known subtotal + explicit unknown | `DESKTOP…:93-104`; usage shapes real; no fabricated cost |
| 6 | attempt-aware readers + `result.md` mirror; CLI `inspect --attempt` | `CAMPAIGN_EVIDENCE…:191-195`; fence contract "v1 outputs and exit codes unchanged" |
| 7 | projection maps durable states exactly; hosted-less `running` = interrupted/uncertain; no resume path exists | `DESKTOP…:143-159`; grep finds no resume code in `src/bots5/*.py` |
| 8 | selection durable; `New Job` clears context only; restart = HSF-2 | `selection.json` spec + HSF-2 register; D-8 |
| 9 | flat `<id>.att<N>` append-only, exclusive-create, explicit model change, route locked, never automatic | `REGENERATION…:13-78`; containment predicate unchanged (`storage.py:196-198`) — **M-6 precondition now present** |
| 10 | pure staleness predicate over durable evidence | `REGENERATION…:140-168`; **M-2 dispatch recording makes it fire for initial runs** (§2 above) |
| 11 | `rerun_synthesis` explicit; new attempt; selection only on success; earlier evidence untouched | `REGENERATION…:196-237`; cost gate uses derived selected cost (F-07) |
| 12 | `evidence_version` marker; v1 readers byte-identical; golden fixtures gate-first | `IMPLEMENTATION_SEQUENCE…:13-17`; 76 tracked `evidence/**` files re-counted (`git ls-files evidence`) |
| 13 | CLI verbs/exit codes unchanged; new parity verbs incl. `regenerate`; `core/application.py` zero-diff | fence `unchanged_guards` ✓; **M-5 repaired (N-2 wording gap only)** |

**Forbidden scope:** every CONTRACT exclusion is explicitly excluded in
`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:170-176` and `MUTATION_FENCE.json:155-166`
(forbidden_expansions). Pack-wide greps for streaming/daemon/authoring/registry return only
negations. No dependency change, no manifest schema change (so no `JOB_SPEC.md` obligation —
`docs/DEVELOPMENT.md` campaign-manifest section confirmed), no pricing registry, no
provider streaming, no app-DB campaign persistence, no enumeration seam, no migration
unless HSF-2 2b, no Phase 11/12. Fence is exactly 9 modify + 8 add (`MUTATION_FENCE.json:19-141`)
and is sufficient for all six M-repairs **and** for N-1…N-4 (design-evidence edits only).

## 5. Implementability against the live code; named source facts

All design citations checked this round (on top of the v2 oracle's verified set):

- `runner.py:253` `async def run_job`; validation at `:259`; prompt/input reads at
  `:264-267`; `create_run_tree` at `:271` — TOCTOU ordering and "refusal creates no run
  dir" both real.
- `runner.py:201-215` stage `except asyncio.CancelledError` (design's marker insertion
  point); `:428` `except TimeoutError`; `:465` `except Exception` (catches `Exception`, not
  `CancelledError` — HSF-4 premise true); cancel + await before sweep at `:466-470`;
  `_best_effort_internal_failure` spans `:56-98` (sweeps `QUEUED`/`RUNNING` only today).
- `runner.py:344-399, 449` synthesis SKIPPED paths (basis for N-3); `:404-415` synthesis
  input build (basis for M-2).
- `providers/openrouter.py:61-62` `base_url` only, no `api_key_env`; `providers/
  openai_compatible.py:78,82` both properties — the entire M-3 repair rests on facts that
  are true of the pinned bytes.
- `storage.py:119-125` `persist_stage` writes bare output text to `.md`; `:196-198`
  containment predicate; `:100-111` `create_run_tree` one-shot refusal.
- `events.py:13-24` exactly 11 `EVENT_TYPES`; unknown event raises (`StorageError`) —
  additive kinds in fence stay fail-closed.
- `models.py:10-14` `RunState = running|succeeded|failed|timed_out` (no `CANCELLED` — HSF-4
  premise); `:110` `provider_side_outcome_unknown` default `False`; no attempt/provenance
  fields exist yet (they are the fence's additive work).
- `paths.py:10` `SAFE_ID_RE` permits `.`/`-`; `:52-62` `locate_run_dir` requires an id.
- `manifest.py` `PROVIDER_CONFIG_KEYS = {"local_openai"}`; `PROVIDERS_V2 = {openrouter,
  local_openai}` — corroborates the M-3 finding that openrouter route config is not
  manifest-declared.
- `rendering.py:11-15` synthesis message wraps raw worker texts (digest consistency for M-2).
- No pricing code anywhere in `src/bots5` (grep: only the word "apricotKey" in a
  docstring) — HSF-1 premise true.
- `tests/conftest.py:13-19` autouse socket block + `OPENROUTER_API_KEY` deletion;
  `tests/helpers.py:31` `FakeProvider` — validation rule 0.5 is real.
- Migration head `0012_phase9_archive_import` exists; no enumeration seam in campaign code
  (`listdir/scandir` appear only in `infrastructure/backup_capture|data_root_authority|
  restore_service`) — HSF-2/HSF-3 premises true.

**Fence conclusion:** implementable inside 9 modify / 8 add against the live code, with
zero `providers/**` diff; all repairs (M and N) stay inside already-fenced files or are
design-evidence edits.

## 6. Lifecycle / evidence semantics, staleness, old-run readability, truth

- **Append-only preservation:** attempts are new files with exclusive-create; `persist_stage`
  legacy signature retained; `os.replace` targets only new names; `evidence/**` untouched
  (76 tracked files); v1 directories read-only (M-6 precondition now enforced).
- **Staleness predicate correctness:** pure function of `selection.json` + recorded
  provenance + current output digests; `.md` bytes equal rendered bytes (verified §2 M-2),
  so digests compare like with like; defaults (`selection.json` absent ⇒ attempt 1) match
  between reader (`CAMPAIGN_EVIDENCE…:63-65`), predicate, and the M-2 recording rule;
  bidirectional by construction (`REGENERATION…:160-168`); no UI/DB participates. Gap for
  non-dispatched synthesis: **N-3**.
- **Old-run readability:** marker-selected readers, byte-identical v1 path,
  gate-first golden fixtures (`IMPLEMENTATION_SEQUENCE…:13-17`), T2.4 mandatory on schema
  change; v1-refusal for regeneration (M-6) prevents mixed directories.
- **No second truth:** filesystem authoritative; no event bus, no SQLite campaign table;
  poller read-only; events narrative-only (`events.jsonl` has no programmatic reader today).
- **Truthful uncertainty:** D-4 (`running` + `started_at` ⇒ unknown), D-9 ambiguous-vs-
  definitive classification with fail-safe default unknown and `errors.py`-only change,
  interrupted/uncertain for hosted-less `running`, never "did nothing".
- **No resume/retry leakage:** no resume code path exists (grep); forbidden-operations list
  is fail-closed; regeneration/rerun each require fresh validation + snapshot + approval;
  one-shot consumption is engine-side.
- **Shutdown:** engine-side terminalization before event-loop teardown; close driver
  verifies terminality from disk and displays interrupted if the write failed; OS kill
  remains truthfully `running`. Crash-window caveat: **N-4**.

## 7. Validation non-vacuity and provability

- **Reused selectors: 22/22 verified present** by exact `def` match in the live tree
  (test_runner 11, test_cli_views 5, test_desktop_draft1 1, test_phase4 1,
  test_phase9_desktop_slice_e 3, test_phase1_core 1), matching `VALIDATION_PLAN.md:32-55`.
- **Zero-provider collect probe:** `pytest --collect-only -q` over the plan's baseline
  campaign selection (9 files) collected **115** tests (per-file 2+6+21+24+7+22+2+29+2),
  exit 0 — matching `VALIDATION_PLAN.md:57-60`. Collection is non-vacuous; the historical
  "115 passed" figure was **not** re-executed (evidence limitation, §10).
- **Repair selectors all present and named in fenced files:** M-2
  (`VALIDATION_PLAN.md:75`), M-3 swap test (`:74`), M-4 (`:76`), M-5 T0.11 (`:78`),
  M-6 v1-refusal pair (`:74-75`) — all in `tests/test_phase10_*.py` files that exist in the
  fence `add` list (verified: no `test_phase10_*` file exists yet, consistent with rule 0.2).
- **Anti-vacuity rules** 0.1–0.6 (collected-count proof, selector validity only after
  implementation, no count-scraping reruns, serial Qt/T4, fakes + socket block + key
  deletion) are stated and consistent with `parcel/VALIDATION.md` and the CONTRACT
  anti-vacuity rules; fakes/socket-block/key-deletion verified real in the live tree.
- **Candidate-seal governance** (seal before review, final T4 on exact seal, post-T4 byte
  verification, unsealed review = historical only) matches `VALIDATION_PLAN.md:145-156` and
  `parcel/VALIDATION.md:83-107`.
- Design-phase honesty: `VALIDATION_PLAN.md:158-161` states no gate has been run and no
  listed test file exists — truthful.

## 8. Human semantic forks HSF-1…HSF-5

- **HSF-1 pricing authority — genuine, correctly framed in all three places; the OPv1
  dollar-bound question is stated honestly.** Verified: no pricing code anywhere in
  `src/bots5` (grep); `docs/OPERATING_PROCEDURE_V1.md:62-75` genuinely requires both the
  highest applicable currently advertised input/output rates for each selected model/route
  **and** a conservative upper bound from prompt size, stage count and output ceilings.
  Register (`HUMAN_SEMANTIC_FORK_REGISTER.md:31-44`), state machine (`PREFLIGHT…:196-220`)
  and main design (`:160`) now agree that A is the only branch OPv1-compliant by default and
  D requires an explicit recorded Mick waiver/supersession; B/C are correctly excluded
  (fence asset / zero-network). Branch A's record shape (`PREFLIGHT…:205-210`) now includes
  observation time, route, cited rate source and the fixed bound basis — closing the
  falsification's "branch A record shape" complaint. Paid approval UI remains gated on
  Mick. **Mick must adjudicate.**
- **HSF-2 restart persistence — genuine** (F-15). The cited warning is real
  (`parcel/VALIDATION.md:63-65`: durable desktop selection state makes migration affected);
  migration head `0012_phase9_archive_import` exists; 2b expands the fence. Recommended 2a.
  **Mick must adjudicate.**
- **HSF-3 run discovery — genuine, low-blocking.** No enumeration seam exists in campaign
  code (verified); `locate_run_dir` requires an id. Recommended 3a. **Mick may adjudicate or
  accept the recommendation.**
- **HSF-4 cancellation vocabulary — genuine.** `RunState` has no `CANCELLED`
  (verified); obligation 7 names four outcomes, not cancellation. Mechanism fixed either
  way; the branch gates T0.6/T0.8. **Mick must adjudicate.**
- **HSF-5 provider change — correctly scoped as optional-expansion with the default settled**
  (route locked; model string changeable matches CONTRACT item 9 and F-04).
  **Mick may confirm 5a.**

No additional hidden product fork was found; approval substrate (D-1) and attempt layout
(D-2) reclassifications as engineering choices remain justified by the CONTRACT's own fork
criterion and the verified containment predicate.

## 9. Blocking analysis — what blocks Mick's design acceptance

**Nothing in this oracle blocks design acceptance.**

- Seal/parcel/baseline/chain: PASS (§0).
- M-1…M-6: genuinely repaired in sealed bytes (§2).
- No obligation-mechanism gap, no forbidden scope, no fence expansion, no second truth, no
  resume/retry leakage, no fabricated cost, validation non-vacuous.
- Residuals N-1…N-4 are documentation/specification defects of Low (N-3: Low-Medium)
  severity, each mechanically repairable inside the existing fence by editing design
  evidence only; none makes any mechanism unimplementable and none can produce a false
  success, false zero-cost, silent retry, or evidence loss. They are **defects**, not
  evidence limitations, and are recorded here so acceptance cannot be read as endorsing
  them silently.
- What *is* blocked without Mick: **paid-approval UI** (HSF-1 until A-vs-waiver-D is
  decided) and **full T0.6/T0.8 validation** (HSF-4 until the vocabulary is decided);
  HSF-2 decides whether the fence must include a migration before parcel-v2.

## 10. Evidence limitations (not defects)

- No tests were executed; only read-only `--collect-only` probes (zero-provider,
  socket-blocked). Test pass counts, Qt behavior, and anything about unimplemented Phase 10
  code are **not claimed** by this oracle.
- The historical "115 passed" baseline figure is design-phase evidence; this oracle
  verified **collection = 115**, not execution.
- OrgMem blob pin `cc0c3c80…` was checked for internal consistency only (seal ↔
  `BASELINE.json`); the OrgMem repository was not fetched and no creator-benchmark claim
  was verified.
- No provider/API calls, no dependency installation, no repository/parcel/design/seal
  mutation. The only file written inside the campaign pack is this report; one transient
  scratch capture was written to `/tmp/collect.txt` (platform temp, not part of the pack).
- Specialist reports (`reports/design/*.md`) were treated as historical evidence; where the
  v1→v2 ledger still contains the false `api_key_env` claim, that is preserved by policy
  (prior findings are not rewritten) and is corrected in the v2→v3 ledger and §4.7 instead.

## 11. Exactly what Mick must decide

1. **HSF-1 (required before any paid-approval UI):** authorize branch **A** —
   operator-entered *currently advertised* rates with recorded source/time/route and the
   fixed conservative bound basis — **or** explicitly waive/supersede OPv1 §4's dollar bound
   to authorize branch **D** (recorded as a waiver, never as an equivalent branch).
2. **HSF-2:** filesystem-only restart behavior (**2a**, recommended, no fence change) vs
   app-DB pointer with migration (**2b**, expands the fence and crosses Phase 9 migration
   authority). Must be decided before parcel-v2 freezes the fence.
3. **HSF-3:** explicit path/id only (**3a**, recommended) vs read-only run browsing
   (**3b**, small fence addition). May accept the recommendation.
4. **HSF-4:** add `RunState.CANCELLED` (**4a**, recommended) vs reuse `FAILED` +
   `error_type="cancelled"` (**4b**). Gates T0.6/T0.8.
5. **HSF-5:** confirm provider route locked (**5a**, default/recommended) vs explicit
   provider switch (**5b**). May confirm.
6. **Acceptance posture on this round's residuals:** accept the sealed v3 design with
   N-1…N-4 recorded as known errata to be corrected inside the implementation candidate's
   own sealed evidence, **or** require one further design micro-reseal (v4) fixing N-1
   (fence note), N-2 (obligation-13 cell), N-3 (skipped-synthesis display row) and N-4
   (crash-window wording/display row) before signing acceptance. All four are
   design-evidence edits only — no fence change, no scope change.
7. **Gate decision:** grant implementation authority and instruct creation of immutable
   parcel-v2 + retained continuation, or route this round for one further independent
   falsification pass first (MiMo design-oracle budget: 2 of 3 launches used).

Nothing in this report requires scope expansion, a dependency change, or a new human fork
beyond HSF-1…HSF-5.
