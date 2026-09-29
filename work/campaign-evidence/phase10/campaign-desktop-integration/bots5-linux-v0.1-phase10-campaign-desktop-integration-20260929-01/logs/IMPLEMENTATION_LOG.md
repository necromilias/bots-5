# Phase 10 implementation log

Parcel: `parcel-v2` (immutable, manifest `45fd91f3f383620e4da7575c7a5ba92abf9298ed47c72cf7eb30a315c854391b`).
Design of record: seal v4 `9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1` (frozen).
Fence: `design/MUTATION_FENCE.json` (9 modify / 8 add).

## Environment correction (important)

The canonical test environment is **`.venv314`** (Python 3.14.7) run as:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest ...
```

`docs/DEVELOPMENT.md:89` documents this. `.venv` (Python 3.12.13) embeds a private SQLite and
cannot register the rooted VFS, so it produces ~1100 spurious `RootedVfsUnsupported`
failures across phases 1–9. A pristine-HEAD worktree reproduces the same count, so this is a
pre-existing interpreter mismatch, **not** a Phase 10 defect. All Phase 10 validation uses
`.venv314`. Recorded because it materially affects the final T4.

## Launch accounting

| # | Milestone | Model | Result |
|---|---|---|---|
| 12 | M0.0 backward-compat fixtures | `z-ai/glm-5.3-flash` | `tests/test_phase10_backward_compat.py`, 15 tests, pass |
| 13 | M0.1a models | `z-ai/glm-5.3-flash` | additive fields + snapshots + `RunState.CANCELLED` |
| 14 | M0.1b storage | `z-ai/glm-5.3-flash` | attempt/selection/approval persistence, v2 readers, staleness |
| 15 | M0.2 errors/events/usage | `z-ai/glm-5.3-flash` | typed refusal + D-9 classification, 7 event kinds, dual accounting |

## Milestone record

### M0.0 — backward-compatibility fixtures (gate-first)

- Deliverable: `tests/test_phase10_backward_compat.py`
- sha256 `4e2f4f47d00ce8e8fb16e8fafc886d879ceca9917e9a557d8be3e8bea59acee5`
- Collection 15 (non-zero proven); run: 15 passed, exit 0 (`.venv` and `.venv314`).
- Real retained v1 runs from `evidence/**` copied read-only into `tmp_path`; tree sha256
  unchanged after reads (reads mutate nothing).

### M0.1a — additive model layer

- `src/bots5/models.py` sha256 (post-M0.1a) `b37498fdd1d0ebd1a4078f826dbd0200b539afc395cc5ece6473e1f854c4a039`
- `RunState.CANCELLED = "cancelled"` added (HSF-4 4a, run level).
- `StageRecord` gained `attempt_number`, `consumed_dependencies`, `dependency_digests`,
  `preflight_digest` with defaults.
- Frozen `FileSnapshot`, `PreflightSnapshot`, `OperationSnapshot`, `ApprovalRecord` +
  `canonical_json`, each with `to_dict`/`from_dict` and digest computation.

### M0.1b — storage attempt semantics

- `src/bots5/models.py` sha256 `a1eceb6e70f0ac27b70e09f9caa83cefa9d148615a2e331754b8d4ee57309df4`
- `src/bots5/storage.py` sha256 `76062c8cbbd01ccf3cbc8ec8a02a762562b55ce777ef88da7a4e0a230ca45496`
- Conformance correction applied: `to_dict()` now emits `attempt_number` **always**, per the
  sealed design (`CAMPAIGN_EVIDENCE_EVOLUTION.md` §3); the other three stay conditional.
- Added `attempt_paths`, `persist_stage_attempt(create=)`, `read_selection`, `write_selection`,
  `selected_attempt`, `persist_preflight`, `load_preflight`, `consume_approval` (exclusive),
  `approval_consumed`, v2-aware `load_run_view`/`load_stage_view`, and
  `reconstruct_run_state` with the O-1-ordered freshness classification
  (NOT_APPLICABLE → LEGACY_UNVERIFIED → UNVERIFIABLE → FRESH/STALE).
- Containment predicate preserved verbatim; `persist_stage`/`persist_run` signatures and
  behaviour untouched.

### M0.2 — errors, events, usage

- `src/bots5/errors.py` sha256 `1a5ec370a33a894a2cae4b038d6096a508d10ee20e415de12861cfe48c3aaa8e`
- `src/bots5/events.py` sha256 `59d51f54716424e1f0833d41c6651fc260b8ccc12f6400f5a6807eaad2367529`
- `src/bots5/usage.py` sha256 `542e886cb08b44a12f1f24f16dee291c657ae91988a9ce4aebcedabc2170e60a`
- `src/bots5/storage.py` sha256 `7c0ef4eeb99df7b72346ec53345ae231b1fa1031714967ae62550d14d75e9a90`
- `ApprovalInvalidatedError` added and imported directly (temporary fallback removed);
  `ProviderError.definitive_rejection` (default `False`), `ProviderHttpError` property
  (4xx excl. 408/429), `ProviderResponseError` True, `provider_side_outcome_unknown(exc)`.
- 7 additive event kinds; legacy 11 intact; writer still fail-closed.
- `usage_document`/`aggregate_cost` byte-unchanged; added `derive_selected_spend`,
  `selected_cache_is_stale`, `usage_document_v2`, `persist_usage_v2`.

## Verification (supervisor, canonical interpreter)

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest \
  tests/test_phase10_backward_compat.py tests/test_manifest.py tests/test_runner.py \
  tests/test_cli_views.py tests/test_local_provider.py tests/test_audit_blockers.py \
  tests/test_storage_events.py
→ 99 passed, exit 0
```

Focused rooted-VFS check `tests/test_phase7_core_contracts.py` → 6 passed, exit 0.

Tracked tree at this point: `M src/bots5/errors.py`, `M events.py`, `M models.py`,
`M storage.py`, `M usage.py`; untracked new test file only. No other path touched.

### M0.3a — v2 run path, truthful cancellation, synthesis provenance

- `src/bots5/runner.py` sha256 `1e6b1b063cf5b7398f8e5f08789c9243a63fb3eea0d4853ddda92c0761abcc5c`
- `src/bots5/storage.py` sha256 `b12fceb96388021eb46b0f83bb13a1d6f69dad794a03faea8a43a78bafe3898c`
- `run_job(job, providers, *, run_id=None, snapshot=None, approval=None)`. With both omitted the
  legacy path is byte-identical; with both supplied the engine performs assertions (a)–(g)
  before any provider request (digest identity, snapshot self-integrity, scope/target, input and
  contract byte re-verification, provider-object route validation including kind and
  `api_key_env` only where exposed), dispatches frozen messages with zero re-read, writes
  `preflight.json` and `evidence_version: 2`, and persists every attempt via
  `persist_stage_attempt(attempt_number=1, preflight_digest=...)`.
- `_execute_stage` now writes the neutral `cancelled_pending` marker; the overall-timeout branch
  relabels it to `run_timed_out`; a new `except asyncio.CancelledError` branch (before
  `except Exception`) drains tasks, sweeps non-terminal records with
  `provider_side_outcome_unknown = started_at is not None`, relabels to `cancelled`, writes
  `stage_failed`/`run_cancelled`, persists `RunState.CANCELLED`, then re-raises.
  `_best_effort_internal_failure` relabels `cancelled_pending` to `cancelled` while the run
  stays `FAILED`/`internal_error` (Mick O-2).
- Synthesis provenance (`consumed_dependencies`, `dependency_digests`) is set and persisted
  before `request_sent` (M-2).
- Mechanical settlement of one ordering detail: the authoritative `consume_approval` claim
  happens immediately after `create_run_tree` and still before `preflight.json` or any dispatch;
  a *literal* claim before `create_run_tree` is impossible because `consume_approval` creates the
  run directory that `create_run_tree`'s exclusivity then refuses. A spent approval is detected
  before the tree is created, so a replayed approval leaves no run directory and makes no
  provider call. The one-shot property is preserved.

### M0.3b — regeneration and synthesis rerun entry points

- `src/bots5/runner.py` sha256 `d61ee2f93bd6b10bb9f06c954915c7d462a130d0058641d00d24fc0c46cf5bb2`
- `src/bots5/storage.py` sha256 `3c7bd1dbda023ec6c47dc7eb8df8d6db56f634703d5919aad865a051f039b945`
- `regenerate_worker(...)` and `rerun_synthesis(...)` added with the full precondition sets:
  v1 runs are read-only (typed refusal, nothing written, M-6), approval scope/digest/target
  binding, disk-derived attempt numbering that refuses rather than renumbers, provider-route
  lock with only the model allowed to change, derived-selected-cost gate for synthesis, and
  selection/bytes binding that refuses before dispatch when a dependency moved after approval.
  Regeneration never auto-selects. Additive storage helpers: `_stage_record_from_dict`,
  `load_attempt_records`, `next_attempt_number`.

### M0.4 — headless CLI parity

- `src/bots5/cli.py` sha256 `2227757f4da40fee061cb4d4119e448eac982ed1355b150df49c2b3353289056`
- `inspect --attempt N`; version 2 `status` appends attempt and `synthesis_freshness` markers
  (version 1 output byte-identical); new `regenerate` and `rerun-synthesis` verbs implementing
  the M-5 consent form — without consent they build the operation snapshot, print the preflight,
  construct no provider and write nothing; with `--approve --actor`/`--approval` they execute.

### M1.0 — Qt-free campaign bridge

- `src/bots5/core/campaign.py` sha256 `936e05c1c2fa78ef5099597bf070b82614190745d07be5adbc27cad8c7e74d7e`
- `CampaignProjection`/`StageProjection` built only from durable state; `CampaignBridge` with
  zero-spend `load_job`/`validate`, `prepare_*` (writes nothing, constructs no provider),
  `approve_and_*` (providers constructed only here), `select_attempt`, bounded
  `projection`/`projection_async`, `cancel`, `close`, `run_to_completion`, `adopt_run`
  (explicit path or id only, HSF-3). Truthfulness rules D-4/D-5/D-6/D-9 and the N-4
  `cancelled_pending` rule are implemented in the module. Verified to import with no PySide6,
  qasync or desktop module in `sys.modules`.

### M2.0a — desktop campaign dock

- `src/bots5/desktop/campaign_dock.py` sha256 `1576eabdf8b12f9c8149b83f809d8eed544289b49208a53487a92067180b18f1`
- `CampaignDockWidget(parent=None, *, bridge_factory)` presenting load/validate, preflight,
  approve, truthful progress with known subtotal plus explicit unknown set, attempts and
  "Make current", freshness and integrity warnings verbatim, New Job (presentation only),
  cancel, locked-route regeneration, synthesis rerun, 250 ms bounded polling and an async
  drain. Confirmed to contain no direct filesystem or network access and no engine duplication.
- First dispatch (M2.0) returned null from the model and delivered nothing; it left two stray
  directories in the repository root, which were removed. The milestone was re-dispatched
  narrowly to the Qt-specialist route and delivered.

### M2.0b — dock attach and runtime composition

- `src/bots5/desktop/window.py` sha256 `588f4c3fecc6f9321dbb3ba08c3c4ad1f516b7d8383f7cccfd6237388345cb5d`
- `src/bots5/bootstrap/desktop.py` (see the close-stage entry below for its final hash)
- `MainWindow.__init__` gained one keyword-only `campaign_bridge_factory=None`; with `None` no
  dock is created and no behaviour changes. The dock is added hidden to the Bottom area with a
  View toggle, and the window shutdown path drains it.
- Conformance correction by the supervisor: the first attach added the View-menu "Campaign"
  action unconditionally, which changed the View menu even when no factory was supplied. It is
  now created only when the dock exists.
- Fenced obligation completed by the supervisor: `DesktopRuntime` now tracks every bridge it
  composes and `_close_driver` runs a **bounded campaign close stage**
  (`_CAMPAIGN_CLOSE_TIMEOUT_SECONDS = 30.0`) that cancels and drains hosted campaign work to a
  durable terminal record *before* `application.close()` and *before* `authority.release()`.
  The precedence table gains one key at the existing workspace rank; every pre-existing stage
  keeps its original precedence. The stage never retries provider work and never converts an
  uncertain outcome into success.

## Verification (supervisor, canonical interpreter)

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest \
  tests/test_phase10_backward_compat.py tests/test_manifest.py tests/test_runner.py \
  tests/test_cli_views.py tests/test_local_provider.py tests/test_audit_blockers.py \
  tests/test_storage_events.py
→ 99 passed, exit 0
```

Desktop regression set after the attach and after the conformance fix:

```bash
... pytest tests/test_phase1_desktop.py tests/test_phase1_core.py \
          tests/test_phase7_desktop.py tests/test_desktop_draft1.py
→ 31 passed, exit 0
... pytest tests/test_phase9_desktop_slice_e.py ...   # see below
```

Tracked tree now: all nine fenced modify files (`errors.py`, `events.py`, `models.py`,
`storage.py`, `usage.py`, `runner.py`, `cli.py`, `desktop/window.py`,
`bootstrap/desktop.py`); untracked new files `src/bots5/core/campaign.py`,
`src/bots5/desktop/campaign_dock.py` and `tests/test_phase10_backward_compat.py`. No path
outside the fence has been modified.

## T0/T1 test contracts (fenced add files)

All six fenced test files now exist and collect non-zero:

| File | Collected | sha256 |
|---|---|---|
| `test_phase10_backward_compat.py` | 15 | `4e2f4f47d00ce8e8fb16e8fafc886d879ceca9917e9a557d8be3e8bea59acee5` |
| `test_phase10_evidence_regeneration.py` | 12 | `806e116e2e6fd54efa9a03e4af5000ab726aa39ac57b1a3f1204c90942619545` |
| `test_phase10_desktop_preflight.py` | 11 | `af896c6f98e8e02a3eaa0f6f6bcc9319794c9443c9a57adbbd63bb6e90274d7e` |
| `test_phase10_desktop_projection.py` | 10 | `3b8eac75d25b7e2305e93b665b67daf339561a00fbce36f2dd8378d4f7b8b3b9` |
| `test_phase10_desktop_lifecycle.py` | 11 | `e739f3f68edb81372126ccd776480e44fb2288bc7a20ffa48f2a8d2be3314298` |
| `test_phase10_cross_cutting.py` | 12 | `4492e8aaeaf2e8841820c853e7fa7997627d7220c745059a57c8ac2873726e9f` |

Combined: 71 collected, 71 passed, exit 0.

Required selectors are present, including Mick clarification O-1 (a skipped synthesis is
classified `NOT_APPLICABLE` and that evaluation happens before FRESH/STALE, so a skipped
synthesis is never reported fresh or stale) in `test_phase10_cross_cutting.py`, and O-2 (a
cancelled sibling keeps its own cause while the run stays `FAILED` with `internal_error`) in
`test_phase10_evidence_regeneration.py`.

Defect reported by a test author and carried to the repair wave: for the O-2 path the sealed
design `DESKTOP_SURFACE_AND_LIFECYCLE.md` section 5.2 specifies the relabel message
"stage was cancelled while the run failed with an internal error", while
`runner.py` `_best_effort_internal_failure` leaves the neutral
"stage cancelled before terminal classification" in place. The pinned assertions (error type,
run state, uncertainty flag) all hold; the message text is the deviation.

## T3 — impact-based validation

Justification for a broad run: `models.py`, `storage.py` and `errors.py` are imported by
essentially every module in the tree, so the impacted set for this change is the whole
repository rather than a subset. The complete suite is therefore the honest impact-based
validation. This run is **not** the final T4; the T4 gate must be re-run serially against the
frozen sealed candidate after the review and repair waves.

Result: **1479 passed, 1 skipped, 43133 warnings in 5609.66s (1:33:29), exit 0.**

## Independent implementation falsification

Candidates sealed before review:

| Seal | candidate_id | sha256 |
|---|---|---|
| `PHASE10_CANDIDATE_SEAL_v1` | `2d20fe9fc10a80af99384e961ed55aa89ce823d38e3bedecbf166d54d376cdd2` | `260f5a9bee2b6433f4ea21134d2f0af8d29c3542e33c9ae49f1366c484c3b18e` |
| `PHASE10_CANDIDATE_SEAL_v2` (repaired) | `154cfca37a265e03542021b0a2bd0cd431315ccccf7448353a6f76ca84247bbb` | `6db93b97cebcbd5e5087e564e1053cc135a82a6d7b115d9bb81657fd64a5d295` |

Routing note, recorded honestly: the design's role plan named Qwen3.8 for the broad
implementation falsification. Two launches on that route returned null and delivered nothing
(the second one still produced a partial report file). The falsification was therefore completed
on `openai/gpt-6-luna`, which is on the authorized route list; the substitution is recorded here
rather than hidden. Accounting: both failed Qwen3.8 launches still consume launch budget.

The completed adversarial review is
`reports/review/IMPL_FALSIFICATION_ENGINE.md`. Its seal and fence checks passed (area A1 SOUND,
all 17 sealed digests recomputed, zero out-of-fence tracked modification). It raised five
confirmed findings, every one of them **inside the accepted mutation fence**:

| Finding | Severity | Substance |
|---|---|---|
| F-A2-1 | MEDIUM | Full-run approval checked only `target.run_id`; a wrong `stage_id`/`attempt_number` was accepted |
| F-A2-2 | HIGH | Paid approvals proceeded with no HSF-1 Branch A pricing evidence or conservative upper bound |
| F-A3-1 | HIGH | Ordinary operator cancellation of a regeneration or synthesis rerun left durable `cancelled_pending`, the marker reserved for the hard-kill window |
| F-A4-1 | MEDIUM | Per-attempt usage was not written when an attempt completed, so `usage.json` could lag a completed attempt |
| F-A5-1 | MEDIUM | Provenance missing a declared dependency key was classified STALE instead of UNVERIFIABLE |

F-A2-2 was checked against the authority before any repair: `parcel-v2/ADJUDICATION.md` records
Mick's HSF-1 decision as Branch A, the sealed fork register marks it "Blocking: yes for the paid
preflight UI", and option A's fence impact is "none (fields on existing snapshot/approval
records)". The defect was therefore a failure to implement an existing adjudication, not a
request to change one: nothing in the fence, the product semantics, the dependency boundary or
the adjudications had to move. It was repaired rather than escalated.

## Repair waves

Repair wave 1 (`runner.py`, `storage.py`, four Phase 10 test files): closed F-A2-1, F-A3-1,
F-A4-1, F-A5-1 and corrected the O-2 relabel message to the sealed text
"stage was cancelled while the run failed with an internal error". Phase 10 set 79 passed;
guard set 84 passed; exit 0 both.

Repair wave 2 (`models.py`, `runner.py`, `core/campaign.py`, `cli.py`,
`desktop/campaign_dock.py`, two Phase 10 test files): implemented HSF-1 Branch A — a
`pricing_evidence` block per paid route carrying operator-supplied currently advertised
input/output rates, the operator-cited `rate_source`, `observed_at`, the route identity, the
selected models, the computed `conservative_upper_bound_usd` and the recorded fixed basis
(input token ceiling `= ceil(len(compiled_prompt_text) / 4)`, output ceiling `= the stage's
configured max_output_tokens`, divisor 4 and every configured ceiling recorded verbatim). A paid
approval without complete evidence is refused before any run tree, attempt or provider call and
writes nothing; a local-only operation stays exempt; the legacy plain `run` verb is unaffected
and version 1 artifacts gain no pricing fields. Phase 10 set 84 passed; guard set 84 passed;
exit 0 both.

## Post-repair independent lifecycle review

`reports/review/IMPL_LIFECYCLE_REVIEW.md` (route `minimax/minimax-m2.7`) re-verified seal v2
(17/17 digests, zero mismatches) and reported every finding CLOSED with the specific closing
code path, plus SOUND verdicts for the full cancellation matrix, shutdown, pricing enforcement
and the honest reporting surfaces. It explicitly confirmed no bypass of the paid-approval
pricing gate and that no cell of the cancellation matrix fabricates success, converts an
uncertain outcome, or auto-retries.

## Final gate

The final serial T4 is the complete repository suite run against sealed candidate v2, launched
after the repair waves and the post-repair review.

Result: **1492 passed, 1 skipped, 44101 warnings in 5246.22s (1:27:26), exit 0.** Seal v2
re-verified immediately afterwards with zero file drift and zero fence violations.

## Reserved final implementation oracle — FAIL, and repair wave 3

`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE.md` (the single reserved MiMo launch, used only
for this purpose) returned **FAIL** with a CRITICAL defect that the adversarial falsification,
the lifecycle review and the full T4 all missed:

- **D-1 CRITICAL** — `CampaignDockWidget` stored `bridge_factory` and never called it;
  `self._bridge` was only ever assigned `None`. The entire desktop surface was inert, so no
  Phase 10 operator behaviour was reachable and `DesktopRuntime._close_campaigns` could only
  ever drain an empty set. Root cause of the escape: **no test anywhere instantiated the dock**,
  and two lifecycle tests named for window attachment constructed neither a window nor a dock.
  The supervisor reproduced the defect in under 20 lines before acting.
- D-2 HIGH — the Regenerate control called `accept`/`reject`/`exec` on a plain `QWidget`.
- D-3 HIGH — the fenced "expandable output" was not implemented.
- D-4 MEDIUM — the attempt switcher was not operable.
- D-6..D-12 — shutdown coverage, missing T4 artifact, untested CLI flags, status output, and
  three LOW items, all recorded in the oracle report.

Repair wave 3 (the last authorized launch) made the surface reachable: the bridge is built and
bound exactly once on Load Job, Regenerate uses a real `QDialog`, status is shown in the UI, and
widget-level tests now instantiate the dock. The supervisor then closed the remaining D-3 gap by
hand inside the fence: `CampaignBridge` gained a read-only
`read_stage_output`/`read_stage_output_async` inspection seam over the engine's own
`storage.load_stage_view` reader, the dock renders the selected attempt's durable output through
it so desktop inspection and headless `inspect` cannot drift apart, and the latent
`prepared.synthesis_output_path` crash on a `PreparedOperation` that has no such field was
removed.

Supervisor verification after repair wave 3: the dock loads a real job, renders its identity and
validates ("Validation successful (zero spend)") with the factory invoked exactly once; Approve
is correctly disabled for a paid route without pricing evidence; 76 Phase 10 tests, 62 desktop
tests and 84 engine guard tests pass.

Candidate v3 sealed: candidate_id
`094e05ac53116cbac5580a99cac93ed3fb68f221133c6efab6a232dc0d0888b1`,
sha256 `d31fb991255cebf0502150e3eabe2bde2671150ed388e24938b678b46da2e315`, zero fence
violations. The final serial T4 was then re-run against v3.

**Residual risk recorded honestly:** no launch budget remains (32 of 32), so candidate v3 has
**not** been re-oracled. It rests on the T4 gate, the focused gates and the supervisor's own
verification, not on a fresh independent oracle.

## Repair wave 3 collateral damage, detected and repaired by the supervisor

The final serial T4 was run against candidate v3 and returned **1484 passed, 1 skipped, exit 0**
in 5310.98s (1:28:30) — eight tests **fewer** than the 1492 of candidate v2. The regression was
not accepted as noise: the count difference was investigated immediately.

Cause: repair wave 3 had been told to strengthen two vacuous lifecycle tests and add widget-level
tests. Instead it **replaced the entire contents of** `tests/test_phase10_desktop_lifecycle.py`,
destroying the eleven original lifecycle contract tests and leaving only its three new tests.
That is exactly the "do not weaken an existing test" prohibition, and it silently removed the
crash/partial-display, cancellation-terminalization, cancelled-pending hard-kill and New Job
coverage.

Recovery (supervisor, no launch consumed):

1. The pre-clobber bytecode snapshot
   `tests/__pycache__/test_phase10_desktop_lifecycle.cpython-314.pyc` (mtime 00:33, before the
   00:50 truncation) still contained all the original test names, proving the loss and its scope.
2. The original source was recovered verbatim from the DSH session transcripts at
   `/home/mick/.dsh/sessions/...` (the repair agent had read the file before overwriting it, so a
   complete line-numbered read result survived in its session log). Seven matching sessions were
   scanned; the longest clean copy reconstructed to a **syntax-valid 681-line file with exactly
   the eleven original test names**.
3. The recovered eleven were merged with repair wave 3's three genuine widget-level tests,
   yielding fourteen tests. All fourteen pass against the repaired implementation, so the
   restored contract tests and the new reachability tests are mutually consistent.

`tests/test_phase10_desktop_lifecycle.py` is now 14 tests
(sha256 `f24ff5881552c19f84e6695de7dedc925935278bb09faf8ae73f994db2d27a33`), and the full Phase 10
set is 87 tests (15 + 16 + 14 + 17 + 10 + 15), up from 84 before repair wave 3.

Candidate v4 sealed to carry the correction: candidate_id
`0b04683646f14ab66b71aa381aa506ffc82e5aa9fbb4e3a29ec9ca65346ffc17`,
sha256 `02fbcce47185dd83645febe14faab5808027f68593de3edfd806441626a29902`, zero fence
violations. Candidate v3 and its T4 run are **superseded** and must not be cited as the final
gate. The final serial T4 is re-run against v4.

Lesson recorded for the campaign: an agent-authorised replacement of a whole fenced file is a
destructive operation, and a falling total-collected count is a first-class failure signal that
must be reconciled before any gate is accepted — a green exit code alone does not prove that the
suite still tests what it tested before.

## T4 on candidate v4 — PASS (later superseded by v5)

Complete repository suite, serial, against candidate v4:

Result: **1495 passed, 1 skipped, 42591 warnings in 5298.92s (1:28:18), exit 0.**

Arithmetic, stated precisely because an earlier draft of this log conflated two different
numbers: repair wave 3 **destroyed eleven** lifecycle contract tests and **added three** new ones,
a net change of **eight fewer** (v2 collected 11 in that file, v3 collected 3). The recovery
restored those eleven, so the file again held 14, and 1495 is exactly run 2's 1492 plus the three
new widget-level tests. The restored eleven were already present in the 1492 baseline and
therefore do not appear as a delta. Seal v4 re-verified immediately afterwards: sha256 match,
zero file drift. `git diff --cached` empty and exactly the nine fenced `modify` files present.

This run was the authoritative gate until the reserved implementation oracle (launch #5) FAILED
candidate v4 with a CRITICAL desktop-dispatch defect; see the sections below and
`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v2.md`.








## Reserved implementation oracle #5 (MiMo, launch 33) — FAIL, then repaired

`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v2.md` (1208 lines) returned **FAIL**: 1 CRITICAL,
1 HIGH, 4 MEDIUM, 5 LOW, every one classified in-fence repairable, no semantic or fence problem.
It independently confirmed the D-1 repair (bridge factory invoked exactly once, real bridge bound,
full operator path live), confirmed the 1495 T4 is attributable to exact candidate v4, confirmed
no tracked drift, and **falsified** the first oracle's qasync `create_task` suspicion by executing
all five sites under a real event loop.

Findings and dispositions:

- **V-1 CRITICAL** — `_on_approve` called `approve_and_start` for every prepared operation, so a
  prepared `worker_regeneration` or `synthesis_rerun` was always refused
  ("requires a full_run operation"). Obligations 9 and 11 were unreachable from the surface.
  **Fixed** (supervisor): `_on_approve` now dispatches by operation to `approve_and_start`,
  `approve_and_regenerate` or `approve_and_rerun_synthesis`. A new guard
  `test_approve_after_regeneration_dispatches_and_creates_sibling_attempt` presses Approve for
  real and requires the durable `w1.att2.json` sibling with state `succeeded` and the operator's
  typed model. **Mutation-tested**: the guard FAILS against the pre-repair dispatch and PASSES
  against the fix, and the source was restored byte-identically (sha256 match).
- **V-2 HIGH** — Regenerate and Rerun-synthesis were disabled whenever the selected stage was
  `succeeded`, i.e. on every row after a normal successful run, although the engine accepts a
  succeeded regeneration target and rerun synthesis is a run-level control.
  **Fixed** (supervisor): Regenerate is offered whenever a non-running stage is selected; Rerun
  synthesis no longer depends on the selected worker's state.
- **V-5 MEDIUM** — a synthesis cancelled before dispatch was reported `UNVERIFIABLE` with an
  integrity warning falsely asserting a *dispatched* attempt, contradicting the accepted design
  ("never-dispatched synthesis is NOT_APPLICABLE, no integrity warning"). **Fixed** (supervisor):
  Rule 1 now also keys on an explicit `started_at: null` for evidence-v2 attempts, with a version
  guard so version-1 evidence still reads LEGACY_UNVERIFIED.
- **V-6 LOW** — D-12 confirmed: `persist_stage_attempt` wrote metadata before the output mirror, so
  a failed output write durably advertised `succeeded` with an unreadable artifact.
  **Fixed** (GLM worker A): on the `create=False` path the output is now written before the
  metadata, so a failure leaves the metadata un-advanced; the `create=True` ordering is unchanged
  because the overwrite-refusal contract requires the exclusive metadata claim to precede output.
  A fault-injection regression test was added, and the worker reported it failing against the old
  ordering and passing after the fix.
- **V-3, V-4, V-10 (all TEST_GAP)** — the D-2 guard was an `inspect.getsource` string assertion
  that three of five assertions would have passed against the broken code; the two window tests
  were still vacuous; the positive attach path, the fence-named T0.8 "shutdown with active
  campaign", `inspect --attempt` and `regenerate|rerun-synthesis --approval <file>` had no test.
  **Fixed** (GLM worker B): the source-text guard was replaced by a behavioural dialog test, both
  window tests were rewritten to construct a real MainWindow, the attach and bounded-drain paths
  gained tests, and four CLI tests were added.
- **V-7, V-8, V-9** accepted as documented limitations (D-10, D-11, and the 200 px output cap).
- **V-11 LOW** documentation contradiction ("eight destroyed" vs "eleven"): **corrected**. Eleven
  were destroyed and three added, a net of eight fewer; both documents now say so.

## Budget amendment and repair wave 4

Mick amended the ceilings twice on 2026-09-30 (verbatim records in
`logs/BUDGET_AMENDMENT_2026-09-30.md`): global child-launch ceiling 32 to 34 with MiMo raised 4 to
6 and launch #5 reserved exclusively for the implementation oracle; then, after #5 returned a
repairable defect, the ceiling raised 34 to 36 and the GLM allowance raised to 14 for two
implementation workers, with MiMo #6 still reserved exclusively for the oracle against the
repaired candidate. Final arithmetic: 33 used + 2 GLM implementation + 1 reserved oracle = 36.

Repair wave 4 result: Phase 10 fenced suite **95 passed** (was 87; +4 cross-cutting, +3 lifecycle,
+1 regeneration) and the desktop and engine guard set **125 passed**.

Candidate v5 sealed to carry the repairs: candidate_id
`bf522194e5a44b61ad68c8a43605a1c2c100f4926e700ec1f2aca4b3874b25ae`, sha256
`ad56b9bfeefdad11d3639eee50571ddd0c371e354b0e9539e9f4111632ba17b6`, zero fence violations.
The final serial T4 is re-run against v5 before the MiMo #6 oracle.

## Candidate v5 superseded by v6 (guard completeness, supervisor)

Candidate v5 was sealed and its T4 started, then deliberately killed: a coverage audit showed
that V-5 was the one candidate-changing repair with **no regression guard**. Existing tests cover
only the literal ``state == "skipped"`` path; the cancelled-before-dispatch shape that the oracle
actually reproduced (``state: failed``, ``failure.type: cancelled``, explicit ``started_at: null``,
no provenance) had none. Shipping a candidate-changing fix with no guard is the exact pattern that
let D-1 and then V-1 survive earlier gates, so the run was stopped rather than banked.

Added ``test_synthesis_cancelled_before_dispatch_is_not_applicable_without_false_warning`` to
``tests/test_phase10_cross_cutting.py``: it persists the real cancelled-before-dispatch shape and
asserts ``NOT_APPLICABLE``, an empty integrity-warning tuple, and specifically that the word
"dispatched" never reappears in a warning. **Mutation-tested**: it FAILS against the pre-repair
Rule 1 (skipped-only) and PASSES against the fix, with ``storage.py`` restored byte-identically
(sha256 match).

Both V-1 and V-5 therefore now have guards that are proven to discriminate between the broken and
repaired code, rather than guards that merely agree with the current source.

Fenced Phase 10 suite after the addition: **96 passed** (15 + 21 + 17 + 17 + 10 + 16).
Candidate **v6** sealed: candidate_id
`d5c433073332b9a0c0de3eaf7dc93d8550168348f8f0603c0e888515181f2946`, sha256
`36078b8838dd1d6266e732e6161ce1bbb7979bb64e9328de4d2957a1cd103197`, zero fence violations.
The MiMo #6 oracle will run against v6; the ledger's reservation of launch 36 for "candidate v5"
is superseded accordingly.

## Final T4 on candidate v6 — PASS

Complete repository suite, serial, against candidate v6: **1504 passed, 1 skipped, 37688 warnings
in 5543.31s (1:32:23), exit 0.** 1504 is exactly the v4 run's 1495 plus the nine tests added in
wave 4. Seal v6 re-verified afterwards with zero file drift.

## Reserved implementation oracle #6 (MiMo, launch 36) — ran fully, died before reporting

The last authorized launch executed the entire oracle assignment against candidate v6 and completed
**all nine** of its verification milestones, then failed while writing its report: five consecutive
`Upstream idle timeout exceeded` errors, the turn ending in error and the workflow returning null.
**No oracle-authored report exists.**

Its evidence and conclusions were recovered by the supervisor from the run's session transcript and
preserved, clearly labelled as a reconstruction, at
`reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE_v3_SUPERVISOR_RECONSTRUCTION.md`; the run's own
scratch is at `logs/demo-scratch/.oracle-v3/`.

What it established by execution: seal v6 verifies with zero drift, HEAD unchanged, exactly nine
fenced files modified, nothing staged, eight fenced adds, and 0 files outside `work/` modified since
the T4 began; the dock still binds its bridge exactly once and the V-1 approve path works; the
qasync probes re-run clean; its scratch copy reproduced a faithful 96-passed baseline identical to
the worktree. It found **no CRITICAL or HIGH defect and no candidate-changing defect of any
severity**, and raised two TEST_GAPs: F-2 (re-coupling Rerun-synthesis to the worker row leaves all
96 tests passing — the V-2 repair is only half guarded) and F-3 (making version-1 freshness return
None leaves the suite green — the legacy-v1 guard does not discriminate).

On that evidence the outcome is **PASS_WITH_LIMITATIONS**, read by the supervisor from the run's own
outputs rather than signed by the oracle.

F-2 and F-3 were deliberately NOT repaired: both are test gaps, and repairing them would change the
candidate to v7 and invalidate the 1504-test T4 with no launch left to re-oracle a v7, leaving the
final candidate less verified rather than more.

Campaign stopped at the sealed pre-commit boundary with candidate v6. Launch budget exhausted (36 of
36). No staging, commit, push, ref mutation, OrgMem mutation or dependency change.
