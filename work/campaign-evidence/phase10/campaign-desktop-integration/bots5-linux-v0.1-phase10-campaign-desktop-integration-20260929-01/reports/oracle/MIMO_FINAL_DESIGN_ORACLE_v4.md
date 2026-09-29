# Fresh MiMo final Phase 10 design oracle (seal v4) — third and last budgeted design round

## 0. Recomputed seal identity and seal-integrity statement

- Recomputed `sha256(seals/design/DESIGN_SEAL_v4.json)` =
  **`9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1`** — matches the
  expected identity exactly. The adjacent `seals/design/DESIGN_SEAL_v4.sha256` records the
  same value with the same filename (`9739e87c…d9c1  DESIGN_SEAL_v4.json`).
- **Every seal-listed entry was independently rehashed with byte counts: 25/25 match,
  0 missing, 0 mismatched** — 12 `design_artifacts`, 10 `referenced_inputs` (including all
  three repair records), 2 `oracle_inputs` (both prior MiMo oracles, sha
  `8dfab3b4…cb21c` / 34247 bytes and `8c1435af…8a6c` / 38509 bytes, matching the files on
  disk), 1 `review_input` (GPT-6 Luna falsification, `16524cfa…b8fe`, 22155 bytes).
- **Chain intact and preserved:** recomputed `sha256(DESIGN_SEAL_v1.json)` =
  `7ecbb502bd65dcc2ad1595b1dd709db3eff0bc5d5e079b00f8e77c29ee6d952f`,
  `sha256(DESIGN_SEAL_v2.json)` =
  `2a57410dc1da155aab7fcbf480a244b999eac3525a9c7737e40adc703fdee0e4`,
  `sha256(DESIGN_SEAL_v3.json)` =
  `b08d85744065ab6d1b41e4f83912f4f194bc025725f6d95ab4c7b959874f1ded` — all three equal the
  expected pinned values, all three sidecars agree. v4 declares
  `supersedes = {DESIGN_SEAL_v3.json, b08d8574…f1ded}`
  (`DESIGN_SEAL_v4.json:162-165`), lists all three predecessors in `seal_chain` (`:146-159`)
  and all three repair records (`:134-138`). v3 in turn declared supersession of v2, v2 of v1.
- **Coverage of the repaired bytes:** diffing the v3 seal map against the v4 seal map shows
  exactly 7 design artifacts changed — `CAMPAIGN_EVIDENCE_EVOLUTION`, `DESKTOP_SURFACE_AND_
  LIFECYCLE`, `MUTATION_FENCE.json`, `PHASE10_CAMPAIGN_DESKTOP_DESIGN`,
  `PREFLIGHT_APPROVAL_STATE_MACHINE`, `REGENERATION_AND_STALE_SYNTHESIS`, `VALIDATION_PLAN` —
  plus the new `DESIGN_REPAIR_RECORD_v3_to_v4.md` and the new `oracle_inputs` entry. That set
  is exactly the union of the locations the v3→v4 ledger claims to have repaired (N-1 fence,
  N-2 main design, N-3 regen/lifecycle/validation, N-4 lifecycle/validation, N-5/N-6 state
  machine + evidence evolution). The two prior oracle reports, the falsification and the
  v1→v2 / v2→v3 ledgers are byte-identical between the v3 and v4 seal maps (preserved
  unedited). Nothing outside the design-evidence area changed.
- **Baseline identity:** live `git rev-parse HEAD` =
  `0756904481ae884bb9e864e8e1e11fc4a27a72ff`, live tree =
  `7fe879035a01a87339dfe6319a435fa0e84c94bd` — equal to the seal pins
  (`DESIGN_SEAL_v4.json:2-3`), to `MUTATION_FENCE.json:4-6` and to `parcel/BASELINE.json`.
  `git status --untracked-files=no` is empty (tracked tree clean before and after this
  review; only untracked workspace material `work/`, audit temps exist, preserved).
  `orgmem_ref` `cc0c3c80…` matches `BASELINE.json` (internal consistency only — OrgMem was
  not fetched; §10). Seal `created_at` `2026-09-28T21:52:06Z` postdates every covered
  artifact's mtime.

**Seal integrity: PASS.** No blocking seal/parcel/baseline/chain finding.

## Overall verdict

**PASS_WITH_LIMITATIONS.**

Seal integrity passes outright. All six prior-oracle residuals N-1…N-6 are genuinely
repaired in the sealed v4 bytes (§2), each repair is real, located where the ledger says,
and none left an unconditional echo of the superseded claim anywhere in `design/`. CONTRACT
obligations 1–13 each retain a sufficient mechanism (§4); there is no forbidden scope, no
fence expansion, no dependency change, no second truth, no resume/retry leakage, no
fabricated cost (§4–§6); implementability inside the unchanged 9-modify / 8-add fence holds
against the live pinned code, and every named source fact checked this round is true (§5);
the staleness predicate is mechanically correct including the newly-gated skipped-synthesis
case (§6); validation remains non-vacuous and provable (22/22 reused selectors exact-def
verified, collect-only = 115, exit 0) (§7); HSF-1…HSF-5 are genuine forks, correctly framed,
with HSF-1's OPv1 dollar-bound question stated honestly in all three places that discuss it
(§8).

Limitations: four **non-blocking** residual findings this round (O-1…O-4, §3) — two Low
documentation/specification inconsistencies left at the edges of the N-3/N-4 repairs, two
pre-existing or informational naming imprecisions. None undermines an obligation mechanism,
none makes anything unimplementable, none can produce a false success, false zero-cost,
silent retry or evidence loss. **Nothing in this oracle blocks Mick's design acceptance.**
The design is acceptable pending only Mick's own decisions (§9, §11): the five human forks
HSF-1…HSF-5, the acceptance posture on this round's residuals, the implementation gate, and
one campaign-budget consequence of the three-round design-oracle sequence (§11 item 7).
HSF-1 still blocks *paid-approval UI*, not design acceptance.

This is a design-evidence finding only. No product code was changed, no test was executed
beyond read-only `--collect-only` probes, no provider/API call was made.

---

## 1. Review identity, method, evidence boundary

- Model: `xiaomi/mimo-v2.6-flash`. Requested effort `provider_maximum_if_exposed`; the
  harness exposes no per-child effort parameter, so **effective effort: not exposed** (no
  silent substitution).
- Read-only exact-seal review: seal chain (v1–v4 + all four sidecars), all 25 seal-listed
  entries, all 12 sealed design artifacts, all three repair records, both prior MiMo oracles,
  the GPT-6 Luna falsification, parcel authority (`CONTRACT.md`, `SCOPE.json`, `BASELINE.json`,
  `BUDGET.json`, `FINDINGS.md`, `VALIDATION.md`, `SOURCES.md`), `MODEL_ROUTING_EVIDENCE.md`.
- Live pinned source verified byte-level this round: `runner.py`, `storage.py`, `models.py`,
  `events.py`, `usage.py`, `cli.py`, `paths.py`, `manifest.py`, `rendering.py`,
  `providers/openrouter.py`, `providers/openai_compatible.py`, `tests/conftest.py`,
  `tests/helpers.py`, `tests/test_runner.py`, `docs/OPERATING_PROCEDURE_V1.md`,
  `docs/DEVELOPMENT.md`, `desktop/bridge.py`, `desktop/window.py`, `bootstrap/desktop.py`,
  migration head `0012_phase9_archive_import`, `git ls-files evidence` (76 files).
- Zero-provider probes (repository `.venv`, socket-blocked by `tests/conftest.py`):
  `pytest --collect-only -q` over the plan's 9-file baseline campaign selection; exact `def`
  match of all 22 reused selectors.
- Not done: no test execution, no network, no dependency installation, no mutation of
  repository/parcel/design/seal; the only file written under the campaign pack is this
  report. `git status --untracked-files=no` empty after probing confirms zero tracked delta.

## 2. Prior residuals N-1…N-6: are the repairs real?

Verified against the **sealed v4 bytes** (hashes confirmed in §0), not against the ledger.

### N-1 — fence no longer claims `api_key_env` on `OpenRouterProvider` — **REPAIRED**

- `design/MUTATION_FENCE.json:173` (notes) now reads: validation "reads the exposed
  `base_url` (and `api_key_env` only where a provider exposes it, i.e.
  `OpenAICompatibleProvider`) without changing provider semantics; `OpenRouterProvider`
  exposes only `base_url`".
- Verified true of the pinned bytes: `openrouter.py` has **zero** occurrences of
  `api_key_env` (re-counted this round); its only public route property is `base_url`
  (`openrouter.py:61-62`); `openai_compatible.py:78` `base_url`, `:82` `api_key_env`.
- Pack-wide grep: the only remaining "existing public properties" / unconditional-`api_key_env`
  claims live in the **preserved historical** `DESIGN_REPAIR_RECORD_v1_to_v2.md:43-46` and in
  the two prior oracle reports quoting the defect — correct by the no-rewriting-of-prior-
  findings policy (v2→v3 ledger `:51` explicitly supersedes it; v3→v4 `:19-25` fixes the last
  live echo). No live `design/` text carries the false claim.
- Consequence: `MUTATION_FENCE.json` is now consistent with `PREFLIGHT_APPROVAL_STATE_MACHINE.md`
  §4.7 (`:177-200`) and with the zero `providers/**` diff guard.

### N-2 — main design row 13 names both consent channels — **REPAIRED**

- `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:67` (row 13): new verbs are "both preflight-only unless
  `--approve --actor <label>` **or `--approval <path>`** is given".
- Four-way agreement verified at line level: main design `:67`;
  `CAMPAIGN_EVIDENCE_EVOLUTION.md:210-215` (§6.4, both channels, refusal without consent);
  `MUTATION_FENCE.json:68` ("`--approve --actor/--approval`"); `IMPLEMENTATION_SEQUENCE.md:26-28`
  (M0.4). The v3→v4 ledger correctly records that the v2→v3 ledger's "the four artifacts now
  agree" claim is superseded, and the v2→v3 ledger itself is preserved unedited (hash unchanged
  between seal v3 and v4) — right policy.

### N-3 — skipped / never-dispatched synthesis is NOT_APPLICABLE — **REPAIRED (two of three
locations fully; §4.2's own lead sentence not updated — see O-1)**

- `REGENERATION_AND_STALE_SYNTHESIS.md:183` (§4.2 table) adds the row: "v2 attempt in a
  non-dispatched terminal state (`SKIPPED`: dependency failed/incomplete, known-cost threshold
  exceeded, not reached before timeout) → **NOT_APPLICABLE** → show the recorded skip reason;
  synthesis never ran, so provenance is not applicable — **no integrity warning**".
- The old blanket row is now dispatch-gated: `:184` "v2 attempt, **dispatched**, provenance
  absent or malformed → UNVERIFIABLE"; rationale `:190-195` states a skipped synthesis
  (`StageState.SKIPPED`) was never dispatched, therefore never had provenance (M-2's recording
  point is dispatch), citing `runner.py:347-361, :366-380, :385-399, :449` — **all four
  citation windows independently verified true against the pinned runner** (all four set
  `StageState.SKIPPED` with no provenance fields).
- `DESKTOP_SURFACE_AND_LIFECYCLE.md:160` (§4.1) adds the matching display row: skipped /
  never dispatched → show the recorded skip reason, provenance not applicable; `:161` now says
  "provenance absent/malformed on a v2 run **and dispatched** → unverifiable".
- `VALIDATION_PLAN.md:86` adds gate **T0.12** with selector
  `test_phase10_evidence_regeneration.py::test_skipped_synthesis_provenance_absence_is_not_reported_as_unverifiable_or_stale`
  — in a fenced `add` file.
- Residual: the section's heading/lead (`:173-176`) and the helper description in
  `CAMPAIGN_EVIDENCE_EVOLUTION.md:199-204` were not updated — **O-1** below.

### N-4 — honest hard-kill limit; sibling keeps its own cancellation cause — **REPAIRED (both parts)**

- Part (a), `DESKTOP_SURFACE_AND_LIFECYCLE.md:218-224` ("Honest limit (N-4)"): the guarantee is
  explicitly scoped to **in-process**; a hard kill (SIGKILL, power loss) inside the window
  between the stage handler's write and outer terminalization "can leave
  `error_type = "cancelled_pending"` on disk"; the reader treats it as **interrupted /
  uncertain** — never a normal outcome, never success, never auto-retried; "No startup repair
  rewrites it". Display row added at `:163`. Selector added at `VALIDATION_PLAN.md:80`
  (`::test_durable_cancelled_pending_after_hard_kill_reads_as_interrupted_uncertain_and_is_not_retried`),
  and the old in-process claim became `::test_cancelled_pending_is_reclassified_in_process_and_never_written_as_a_final_state`.
- Part (b), `DESKTOP_SURFACE_AND_LIFECYCLE.md:206-216`: the generic-failure sweep now
  reclassifies a `cancelled_pending` **sibling** to `error_type = "cancelled"` with its own
  cancellation message, "rather than inheriting the run-level cause; the run itself remains
  `FAILED` with `internal_error`" — the sibling's own cause is preserved.
- Mechanism sanity against live code re-verified this round: the generic handler cancels and
  **awaits** worker tasks (`runner.py:466-470`) before `_raise_after_best_effort_failure`
  (`:471`), and `_best_effort_internal_failure` (`:56-98`) sweeps `QUEUED`/`RUNNING` only
  today — so the specified extension point and sequencing are exactly as the design claims.
- Residuals: no selector pins the sibling's `cancelled` value (**O-2**); the operator-cancel
  branch's stage label under HSF-4 option 4a is left as "the HSF-4 cancellation error type"
  (**O-3**).

### N-5 — exclusive-create scope clarified — **REPAIRED**

- `PREFLIGHT_APPROVAL_STATE_MACHINE.md:154-157` (§3.1 point 3): "**Exclusive-create scope
  (N-5 clarification).** Exclusive-create governs **creating a new attempt** (and the run tree,
  and the approval marker). The ordinary lifecycle of that same attempt — `queued` →
  `running` → terminal — is a normal in-place state update of the file just created, and is
  not forbidden by it." This removes the misreading flagged in N-5 while keeping the replay
  guard (`:148-153`) intact and consistent with `REGENERATION_AND_STALE_SYNTHESIS.md:67-69`.

### N-6 — `api_key_env_name` origin named — **REPAIRED**

- `PREFLIGHT_APPROVAL_STATE_MACHINE.md:72-75` (§2.1): for `openrouter` the value is the CLI
  convention constant `OPENROUTER_API_KEY` (`cli.py:92-94`), because the manifest declares
  provider config only for `local_openai` (`manifest.py:55`,
  `PROVIDER_CONFIG_KEYS = {"local_openai"}`); for `local_openai` it comes from the job's
  provider config.
- Both cited facts verified: `manifest.py:55` is exactly `PROVIDER_CONFIG_KEYS = {"local_openai"}`
  (second use at `:192` closes `providers` to that key); the openrouter env constant appears in
  `cli.py:91` and `:93` (cited window `92-94` includes the `:93` literal and the
  `OpenRouterProvider(api_key)` construction at `:94`; the env read itself is one line lower at
  `:91` — a lower-bound imprecision of one line, not a false claim). Revision note updated at
  `CAMPAIGN_EVIDENCE_EVOLUTION.md:10-13`.

**No new contradiction of blocking weight was introduced by the repairs.** Four low-level
residuals were found — see §3.

## 3. New / residual findings (this round)

### O-1 — §4.2's heading/lead and the §6.3 helper description were not updated for NOT_APPLICABLE

- **Location:** `REGENERATION_AND_STALE_SYNTHESIS.md:173-176`: heading still reads "Three
  outcomes, never a fabricated 'current' (F-06 repair)" and the lead still reads "The
  predicate yields exactly one of **FRESH**, **STALE**, or **UNVERIFIABLE**" — while the table
  below (N-3 repair, `:183`) now yields a fourth classification, **NOT_APPLICABLE**, and the
  §4 predicate definition (`:152-159`) still says "Otherwise `S_M` is **MECHANICALLY STALE**"
  with no dispatch-state gate before it (a skipped attempt with empty provenance satisfies the
  literal "otherwise → STALE"). Same class: `CAMPAIGN_EVIDENCE_EVOLUTION.md:199-204` (§6.3),
  one of the three locations the N-3 finding named, still describes the helper's output as
  unconditional "per-stage staleness of the selected synthesis" with no state gate or outcome
  vocabulary.
- **Reading that resolves it:** the §4.2 table is authoritative and the dispatch-state gate
  applies before the predicate — exactly how the pre-existing UNVERIFIABLE/LEGACY rows have
  always been read. So the mechanism is unambiguous in practice and no obligation is violated
  either way; the defect is that the section's own summary sentence contradicts its table.
- **Obligation:** CONTRACT §"Lifecycle truth" (truthful outcomes) and item 11; F-14/F-06.
- **Severity:** Low (documentation consistency; display semantics themselves are correct and
  gated by `DESKTOP…:160-161` and by T0.12).
- **Mechanically repairable:** yes — one sentence ("a non-dispatched attempt is classified
  NOT_APPLICABLE before the predicate is evaluated"), retitle the heading, add one clause to
  §6.3; design-evidence only, then reseal.
- **Blocks Mick's design acceptance:** no.

### O-2 — no selector pins the N-4(b) sibling cause value

- **Location:** `VALIDATION_PLAN.md:80` (T0.6). The N-4(b) repair's substance is that the
  generic-failure sweep relabels the sibling to `error_type = "cancelled"` (not
  `internal_error`, per the superseded v2→v3 approach at
  `DESIGN_REPAIR_RECORD_v2_to_v3.md:58-64`). The split selectors cover "reclassified in
  process / never written as a final state" and the durable-after-hard-kill display, but no
  selector name or assertion pins **which value** the sibling keeps, so a test could pass while
  asserting only `!= cancelled_pending`.
- **Obligation:** validation provability for the N-4 repair; CONTRACT anti-vacuity rules;
  obligation 7 (truthful typed outcomes).
- **Severity:** Low (the spec text at `DESKTOP…:210-214` is unambiguous; only the proof
  selector is loose).
- **Mechanically repairable:** yes — extend/rename one T0.6 selector to assert the sibling's
  `error_type == "cancelled"` and the run's `FAILED`+`internal_error`; design-evidence only.
- **Blocks acceptance:** no.

### O-3 (informational) — stage-level cancellation label under HSF-4 option 4a is not named

- **Location:** `DESKTOP_SURFACE_AND_LIFECYCLE.md:196`: the operator-cancel branch
  "reclassifies every `cancelled_pending` record to **the HSF-4 cancellation error type**",
  while the generic path (`:210`) fixes the stage label to `error_type = "cancelled"`
  unconditionally. Under HSF-4 option 4b those coincide; under option **4a** (`RunState.CANCELLED`,
  `HUMAN_SEMANTIC_FORK_REGISTER.md:114`) the fork defines a run-level state only, so the
  stage-level string the operator-cancel branch writes is left undefined.
- **Obligation:** CONTRACT §"Lifecycle truth"; HSF-4 framing ("either branch inside the fence",
  `DESKTOP…:231-232`).
- **Severity:** Informational. Both branches remain conservative (never a false success, never
  auto-retry); the generic path and §4.1 already fix `cancelled` at stage level.
- **Mechanically repairable:** yes — one sentence: the stage-level label is `cancelled` under
  both branches; HSF-4 governs the run-level state only.
- **Blocks acceptance:** no; may alternatively be settled when Mick adjudicates HSF-4.

### O-4 (informational, pre-existing since v2) — snapshot field named two ways

- **Location:** `PREFLIGHT_APPROVAL_STATE_MACHINE.md:71` names the frozen-route field
  `api_key_source`; the sealed `preflight.json` example
  `CAMPAIGN_EVIDENCE_EVOLUTION.md:98-100` uses `key_source` (and the v2→v3 ledger's route-block
  summary at `DESIGN_REPAIR_RECORD_v2_to_v3.md:52` says `key_source`). Same field, two names.
- **Obligation:** CONTRACT §"Preflight / approval integrity" (digest-input determinacy).
- **Severity:** Informational — both artifacts describe one snapshot produced by one
  serializer, so a single implementation cannot actually diverge; only the prose/schema example
  disagree. Not introduced by the v3→v4 repairs.
- **Mechanically repairable:** yes — align on one name at implementation (or in a micro-reseal).
- **Blocks acceptance:** no.

**Precision footnote (not a defect):** the `OPENROUTER_API_KEY` literal is at `cli.py:91`
(env read) and `:93` (error message); the N-6 citation window `92-94` includes `:93` and the
provider construction at `:94`.

## 4. Authority fidelity — CONTRACT obligations 1–13 and forbidden scope

Verified against live bytes this round; each mechanism exists or is inside the fence.

| # | Mechanism (sealed design) | Verified against live code |
|---|---|---|
| 1 | `manifest.load_job` + `validate_referenced_files` via bridge, explicit path (HSF-3) | validator pair side-effect-free; `locate_run_dir` requires an id (`paths.py:52`) |
| 2 | same validator pair as `bots5 validate`; no provider, no runs dir | `_cmd_validate` = load + validate + print only (`cli.py:73-77`); T0.1 |
| 3 | `PreflightSnapshot` in memory; `preflight.json` only on approved start | `PREFLIGHT…:41-59,204-206`; assertions run before `create_run_tree` (`runner.py:259→271`) |
| 4 | identity/scope/one-shot/disk/config/selection/route assertions before first `provider.complete`; refusal creates nothing | `PREFLIGHT…:161-206`; one-shot marker `:137-159`; `create_run_tree` refuses existing id (`storage.py:109-110`) |
| 5 | bounded polling 250/1000 ms over `run.json`/`stages/*`/`usage.json`; known subtotal + explicit unknown | `DESKTOP…:95-106`; `usage.py:9-69` shapes real; no fabricated cost anywhere |
| 6 | attempt-aware readers + `result.md` mirror; CLI `inspect --attempt` | `CAMPAIGN_EVIDENCE…:184-217`; fence contract "v1 outputs and exit codes unchanged" (`MUTATION_FENCE.json:69`) |
| 7 | projection maps durable states exactly; hosted-less `running` = interrupted/uncertain; durable `cancelled_pending` = interrupted; no resume path exists | `DESKTOP…:145-163`; grep finds no resume code in campaign modules |
| 8 | selection durable on disk; `New Job` clears context only; restart persistence = HSF-2 | `selection.json` spec + HSF-2 register; D-8 |
| 9 | flat `<id>.att<N>` append-only, exclusive-create, explicit model change, route locked, never automatic | `REGENERATION…:15-92`; containment predicate unchanged (`storage.py:196-198`); v1-refusal precondition 0 (`:39-44`, `:214-216`) |
| 10 | pure staleness predicate over durable evidence; initial-run provenance recorded at dispatch | `REGENERATION…:110-168`; `.md` bytes = rendered bytes (`storage.py:119-125` bare text vs `rendering.py:11-15` wrappers) so digests compare like with like; NOT_APPLICABLE gate added (N-3) |
| 11 | `rerun_synthesis` explicit; new attempt; selection only on success; earlier evidence untouched | `REGENERATION…:204-245`; cost gate uses derived selected cost (F-07 derivation `CAMPAIGN_EVIDENCE…:148-167`) |
| 12 | `evidence_version` marker; v1 readers byte-identical; golden fixtures gate-first | `IMPLEMENTATION_SEQUENCE…:13-17`; 76 tracked `evidence/**` files re-counted |
| 13 | CLI verbs/exit codes unchanged; new parity verbs incl. `regenerate`; `core/application.py` zero-diff (2775 lines) | fence `unchanged_guards` ✓; consent form now four-way consistent (N-2 repaired) |

**Forbidden scope:** every CONTRACT exclusion is explicitly excluded in
`PHASE10_CAMPAIGN_DESKTOP_DESIGN.md:172-178` and `MUTATION_FENCE.json:155-166`
(`forbidden_expansions`). Pack-wide greps for streaming/daemon/authoring/registry/pricing
return only negations or zero hits (no pricing code anywhere in `src/bots5` — HSF-1 premise
true). No dependency change, no manifest schema change (so no `JOB_SPEC.md` obligation —
`docs/DEVELOPMENT.md:110-113` confirmed), no pricing registry, no provider streaming, no
app-DB campaign persistence, no enumeration seam (campaign code has none; only
`infrastructure/*` uses `listdir/scandir`), no migration unless HSF-2 2b, no Phase 11/12.
Fence is exactly 9 modify + 8 add (`MUTATION_FENCE.json:19-141`) and is sufficient for all M-,
N- and this round's O-repairs (design-evidence edits, or already-fenced `runner.py`/test files).

## 5. Implementability against the live code; named source facts

All design citations checked this round (on top of the two prior oracles' verified sets):

- `cli.py:73-77` `_cmd_validate` = load + validate only ✓; `cli.py:91/93/94` openrouter key
  convention and construction ✓ (N-6).
- `manifest.py:55` `PROVIDER_CONFIG_KEYS = {"local_openai"}` ✓; `PROVIDERS_V2 = {openrouter,
  local_openai}` ✓ (`:54`).
- `providers/openrouter.py:61-62` `base_url` only, zero `api_key_env` occurrences ✓;
  `providers/openai_compatible.py:77-83` exposes both properties (`:78` / `:82`) ✓ — the whole
  M-3/N-1 repair rests on facts true of the pinned bytes.
- `runner.py:201-215` stage `except asyncio.CancelledError` marker point ✓; `:231-250`
  provider mapping validated for presence + callable `complete` only ✓ (F-03 premise);
  `:253` `run_job`, `:259` validation, `:264-267` prompt/input reads, `:271` `create_run_tree`
  ✓ (TOCTOU ordering and "refusal creates no run dir" real); `:294` initial-persist failure
  path; `:344-399` three synthesis SKIPPED gates (`:347-361`, `:366-380`, `:385-399`) and
  `:449` not-reached-before-timeout SKIPPED ✓ (N-3 basis); `:404-415` synthesis input build
  (`:405` render, `:410` user_message) ✓ (M-2 basis); `:428` `except TimeoutError`; `:465`
  `except Exception` (catches `Exception`, not `CancelledError` — HSF-4 premise true);
  `:466-470` cancel+await before sweep; `:471` sweep call; `:56-98`
  `_best_effort_internal_failure` sweeps `QUEUED`/`RUNNING` only ✓; `:307, :331-333, :404`
  in-memory outputs/id-equality ✓.
- `storage.py:94-111` `create_run_tree`, refusal at `:109-110` ✓; `:119-125` `persist_stage`
  writes the bare output text to `.md` ✓ (digest/byte consistency for the §4 predicate);
  `:175` `load_run_view`, `:188` `load_stage_view` (today stage-id-only, no attempt arg — the
  fence's additive work); containment predicate at `:196-198` ✓.
- `events.py:13-24` exactly 11 `EVENT_TYPES`; unknown event raises at `:41-42` ✓ (additive
  kinds in fence stay fail-closed).
- `models.py:10-14` `RunState = running|succeeded|failed|timed_out` (no `CANCELLED` — HSF-4
  premise); `:110` `provider_side_outcome_unknown` default `False`; no attempt/provenance
  fields exist yet (confirmed absent — the fence's additive work).
- `paths.py:10` `SAFE_ID_RE` permits `.`/`-` ✓; `:52` `locate_run_dir` requires an id ✓.
- `rendering.py:11-15` synthesis message wraps raw worker texts ✓; `usage.py:9-69`
  `aggregate_cost`/`usage_document` with `stages`/`aggregate` keys ✓ (F-07 derivation target).
- `errors.py` has no `ApprovalInvalidatedError`/`definitive_rejection` yet (fence adds them) ✓;
  `desktop/bridge.py:42-47` existing single-flight task guard ✓ (the "single-flight/loop-pinned
  discipline" the bridge reuses exists); `bootstrap/desktop.py:275` `_close_driver` ✓;
  `window.py` dock composition seam (import-queue dock attached at `:337-360`) ✓.
- `docs/OPERATING_PROCEDURE_V1.md:62-75` genuinely requires both the highest applicable
  currently advertised input/output rates **and** a conservative upper bound from prompt size,
  stage count and output ceilings ✓ (HSF-1 premise true); `:115` result.md non-proof line ✓;
  `:123` runs-dir/cwd asymmetry ✓. Migration head `0012_phase9_archive_import` exists ✓.
- `tests/conftest.py:13-19` autouse socket block + `OPENROUTER_API_KEY` deletion ✓;
  `tests/helpers.py:31` `class FakeProvider` ✓; `tests/test_runner.py:373-400` proves no
  secret is persisted ✓ (cited by `CAMPAIGN_EVIDENCE…:114`).
- Add-list targets `src/bots5/core/campaign.py`, `src/bots5/desktop/campaign_dock.py` and
  `tests/test_phase10_*` do not exist yet — consistent with rule 0.2 and with a genuinely
  additive fence.

**Fence conclusion:** implementable inside 9 modify / 8 add against the live code, with zero
`providers/**` diff; every repair across all three waves stays inside already-fenced files or
is design-evidence text. No named source fact found false this round.

## 6. Lifecycle / evidence semantics, staleness, old-run readability, truth

- **Append-only preservation:** attempts are new files with exclusive-create (creation only —
  N-5 clarified); `persist_stage` legacy signature retained; `os.replace` targets only new
  names; `evidence/**` untouched (76 tracked files); v1 directories read-only for
  regeneration/rerun (M-6 preconditions present at `REGENERATION…:39-44` and `:214-216`).
- **Staleness predicate correctness:** pure function of `selection.json` + recorded provenance
  + current output digests; `.md` bytes equal rendered bytes (verified §5), so digests compare
  like with like; absent selection ⇒ attempt 1 default is consistent across reader
  (`CAMPAIGN_EVIDENCE…:60-67`), predicate (`REGENERATION…:152-157`) and the M-2 dispatch
  recording rule (`:119-124`); bidirectional by construction (`:162-171`); no UI/DB
  participates. The skipped/not-dispatched case is now classified NOT_APPLICABLE before any
  integrity warning (N-3), with T0.12 proving it — residual wording gap **O-1** only.
- **Old-run readability:** marker-selected readers, byte-identical v1 path, gate-first golden
  fixtures (`IMPLEMENTATION_SEQUENCE…:13-17`), T2.4 mandatory on schema change; readers reject
  unknown future versions (`CAMPAIGN_EVIDENCE…:232-237`).
- **No second truth:** filesystem authoritative; no event bus, no SQLite campaign table; poller
  read-only (`DESKTOP…:95-106`); events narrative-only (`events.jsonl` has no programmatic
  reader today); derived `selected_spend` beats a stale cache with an integrity warning
  (`CAMPAIGN_EVIDENCE…:156-164`) — no permanent contradiction possible.
- **Truthful uncertainty:** D-4 (`running` + `started_at` ⇒ unknown), D-9 ambiguous-vs-
  definitive classification with fail-safe default unknown and `errors.py`-only change,
  interrupted/uncertain for hosted-less `running`, interrupted/uncertain for durable
  `cancelled_pending` (N-4a), never "did nothing".
- **No resume/retry leakage:** no resume code path exists (grep); forbidden-operations list is
  fail-closed (`REGENERATION…:258-267`); regeneration/rerun each require fresh validation +
  snapshot + approval; one-shot consumption is engine-side (`PREFLIGHT…:137-159`); rerun never
  triggered automatically (`REGENERATION…:244-245`).
- **Shutdown:** engine-side terminalization before event-loop teardown; all three outer paths
  terminalize (`runner.py:294`, `:428-464`, `:465-471` — sequencing re-verified); close driver
  verifies terminality from disk and displays interrupted if the write failed; OS kill remains
  truthfully `running`; hard-kill `cancelled_pending` now has an honest limit and a display row
  (N-4a).

## 7. Validation non-vacuity and provability

- **Reused selectors: 22/22 verified present** by exact `def` match in the live tree (test_runner
  11, test_cli_views 5, test_desktop_draft1 1, test_phase4 1, test_phase9_desktop_slice_e 3,
  test_phase1_core 1), matching `VALIDATION_PLAN.md:36-59`.
- **Zero-provider collect probe:** `pytest --collect-only -q` over the plan's baseline campaign
  selection (9 files) collected **115** tests (per-file 2+6+21+24+7+22+2+29+2 = 115), exit 0 —
  matching `VALIDATION_PLAN.md:61-64`. Collection is non-vacuous; the historical "115 passed"
  figure was **not** re-executed (evidence limitation, §10).
- **Repair selectors present and named in fenced files:** M-2 (`:79`), M-3 swap test (`:78`),
  M-4/N-4 T0.6 five-selector split (`:80`), M-5 T0.11 (`:82`), M-6 v1-refusal pair (`:78-79`),
  N-3 T0.12 (`:86`) — all in `tests/test_phase10_*.py` files in the fence `add` list (verified:
  no `test_phase10_*` file exists yet, consistent with rule 0.2).
- **Gap:** no selector pins the sibling `cancelled` label — **O-2** (Low).
- **Anti-vacuity rules** 0.1–0.6 (collected-count proof, selector validity only after
  implementation, no count-scraping reruns, serial Qt/T4, fakes + socket block + key deletion)
  are stated and consistent with `parcel/VALIDATION.md` and the CONTRACT anti-vacuity rules;
  fakes/socket-block/key-deletion verified real in the live tree.
- **Candidate-seal governance** (seal before review, final T4 on exact seal, post-T4 byte
  verification, unsealed review = historical only) matches `VALIDATION_PLAN.md:150-161` and
  `parcel/VALIDATION.md:83-107`.
- Design-phase honesty: `VALIDATION_PLAN.md:163-166` states no gate has been run and no listed
  test file exists — truthful.

## 8. Human semantic forks HSF-1…HSF-5

- **HSF-1 pricing authority — genuine, correctly framed in all three places; the OPv1
  dollar-bound question is stated honestly.** Verified: no pricing code anywhere in `src/bots5`
  (grep zero hits); OPv1 §4 (`docs/OPERATING_PROCEDURE_V1.md:62-75`) genuinely requires both
  the advertised-rate observation **and** a conservative dollar upper bound. Register
  (`HUMAN_SEMANTIC_FORK_REGISTER.md:31-44`), state machine (`PREFLIGHT…:216-232`) and main
  design (`:162`) agree that **A is the only branch compliant by default** and **D does not
  satisfy** the dollar-bound requirement, available only under an explicit recorded Mick
  waiver/supersession — never as an equivalent branch; B/C correctly excluded (fence asset /
  zero-network). Branch A's record shape includes observation time, route, cited rate source
  and fixed bound basis (`PREFLIGHT…:218-222`). Pack-wide grep finds no "acceptable-D"
  framing. Paid approval UI remains gated on Mick. **Mick must adjudicate.**
- **HSF-2 restart persistence — genuine** (F-15). The cited warning is real
  (`parcel/VALIDATION.md:63-65`: durable desktop selection state makes migration affected);
  migration head `0012_phase9_archive_import` exists; 2b expands the fence. Recommended 2a.
  **Mick must adjudicate before parcel-v2 freezes the fence.**
- **HSF-3 run discovery — genuine, low-blocking.** No enumeration seam exists in campaign code
  (verified); `locate_run_dir` requires an id (`paths.py:52`). Recommended 3a. **Mick may
  adjudicate or accept the recommendation.**
- **HSF-4 cancellation vocabulary — genuine.** `RunState` has no `CANCELLED` (verified);
  obligation 7 names four outcomes, not cancellation. Mechanism fixed either way; the branch
  gates T0.6/T0.8. The N-4 repairs hold under either branch (stage label is a string; run state
  forks) — with the minor stage-label wording gap **O-3** that Mick's HSF-4 decision or one
  sentence can settle. **Mick must adjudicate.**
- **HSF-5 provider change — correctly scoped as optional-expansion with the default settled**
  (route locked; model string changeable matches CONTRACT item 9 and F-04).
  **Mick may confirm 5a.**

No additional hidden product fork was found; approval substrate (D-1) and attempt layout (D-2)
remain justified engineering choices under the CONTRACT's own fork criterion and the verified
containment predicate.

## 9. Blocking analysis — what blocks Mick's design acceptance

**Nothing in this oracle blocks design acceptance.**

- Seal/parcel/baseline/chain: PASS (§0).
- N-1…N-6: genuinely repaired in sealed bytes, no unconditional echoes left in `design/` (§2).
- No obligation-mechanism gap, no forbidden scope, no fence expansion, no second truth, no
  resume/retry leakage, no fabricated cost; validation non-vacuous and provable (§4–§7).
- The four residuals O-1…O-4 are **defects**, not evidence limitations: documentation /
  specification inconsistencies of Low (O-1, O-2) or Informational (O-3, O-4) severity, each
  mechanically repairable in design evidence only (or by Mick's own HSF-4 wording), none making
  any mechanism unimplementable and none capable of a false success, false zero-cost, silent
  retry or evidence loss. They are recorded so acceptance cannot read as endorsing them
  silently.
- What *is* blocked without Mick: **paid-approval UI** (HSF-1 until A-vs-waiver-D is decided);
  **full T0.6/T0.8 validation** (HSF-4 until the vocabulary is decided); **parcel-v2 fence
  freeze** (HSF-2 until 2a-vs-2b is decided); and the **MiMo implementation-oracle budget**
  (§11 item 7).

## 10. Evidence limitations (not defects)

- No tests were executed; only read-only `--collect-only` probes (zero-provider,
  socket-blocked). Test pass counts, Qt behavior, and anything about unimplemented Phase 10
  code are **not claimed** by this oracle.
- The historical "115 passed" baseline figure is design-phase evidence; this oracle verified
  **collection = 115**, not execution.
- OrgMem blob pin `cc0c3c80…` was checked for internal consistency only (seal ↔
  `BASELINE.json`); the OrgMem repository was not fetched and no creator-benchmark claim was
  verified (routing evidence only).
- No provider/API calls, no dependency installation, no repository/parcel/design/seal
  mutation; the only file written inside the campaign pack is this report.
- The v1→v2 ledger still contains the false `existing public properties` claim at `:43-46`;
  this is preserved by policy (prior findings are not rewritten) and is superseded by the
  v2→v3 ledger, `PREFLIGHT…:177-196` and the repaired fence note — treated as historical
  evidence, not a live defect.

## 11. Exactly what Mick must decide

1. **HSF-1 (required before any paid-approval UI):** authorize branch **A** — operator-entered
   *currently advertised* rates with recorded source/time/route and the fixed conservative
   bound basis — **or** explicitly waive/supersede OPv1 §4's dollar bound to authorize branch
   **D** (recorded as a waiver, never as an equivalent branch).
2. **HSF-2:** filesystem-only restart behavior (**2a**, recommended, no fence change) vs
   app-DB pointer with migration (**2b**, expands the fence and crosses Phase 9 migration
   authority). Must be decided before parcel-v2 freezes the fence.
3. **HSF-3:** explicit path/id only (**3a**, recommended) vs read-only run browsing
   (**3b**, small fence addition). May accept the recommendation.
4. **HSF-4:** add `RunState.CANCELLED` (**4a**, recommended) vs reuse `FAILED` +
   `error_type="cancelled"` (**4b**). Gates T0.6/T0.8; also settles O-3's wording if so.
5. **HSF-5:** confirm provider route locked (**5a**, default/recommended) vs explicit provider
   switch (**5b**). May confirm.
6. **Acceptance posture on this round's residuals:** accept the sealed v4 design with O-1
   (§4.2 heading/lead + §6.3 helper wording), O-2 (sibling-cause selector), O-3 (stage-label
   wording under 4a) and O-4 (`key_source` naming) recorded as known errata to be corrected
   inside the implementation candidate's own sealed evidence, **or** require one further design
   micro-reseal (v5) fixing them before signing acceptance. All four are design-evidence edits
   only — no fence change, no scope change.
7. **MiMo oracle budget (campaign governance, not a design defect):** this is the third of the
   three authorized MiMo launches (`BUDGET.json` `max_launches: 3` for MiMo V2.6 Flash, whose
   listed roles include the CONTRACT-required *final implementation oracle*). The design-oracle
   sequence now consumes 3/3. Before parcel-v2, Mick must either augment the MiMo launch cap
   for the implementation-phase final oracle, or explicitly re-route that gate to an
   already-authorized family whose listed role covers it, or record acceptance without it —
   per `BUDGET.json` `availability.silent_substitution = false`, this cannot be settled
   silently.
8. **Gate decision:** grant implementation authority and instruct creation of immutable
   parcel-v2 + retained continuation (step 1 of the CONTRACT's implementation phase), or
   route for one further independent pass first — noting the MiMo design-oracle budget is now
   exhausted (3 of 3), so any further *MiMo* design round requires Mick's explicit budget
   change; a fresh GPT-6 Luna falsification round remains within the existing Luna cap
   (1 of 4 used) if Mick prefers a different family for another pass.

Nothing in this report requires scope expansion, a dependency change, or a new human fork
beyond HSF-1…HSF-5. **The design as sealed under v4 is acceptable pending only Mick's own
decisions above.**