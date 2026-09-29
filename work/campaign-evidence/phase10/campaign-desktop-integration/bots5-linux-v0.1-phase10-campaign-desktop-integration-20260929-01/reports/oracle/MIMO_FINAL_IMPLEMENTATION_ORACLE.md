# MIMO FINAL IMPLEMENTATION ORACLE — B.O.T.S. Linux v0.1 Phase 10

STATUS: complete — verdict FAIL (see section F)

## A. SEAL VERIFICATION

Seal hash verified: `6db93b97cebcbd5e5087e564e1053cc135a82a6d7b115d9bb81657fd64a5d295`
(sealed file `seals/implementation/PHASE10_CANDIDATE_SEAL_v2.json`), which is exactly the sha256 the
campaign brief asserts. The sidecar `PHASE10_CANDIDATE_SEAL_v2.sha256` carries the same digest.

All digests were recomputed independently by me (a from-scratch python3 hashlib script, not the
candidate's own `make_candidate_seal.py verify`). Results:

| Item in seal | Recomputed value | Match |
|---|---|---|
| seal file self-sha256 | `6db93b97...a5d295` | YES (== brief) |
| `files` — all 17 fenced paths | recomputed each with sha256 | YES — drift: none (0 mismatched, 0 missing) |
| `candidate_id` = sha256(canonical JSON of `files` map) | `154cfca37a265e03542021b0a2bd0cd431315ccccf7448353a6f76ca84247bbb` | YES (== brief) |
| `design_seal_v4_sha256` | `9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1` | YES |
| `mutation_fence_sha256` | `602e6d5d3a88eb893d11d064dfc28c70e8f692c52cb823cd0c8b2711907cf452` | YES |
| `parcel_v2_manifest_sha256` | `45fd91f3f383620e4da7575c7a5ba92abf9298ed47c72cf7eb30a315c854391b` | YES (also equals the `PARCEL_V2_MANIFEST.sha256` sidecar) |
| `file_count` | 17 | YES |
| fence path set (9 `modify` + 8 `add`) vs seal `files` key set | identical sorted sets, 17 paths | YES |
| `head_commit` | `git rev-parse HEAD` = `0756904481ae884bb9e864e8e1e11fc4a27a72ff` | YES (matches `BASELINE.json`/fence `baseline_head`) |

I also recomputed the digests *inside* the referenced seals (chain of custody), all clean:

- `DESIGN_SEAL_v4.json`: all 12 `design_artifacts`, 2 `oracle_inputs`, 10 `referenced_inputs`,
  1 `review_inputs` digests AND byte counts recomputed — drift: none. Its embedded
  `design/MUTATION_FENCE.json` digest equals the current fence file. (Symbol: n/a — hash check.)
- `parcel-v2/PARCEL_V2_MANIFEST.json`: all 11 file digests + byte counts recomputed — drift: none.
  Its `design_of_record.seal_id`, `mutation_fence.sha256`, and `baseline.head` all cross-match the
  design seal, fence, and live `HEAD` respectively.

**Worktree fence check.** `git status --porcelain` at verification time:

- tracked modifications (letter `M`): exactly the 9 paths in the seal's
  `tracked_modified_in_fence` — `src/bots5/{bootstrap/desktop,cli,desktop/window,errors,events,
  models,runner,storage,usage}.py`.
- tracked modifications outside the fence: **NONE** (`tracked_modified_out_of_fence: []`,
  `fence_violations: []` — recomputed and confirmed). Command: `git status --porcelain`.
- untracked (`??`) entries: `.audit-tmp4/`, `.verifyrun/`, `.verifyrun2/`,
  `AUDIT-2026-09-28-evidence-only-review.md`, `work/` (evidence tree, contains this report), plus
  the 8 fence-`add` paths (`src/bots5/core/campaign.py`, `src/bots5/desktop/campaign_dock.py`,
  6 `tests/test_phase10_*.py`). No untracked file sits in a guarded path outside the fence list
  other than the pre-existing scratch dirs/audit note above, which are untracked (never tracked at
  `HEAD`), so no *tracked* path outside `MUTATION_FENCE.json` has been modified.
- `git stash list`: empty — nothing hidden from the status check.

Verdict for A: **SEAL VERIFIED — every digest in `PHASE10_CANDIDATE_SEAL_v2.json` recomputes
correctly and no tracked path outside the mutation fence has been modified.**

## B. INDEPENDENT OBLIGATION CHECK

I checked each item against the code myself (I read the candidate modules in full and ran the
probes in section C); the earlier reviews are treated as claims, not evidence. Notation:
**SATISFIED** = verified by me; **NOT SATISFIED** = verified defect; **UNPROVEN** = mechanism
exists but nothing proves the required behavior.

### Locked product obligations (CONTRACT.md lines 15–37)

| # | Obligation | Verdict | Evidence (file + symbol) |
|---|---|---|---|
| 1 | load an existing campaign job | **NOT SATISFIED** at the desktop surface; SATISFIED headlessly | `CampaignDockWidget._on_load_job` (`src/bots5/desktop/campaign_dock.py:493`) never gets a bridge: `self._bridge` is initialized `None` at line 253 and assigned to a real bridge **nowhere** in the file, and `bridge_factory` (line 252) is never called — probe C1 printed `[CampaignDock] Bridge not available`, factory call count 0 (the success path that would enable the rest of the dock is at lines 510-517). Engine path exists: `CampaignBridge.load_job` (`src/bots5/core/campaign.py:705`). |
| 2 | zero-spend validation, no provider call, no run-directory creation | **NOT SATISFIED** at the desktop surface; SATISFIED in engine | `_on_validate` (`campaign_dock.py:519`, guard at 521) short-circuits on `self._bridge is None`, and `_validate_button` starts disabled (line 354) — only `_on_load_job`'s success path enables it (line 512), so C1 observed `isEnabled() == False`. Engine: `CampaignBridge.validate` (`campaign.py:722`) is side-effect-free — probe C2 shows `runs dir exists: False` after load+validate+prepare. |
| 3 | preflight before spend | **NOT SATISFIED** at the desktop surface; SATISFIED in engine | The dock's preflight block (`campaign_dock.py:579-591`, `if self._bridge is not None and self._runs_dir is not None`) never executes. Engine: `CampaignBridge.prepare_full_run` (`campaign.py:924`) constructs no provider and writes nothing; `test_paid_approval_without_pricing_is_refused_without_writes_or_provider_calls` (`tests/test_phase10_desktop_preflight.py:195`) + probe C2. |
| 4 | explicit operator approval before any provider request | **SATISFIED** (accepted scope: v2/desktop paths) | Sole construction point is `approve_and_start`/`approve_and_regenerate`/`approve_and_rerun_synthesis` (`campaign.py:1228/1267/1311`), each of which runs `_validate_prepared_pricing` (`campaign.py:791-817`) and consumes consent (`_consume_approval`, `campaign.py:819-833`) BEFORE `self._provider_factory(...)` (lines 1246/1283/1328); engine-side `_require_approval_binding` (`runner.py:235-419`) asserts digest/scope/target/pricing before `create_run_tree` (`runner.py:898`) and dispatch — probes C2, C3. Legacy headless `bots5 run` intentionally executes without approval per the fence contract "snapshot/approval optional: headless run_job behavior unchanged when omitted" (`MUTATION_FENCE.json` runner.py entry) — a grandfathered product choice the human acceptor should acknowledge, not an implementation deviation. |
| 5 | live truthful campaign/worker/stage/synthesis/cost progress | **NOT SATISFIED** at the desktop surface; SATISFIED in engine | The projection exists and is honest (`project_run`, `campaign.py:417`; `live_cost` = known subtotal + explicit unknown set, `campaign.py:503-510`), polling bounded (`_sync_polling`, `campaign_dock.py:932-943`), but `_refresh_projection_async` early-returns on `self._bridge is None` (`campaign_dock.py:663-665`) and the poll gate also requires `self._projection is not None` (line 937), which stays `None` — probe C1. |
| 6 | expandable worker/stage output and durable result inspection | **NOT SATISFIED** on the desktop; SATISFIED headlessly | `_result_text.setPlainText` is called only with `prepared.summary` (lines 875/897) or `""` (line 636); grep proves no call ever renders a stage's `output_path` (carried but unconsumed at `campaign_dock.py:159`). Fence requires "expandable output" for this module (`MUTATION_FENCE.json` campaign_dock.py `why`) and the design requires "expandable persisted output per attempt" (`design/DESKTOP_SURFACE_AND_LIFECYCLE.md:68`). Headless durable inspection works: `bots5 inspect` output line `output=...` (`cli.py:219`). |
| 7 | truthful successful / failed / timed-out / partial outcomes | **SATISFIED** in engine + readers; desktop unreachable | `_display_state` (`campaign.py:367`), interrupted/uncertain stage handling (`campaign.py:293`), run states written at `runner.py:928-930` (RUNNING), `1155` (TIMED_OUT), `1197-1199` (CANCELLED), `1234-1236` (final SUCCEEDED/FAILED), `661-663` (best-effort FAILED); probe C4 shows durable `run.json state failed` + `run_failed error_type internal_error` with no `run_cancelled`. Desktop rows would render these (`_render_projection`, `campaign_dock.py:676-697`) but no projection ever arrives. |
| 8 | completed/failed result remains current until explicit `New Job` | **UNPROVEN** | `_on_clear_job` (`campaign_dock.py:620`) clears UI only and writes nothing — correct — but the two tests named for this (`test_new_job_clears_ui_working_context_without_deleting_historical_run_evidence`, `test_new_job_does_not_modify_run_directory_bytes`, `tests/test_phase10_desktop_lifecycle.py:580/614`) simulate New Job by poking `bridge._job = None` etc.; **no test constructs `CampaignDockWidget` or clicks the button** (grep: no test references the widget besides comments). Inert dock makes the button unreachable anyway (C1). |
| 9 | selected-worker regeneration (preserve, sibling, model change, no silent route change, never generic retry) | **SATISFIED** in engine; **NOT SATISFIED** at the desktop surface | `regenerate_worker` (`runner.py:1708`) claims attempt N+1 via `_exclusive_write_json` (`storage.py:187`), preserves attempt 1, never touches `selection.json`, and locks the route through `_require_operation_provider_route` (`runner.py:1478`) + `_require_operation_snapshot_binding` (`runner.py:1527`); no auto-retry exists anywhere in the operation paths (comment block `runner.py:1264-1270`, code verified by full read). Desktop: `CampaignDockWidget._on_regenerate` (`campaign_dock.py:827`) crashes with `AttributeError: 'PySide6.QtWidgets.QWidget' object has no attribute 'accept'` at line 855 (also `reject` at 856, `exec` at 858) — probe C1f. |
| 10 | changed selected worker attempt marks dependent synthesis stale | **SATISFIED** | `storage._synthesis_freshness` (`storage.py:887`) — selection/digest mismatch ⇒ `STALE` with `integrity_warning`; probe C5 `stale` case: `classification=STALE warnings=["dependency digest mismatch for 'w1' attempt 2"]`. `synthesis_stale` event emitted by `CampaignBridge.select_attempt` (`campaign.py:1355`). |
| 11 | synthesis rerun is explicit, new attempt, earlier evidence preserved | **SATISFIED** in engine; **NOT SATISFIED** at the desktop surface | `rerun_synthesis` (`runner.py:1877`) writes attempt N+1 exclusively, moves `selection.json` only on success (line 2095-2104), appends events; `_on_rerun_synthesis` (`campaign_dock.py:884`) returns immediately when `self._bridge is None` (C1). |
| 12 | existing campaign filesystem evidence inspectable and authoritative | **SATISFIED** | Readers never rewrite: probe C6 built a version-1 run, exercised every new verb, and the whole run tree stayed byte-identical (`version-1 run tree byte-identical after all new verbs: True`); projection is read-only (grep no `write`/`open(...,"w")` in `project_run`); golden-fixture tests `tests/test_phase10_backward_compat.py`. |
| 13 | headless CLI/engine remains usable without the desktop | **SATISFIED** | `cli.py` verbs `validate/run/status/inspect/regenerate/rerun-synthesis` dispatch in `main` (`cli.py:603`); probe C6: all verbs parse and refuse a v1 target with exit 1, no writes; consent gating at `cli.py:500/565` (`--approve`/`--approval` preflight-only otherwise, paid consent requires `--pricing`). |

### Contract integrity sections (CONTRACT.md lines 39–110) and anti-vacuity rules (lines 174–185)

| Requirement | Verdict | Evidence |
|---|---|---|
| Immutable/mechanically reverified execution snapshot before first provider request (TOCTOU closure) | **SATISFIED** | `build_preflight_snapshot`/`PreflightSnapshot.compute_digest` (`runner.py:201`, `models.py`), asserted inside `_require_approval_binding` — snapshot self-identity (lines 262-265), live-configuration recompute vs frozen payload (lines 278-296), referenced file bytes (steps 5-6, `verify_files` at 298+) — all before `create_run_tree` (`runner.py:898`); operation variants re-read referenced bytes exactly once and rebuild the same digest via `_rebuild_live_preflight_digest` (`runner.py:1348`); probe C2 engine refusal before any dispatch; test `test_run_refuses_when_referenced_bytes_change_after_approval` (`tests/test_phase10_desktop_preflight.py:453`). |
| No in-place rewrite of historical stage output | **SATISFIED** | v2 attempts are separate flat files `stages/<id>.att<N>.{json,md}` created exclusively (`storage.attempt_paths`, `storage.py:172`); v1 paths never written by v2 code (same symbol docstring); probe C6 v1 tree untouched. |
| Cost truth: truthful known cost + unknown state, no fabricated token-dollar progress | **SATISFIED** | `usage._spend_summary`/`derive_selected_spend` (`usage.py:78`) mirror `aggregate_cost`'s unknown/partial status rules; projection exposes `live_cost` = "known subtotal plus explicit unknown set over the selected attempts" (`campaign.py:503-510`); no interpolation anywhere (grep `known_cost_usd` assignment only `_apply_result`/`_durable_known_cost`). |
| Conservative preflight pricing for paid execution without inventing a pricing authority | **SATISFIED** | `build_pricing_evidence` (`runner.py:421-510`) requires operator rates + `rate_source` + `observed_at` (validation block `runner.py:455-475`), records route/models/bound/basis (entry built at 497-508) with fixed formula `_PRICING_BASIS_FORMULA` (`runner.py:411-415`); no registry, no network pricing lookup (grep: no pricing fetch anywhere in `src/bots5`); probe C2 refusal, C3a bound `0.00172625` recorded. |
| Lifecycle truth: no auto-retry/resume, durable truth, cancellation terminalized | **SATISFIED** | `_terminalize_operation_cancellation` (`runner.py:1679`); run-level `RunState.CANCELLED` written only from the `CancelledError` handler (`runner.py:1157-1199`); no retry loops in operation paths (verified by reading `regenerate_worker`/`rerun_synthesis` in full); probe C4. |
| Backward compatibility with V0/V0.2 evidence | **SATISFIED** | probe C6 byte-identical v1 tree across all new verbs; golden readers `tests/test_phase10_backward_compat.py`; `EVIDENCE_VERSION` marker absent ⇒ readers use legacy paths (`storage._run_dir_evidence_version`, `storage.py:721`). |
| Zero-diff guards (providers, manifest, paths, application, pyproject, db, evidence, examples) | **SATISFIED** | `git diff HEAD --stat -- pyproject.toml db/ evidence/ examples/ src/bots5/providers/ src/bots5/manifest.py src/bots5/paths.py src/bots5/core/application.py` → empty; `git diff --cached` → empty (nothing staged). |
| Fence containment (only the 17 sealed paths) | **SATISFIED** | section A: tracked modifications outside `MUTATION_FENCE.json` = none. |
| Explicit exclusions (lines 94–110): no campaign-authoring UI, no retry/resume, no provider streaming redesign, no daemon, no Phase 11/12, no retained-installation cleanup, no dependency change | **SATISFIED** | no retry loops anywhere in the operation paths (full read of `regenerate_worker`/`rerun_synthesis`); no streaming code added (providers zero-diff); `pyproject.toml` zero-diff (git, C-7); no new top-level modes/commands beyond the three fenced verbs; `db/`, `evidence/`, `examples/` untouched. |
| Anti-vacuity rules (lines 174–185): every selected pytest gate proves non-zero collection | **PARTIALLY SATISFIED** | all six `tests/test_phase10_*.py` files collect and the focused selectors run (C-4, C-10, exit 0), but two *named* gates execute nothing about their namesake requirement (`tests/test_phase10_desktop_lifecycle.py:640/664`) — collection is non-zero while the assertion is vacuous (D-5). |

### Adjudicated items

| Item | Verdict | Evidence (file + symbol) |
|---|---|---|
| **HSF-1 Branch A** — paid approval requires operator rates + source + observation time + route + fixed conservative bound; no registry/lookup/waiver | **SATISFIED** | `build_pricing_evidence` (`runner.py:421-510`) enforces non-empty `rate_source`/`observed_at` (validation block lines 455-475), always records `route`/`models`/`basis`/`conservative_upper_bound_usd` (entry at lines 497-508); `runner._require_approval_binding` refuses paid approvals without evidence (lines 400-406) — probe C2 `engine refused: paid approval requires complete operator pricing evidence`, `provider.complete() calls: 0`; bridge refuses before the factory (`campaign.py:791-817`, refusal at 814-817) — probe C2c `provider factory constructions: 0`. Local (unpaid) routes are exempt because `build_pricing_evidence` returns `None` when no paid stage is dispatched (`runner.py:430-433`), matching the OPv1 local clause. |
| **HSF-2 option 2a** — filesystem-only restart persistence, no app-DB pointer/migration | **SATISFIED** | `grep sqlite` over `core/campaign.py` + `campaign_dock.py` → no hits; bridge state is in-memory (`CampaignBridge.__init__`, `campaign.py:610`) with durable truth only in the run directory; `db/migrations/**` untouched (git zero-diff, section A/fence guards). |
| **HSF-3 option 3a** — explicit run path/ID, no run-directory enumeration | **SATISFIED** | `grep -n "iterdir\|listdir\|scandir\|glob(\|os.walk"` over `core/campaign.py` and `campaign_dock.py` → **no hits**; `adopt_run` (`campaign.py:744`) accepts an explicit path or validated run id via `locate_run_dir` (`paths.py:52`, a zero-diff guard that joins + containment-checks one id, no listing). |
| **HSF-4 option 4a** — `RunState.CANCELLED` run-level; stage-level `"cancelled"`; sibling cause preserved; hard-kill `cancelled_pending` reads interrupted/uncertain | **SATISFIED** | `RunState.CANCELLED` (`models.py:21`); run-level write only in the `CancelledError` handler (`runner.py:1157-1199`, `state=RunState.CANCELLED` at 1197-1199); sibling preservation at `_best_effort_internal_failure` (`runner.py:628-641`) — probe C4e `sibling w2 failure: cancelled | unknown: True` while C4b `run.json state: failed` and C4c `run_failed ... error_type: internal_error`; hard-kill reads `interrupted_uncertain` (`campaign.py:_display_state:367-374`) + test `test_durable_cancelled_pending_after_hard_kill_reads_interrupted_uncertain` (`tests/test_phase10_desktop_lifecycle.py:481`). |
| **HSF-5 option 5a** — provider route locked; explicit model change allowed, no route switch | **SATISFIED** | `_require_operation_provider_route` (`runner.py:1478`) compares the frozen route dict (and class identity via `_PROVIDER_KIND_CLASSES`, `runner.py:97`); only `snapshot.model` may differ; binding digest covers the route (`models.OperationSnapshot.compute_digest`); test `test_worker_regeneration_explicit_model_change_does_not_silently_change_provider_route` (`tests/test_phase10_evidence_regeneration.py:397`); `providers/**` zero-diff (git, section A). |
| **O-1** — non-dispatched terminal synthesis classified NOT_APPLICABLE before staleness | **SATISFIED** | `storage._synthesis_freshness` Rule 1 (`storage.py:915-924`) returns before any provenance/staleness evaluation; probe C5 `skipped -> classification=NOT_APPLICABLE integrity_warning=False warnings=[]`; selector present: `test_non_dispatched_synthesis_never_reported_as_stale` (`tests/test_phase10_cross_cutting.py:177`). |
| **O-2** — sibling cancelled during generic run failure keeps `error_type=="cancelled"` while run stays FAILED/internal_error, asserted on durable artifacts | **SATISFIED** | Required selector exists and passes (ran it, exit 0): `test_cancelled_sibling_keeps_cancelled_cause_while_run_remains_failed_internal_error` (`tests/test_phase10_evidence_regeneration.py:831`), which reads `run.json`, `events.jsonl` and `stages/*.att1.json` from disk. My independent probe C4 reproduced it with a different fault: `run.json state: failed`, `run_failed error_type: internal_error`, `w2 failure: cancelled`, `provider_side_outcome_unknown: True`, `run_cancelled events: []`. |
| **O-3** — stage-level label is exactly `"cancelled"`; HSF-4 changes run level only | **SATISFIED** | `record.error_type = "cancelled"` in `_best_effort_internal_failure` (`runner.py:634`), the `CancelledError` handler (`runner.py:1173`), and `_terminalize_operation_cancellation` (`runner.py:1690`); probe C4e/C4i (`cancelled_pending left: []`); grep for any other stage cancellation label → none. |
| **O-4** — serialized field name `api_key_source`, never `key_source`, never a secret | **SATISFIED** | `grep -rn "key_source"` restricted to non-`api_key_source` occurrences in the fenced modules → no hits; emitted at `runner.py:155` and `:164` (`declared_provider_routes`) and `runner.py:1452`/`:1459` (`_provider_routes_from_resolved_job`); asserted on disk by `test_evidence_version_2_run_writes_preflight_and_attempt_one_records_with_digest` (`tests/test_phase10_desktop_preflight.py:603-609`), which also asserts the key value never appears in `preflight.json`. |

### Independent re-check of the prior reviews' findings (not trusted)

| Prior finding | Oracle verdict |
|---|---|
| Falsification F-A2-1 (full-run approval accepted a mismatched target) | **CLOSED — CHECKED-AND-SOUND**: `runner._require_approval_binding` now compares the whole target dict against `{"run_id", "stage_id": None, "attempt_number": None}` (`runner.py:272-277`) and also binds scope, actor and digest. |
| Falsification F-A3-1 (operation cancellation persisted as `cancelled_pending`) | **CLOSED — CHECKED-AND-SOUND**: `except asyncio.CancelledError` in `regenerate_worker` (`runner.py:1857`) and `rerun_synthesis` (`runner.py:2080`) call `_terminalize_operation_cancellation` (`runner.py:1679`) which persists `error_type="cancelled"` before re-raise. |
| Falsification F-A4-1 (completed attempt usage written only at run end) | **CLOSED — CHECKED-AND-SOUND**: every stage completion fires `usage_callback` → `_persist_operation_usage` → `persist_usage_v2` (`_execute_stage` wiring `runner.py:712-716`, callback defined 957-959, passed at 977 and 1085); proven on disk by `test_v2_usage_includes_completed_worker_while_sibling_is_still_running` (`tests/test_phase10_desktop_preflight.py:627`), which reads `usage.json` mid-run. |
| Falsification F-A5-1 (incomplete provenance labeled STALE) | **CLOSED — CHECKED-AND-SOUND**: probe C5 `incomplete -> UNVERIFIABLE integrity_warning=True` and `missing-one-key -> UNVERIFIABLE`. |
| Lifecycle review F-L3 "desktop dock Approve button gate is empty ⇒ only a complete approval can be clicked" | **REJECTED as sufficient**: the gate (`campaign_dock.py:579-591`) is real, but the button and the whole dock are unreachable because `self._bridge` is never assigned — see C1 and D-1. The lifecycle review never instantiated the widget. |
| Lifecycle review T0.10 claims (window attaches/does not attach the dock) | **UNPROVEN**: the two tests named for it construct no window and no dock (`tests/test_phase10_desktop_lifecycle.py:640/664` only assert `bridge.job_path is None`); no test passes `campaign_bridge_factory` to `MainWindow`. |



## C. ADVERSARIAL SPOT CHECKS

All probes ran against the sealed worktree with the canonical interpreter
(`QT_QPA_PLATFORM=offscreen PYTHONPATH=src /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1/.venv314/bin/python`),
each in its own heredoc script writing only under `/tmp/oracle-c*`. Every probe below is
individually quoted with its observed output. No repository-wide pytest run was started.

### C-1 — Is the desktop campaign dock actually operator-reachable? (highest-risk claim: "Phase 10 is operator-reachable from the native desktop")

Command (inline script, key operations):
```python
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python - <<'PY'
from PySide6.QtWidgets import QApplication, QFileDialog, QTableWidgetItem
app = QApplication([])
from bots5.desktop.campaign_dock import CampaignDockWidget
factory_calls = []
def factory(runs_dir):
    factory_calls.append(runs_dir); raise AssertionError("bridge factory invoked")
dock = CampaignDockWidget(bridge_factory=factory)
print("dock._bridge after construction :", dock._bridge)
print("bridge_factory call count       :", len(factory_calls))
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("/tmp/oracle-job.json", ""))
dock._on_load_job()
print("bridge_factory call count after Load Job:", len(factory_calls))
dock._on_validate()
print("validate button enabled:", dock._validate_button.isEnabled())
dock._bridge = object()                      # only to reach the dialog code
cols = ["w1","succeeded","1","model-w1","0.1s","30","0.01","Complete"]
dock._stages_table.setRowCount(1)
[dock._stages_table.setItem(0, i, QTableWidgetItem(t)) for i, t in enumerate(cols)]
dock._stages_table.selectRow(0)
print("selected stage:", dock._selected_stage()["stage_id"])
try:
    dock._on_regenerate(); print("regenerate dialog: returned normally")
except Exception as exc:
    print("REGENERATE DIALOG RAISED:", type(exc).__name__, "-", exc)
PY
```
Observed output:
```
dock._bridge after construction : None
bridge_factory call count       : 0
[CampaignDock] Bridge not available
bridge_factory call count after Load Job: 0
[CampaignDock] Bridge not available
validate button enabled: False
selected stage: w1
REGENERATE DIALOG RAISED: AttributeError - 'PySide6.QtWidgets.QWidget' object has no attribute 'accept'
```
**Result: CONFIRMED DEFECT ×2** (see D-1, D-2). Supporting static checks (executed):
```python
import inspect; from bots5.desktop import campaign_dock as cd
src = inspect.getsource(cd)
print("_bridge_factory( in dock source :", "_bridge_factory(" in src)   # False (stored at line 252, never called)
print("self._bridge = (non-None)       :", "self._bridge =" in src.replace("self._bridge = None",""))  # False
```
and, from `grep -n "_result_text\.\|output_path" src/bots5/desktop/campaign_dock.py`:
```
159:  "output_path": stage.output_path,          # carried in the row model, consumed nowhere
469:  self._result_text.setObjectName(...)
636:  self._result_text.setPlainText("")
875:  self._result_text.setPlainText(prepared.summary)
897:  self._result_text.setPlainText(prepared.summary)
```
→ the result pane never receives stage output, and `output_path` has no consumer.

Exhaustive cross-check (`grep -n "self._bridge" src/bots5/desktop/campaign_dock.py`) shows only
two assignments — `253: self._bridge: CampaignBridge | None = None` and `622: self._bridge = None`
— and every consumer guards on `is not None` (lines 507/521/579/608/665/814/830/886/912/963), so
the entire widget is provably inert. `CampaignDockWidget` is constructed by `window.py:383-384`
with the runtime factory (`bootstrap/desktop.py:240`), but the dock never uses it — and therefore
`DesktopRuntime._close_campaigns` (`bootstrap/desktop.py:246`) always iterates an empty set: the
bounded-shutdown machinery can never have a hosted campaign to drain.

### C-2 — A paid approval without pricing evidence is refused before any provider call

Command (inline script, key operations): build `tests.helpers.make_job_tree` (openrouter routes);
layer 1 = `CampaignBridge.load_job` → `validate` → `prepare_full_run("oracle", pricing_evidence=None)`
→ `approve_and_start(prepared)` with a provider factory that records construction; layer 2 =
direct `asyncio.run(run_job(job, {"openrouter": spy}, run_id=..., snapshot=..., approval=...))`
with an `OpenRouterProvider` subclass whose `complete()` increments a counter and raises.

Observed output:
```
approval.pricing_evidence: None
route kinds: {'openrouter': 'openrouter'}
bridge refused: ApprovalInvalidatedError - paid approval requires complete operator pricing evidence
provider factory constructions: 0
runs dir exists: False | entries: []
engine refused: ApprovalInvalidatedError - paid approval requires complete operator pricing evidence
provider.complete() calls: 0
run dir created: False
```
**Result: CHECKED-AND-SOUND (HSF-1 Branch A).** Refused at both layers, with zero provider
constructions, zero provider requests and no run directory. (Carry symbol:
`runner._require_approval_binding` lines 400-406; `campaign._validate_prepared_pricing` lines 814-817.)

### C-3 — A replayed approval creates no run directory and makes no provider request

Command (inline script, key operations): full v2 run through the bridge with pricing evidence,
`bridge.approve_and_start` + `await bridge.run_to_completion()`; snapshot every file+sha256 under
the runs dir; then (a) replay the SAME approval through the engine entry point with a FRESH
provider spy, (b) replay the same `PreparedOperation` through the bridge.

Observed output:
```
pricing bound recorded: 0.00172625
first run state: succeeded | provider calls: 3
engine refused replay: ApprovalInvalidatedError - approval already consumed: approval-20260929T134945Z-3cc57d11
replay provider calls: 0
original provider calls unchanged: True
run-dir tree identical after replay: True | run dirs: ['test-job-20260929T134945Z-f1dff2b7']
bridge refused replay: ApprovalInvalidatedError - approval already consumed: approval-20260929T134945Z-3cc57d11
run-dir tree identical after bridge replay: True
```
**Result: CHECKED-AND-SOUND (one-shot durable consent).** The durable marker
(`storage.consume_approval`, O_CREAT|O_EXCL at `storage.py:202`) is what refuses the replay —
exactly one run directory exists before and after, and the fresh spy recorded 0 calls.

### C-4 — A cancelled sibling keeps its own cause while the run stays FAILED with internal_error (O-2, independent reproduction)

Command (inline script, key operations): my own run through `run_job` with an
`OpenRouterProvider` subclass that, on `model-w1`, turns `stages/w1.att1.md` into a directory
(so persisting w1's completion raises `StorageError` mid-pipeline) while `model-w2` sleeps 1 s;
then read `run.json`, `events.jsonl` and `stages/*.att1.json` straight from disk.

Observed output:
```
generic failure propagated: StorageError - cannot write artifact: .../stages/w1.att1.md: Is a directory
run.json state         : failed
run_failed meta        : {'error_type': 'internal_error', 'message': 'cannot write artifact: ...w1.att1.md: Is a directory'}
run_cancelled events   : []
sibling w2 failure     : cancelled | unknown: True
sibling w2 message     : stage was cancelled while the run failed with an internal error
w1 state/type          : succeeded / None
synth state/type       : failed / internal_error
cancelled_pending left : []
```
Corroborating shipped test run (focused, single test id — not a repository-wide run):
```
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest \
  "tests/test_phase10_evidence_regeneration.py::test_cancelled_sibling_keeps_cancelled_cause_while_run_remains_failed_internal_error" \
  -q -p no:cacheprovider
→ .  [100%]   EXIT=0
```
**Result: CHECKED-AND-SOUND (O-2/O-3/HSF-4).** Durable artifacts assert run-level
`failed` + `internal_error`, sibling `cancelled` with `provider_side_outcome_unknown: True`,
no `run_cancelled` event, and no `cancelled_pending` left anywhere.
(Also reads: the shipped test asserts the same facts from disk, not from memory —
`tests/test_phase10_evidence_regeneration.py:831-884`.)

### C-5 — Incomplete synthesis provenance reads UNVERIFIABLE (not STALE); skipped reads NOT_APPLICABLE

Command (inline script, key operations): hand-built evidence-v2 run directories (run.json with
`evidence_version: 2`, `job.resolved.json` with `synthesis.depends_on: ["w1"]`, attempt files)
covering five variants; `storage.reconstruct_run_state(run_dir)["stages"]["synth"]["synthesis_freshness"]`.

Observed output:
```
incomplete       -> classification=UNVERIFIABLE     integrity_warning=True  warnings=['dispatched synthesis provenance is missing declared dependency binding(s) (consumed_dependencies: w1; dependency_digests: w1)']
missing-one-key  -> classification=UNVERIFIABLE     integrity_warning=True  warnings=['dispatched synthesis provenance is missing declared dependency binding(s) (dependency_digests: w1)']
skipped          -> classification=NOT_APPLICABLE   integrity_warning=False warnings=[]
stale            -> classification=STALE            integrity_warning=True  warnings=["dependency digest mismatch for 'w1' attempt 2"]
fresh            -> classification=FRESH            integrity_warning=False warnings=[]
```
**Result: CHECKED-AND-SOUND (O-1 + F-A5-1 closure).** Incomplete provenance never degrades to
STALE; a never-dispatched (SKIPPED/`dependency_failed`) synthesis is NOT_APPLICABLE with no
integrity warning; the control cases (STALE/FRESH) behave as designed.

### C-6 — A version 1 run is never mutated by any new verb

Command (inline script, key operations): build a v1 run with the legacy writers
(`create_run_tree` + `persist_usage` + `persist_run` + `persist_stage`; `evidence_version` absent —
confirmed), sha256 every file, then exercise: `regenerate_worker`, `rerun_synthesis`,
`CampaignBridge.select_attempt/prepare_regeneration/prepare_synthesis_rerun`,
`bots5 regenerate` (no consent), `bots5 rerun-synthesis` (no consent),
`bots5 inspect --attempt 7`; sha256 the tree again.

Observed output:
```
evidence_version key present: False
regenerate_worker: ValidationError: run declares evidence version 1; this operation requires evidence_version >= 2 (version 1 evidence is read-only, nothing was written)
rerun_synthesis:   ValidationError: run declares evidence version 1; ... (version 1 evidence is read-only, nothing was written)
bridge.select_attempt(w1,1): ValidationError: run declares evidence version 1; ...
bridge.prepare_regeneration: ValidationError: run declares evidence version 1; ...
bridge.prepare_synthesis_rerun: ValidationError: run declares evidence version 1; ...
cli regenerate (no consent) exit: 1      error: run declares evidence version 1; ...
cli rerun-synthesis (no consent) exit: 1 error: run declares evidence version 1; ...
cli inspect --attempt 7 exit: 1          error: artifact not found: .../stages/w1.att7.json
version-1 run tree byte-identical after all new verbs: True
files: ['.../events.jsonl', '.../run.json', '.../stages/w1.json', '.../stages/w1.md', '.../usage.json']
```
**Result: CHECKED-AND-SOUND (M-6 read-only rule).** Every new verb refuses with a typed
`ValidationError` before writing, and the digest-verified tree is unchanged.
(Carry symbol: `runner._require_v2_operation_target`, `runner.py:1281-1320`.)

### C-7 — Fence hygiene re-check with git (complements section A)

```
$ git diff HEAD --stat -- pyproject.toml db/ evidence/ examples/ src/bots5/providers/ \
      src/bots5/manifest.py src/bots5/paths.py src/bots5/core/application.py
(empty)
$ git diff --cached --stat
(empty)
$ git stash list
(empty)
```
**Result: CHECKED-AND-SOUND.** All fence `unchanged_guards` are untouched, nothing is staged,
nothing is stashed.

### C-8 — Provider-route lock (HSF-5): wrong provider class and tampered route are both refused before dispatch

Command (inline script, key operations): build a real evidence-v2 run (resolved job, attempt-1
attempt files via `persist_stage_attempt`, `selection.json`), then call `regenerate_worker`
three times: (a) positive control with the correct route/class, (b) an `OpenAICompatibleProvider`
instance presented under the approved `"openrouter"` id, (c) the correct provider object but an
`OperationSnapshot` whose `provider_route.base_url` is `https://evil.example/api/v1`.

Observed output:
```
C8a positive control: state: succeeded | attempt: 2 | provider calls: ['model-w1-alt'] | returned_model: model-w1-alt
C8b wrong class refused: ApprovalInvalidatedError - provider 'openrouter' instance does not match the approved kind 'openrouter'
     provider calls: 0 | attempt-2 absent: True
C8c tampered route refused: ApprovalInvalidatedError - operation snapshot provider route does not match the frozen route for provider 'openrouter'
     provider calls: 0 | attempt-2 absent: True
```
**Result: CHECKED-AND-SOUND (HSF-5 option 5a).** The explicit model change executes only in the
positive control; both tampering attempts refuse with typed errors before any provider call and
before any attempt file is created. Incidental observation from an intermediate run of this probe:
binding the approval to attempt 2 while the disk-derived next attempt is 1 was refused with
`ApprovalInvalidatedError: approved attempt number 2 does not match the disk-derived next attempt
number 1; the operation refuses to renumber` (`runner._require_binding_attempt_number`,
`runner.py:1615-1623`) — CHECKED-AND-SOUND, no renumbering possible.

### C-9 — Headless `inspect --attempt` (the flag with no test anywhere)

Command (inline script): craft an evidence-v2 run with `w1` attempts 1 and 2 (distinct outputs),
then `cli_main(["inspect", run_id, "w1", ...])` three ways.

Observed output (abridged to the distinguishing lines):
```
(default)      -> attempt_number: 1 | output_path: stages/w1.att1.md | --- output --- ATT1-OUTPUT     exit 0
--attempt 2    -> attempt_number: 2 | output_path: stages/w1.att2.md | --- output --- ATT2-OUTPUT     exit 0
--attempt 9    -> error: artifact not found: .../stages/w1.att9.json                                  exit 1
```
**Result: CHECKED-AND-SOUND functionally, but UNGUARDED** — `grep -rn -- "--approval" tests/`
and `grep -rn '"--attempt"' tests/` both return no hits, so neither the `inspect --attempt` flag
nor the CLI `--approval <file>` consent path has any automated test (see D-8).

### C-10 — Focused shipped-test batch (four selectors, not a repository-wide run)

```
$ QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest -p no:cacheprovider \
    "tests/test_phase10_evidence_regeneration.py::test_worker_regeneration_explicit_model_change_does_not_silently_change_provider_route" \
    "tests/test_phase10_desktop_preflight.py::test_full_run_approval_requires_exact_full_run_target_shape" \
    "tests/test_phase10_desktop_lifecycle.py::test_window_construction_without_bridge_factory_creates_no_campaign_dock" \
    "tests/test_phase10_evidence_regeneration.py::test_regeneration_refused_for_evidence_version_1_run_writing_nothing"
4 passed, 1 warning in 0.26s        EXIT=0
```
**Result:** three of the four selectors are real guards and pass (route lock; F-A2-1
target-shape closure; v1 regeneration read-only). The third selector
(`test_window_construction_without_bridge_factory_creates_no_campaign_dock`) passes but is
**vacuous** — it constructs no window and no dock (see D-5).



## D. SURVIVING DEFECTS

Severity scale: **CRITICAL** = blocks the contract's completion criterion; **HIGH** = a required
behavior is broken or its proof is false; **MEDIUM** = required behavior/proof missing but
recoverable; **LOW** = cosmetic, fragile, or documentation-grade.

### CONFIRMED DEFECTS (each with a minimal reproduction)

**D-1 — CRITICAL — The campaign dock never binds a bridge: the entire Phase 10 desktop surface is inert.**
Minimal repro = probe C-1 (three lines): construct `CampaignDockWidget(bridge_factory=...)`,
observe `dock._bridge is None`, call `_on_load_job()` → `[CampaignDock] Bridge not available`,
factory call count 0. Evidence: `campaign_dock.py:252-253` (factory stored, bridge `None`),
`grep -n "self._bridge" src/bots5/desktop/campaign_dock.py` → the ONLY assignments are 253
(`= None`) and 622 (`= None` inside `_on_clear_job`); `window.py:383-384` and
`bootstrap/desktop.py:294` only pass the factory through. Consequences: obligations 1, 2, 3, 5,
6, 8, 9, 10, 11 are not operator-reachable from the native desktop; `DesktopRuntime._close_campaigns`
(`bootstrap/desktop.py:246`) always drains an empty set, so "shutdown with an active campaign"
can never actually occur. Inside-fence: yes (`campaign_dock.py`, fence `add`).

**D-2 — HIGH — The Regenerate dialog crashes: a plain `QWidget` is used as a `QDialog`.**
Minimal repro = probe C-1 tail: with a bridge present and a stage row selected,
`dock._on_regenerate()` raises
`AttributeError: 'PySide6.QtWidgets.QWidget' object has no attribute 'accept'`.
Evidence: `campaign_dock.py:855` `ok_button.clicked.connect(dialog.accept)`,
`:856` `dialog.reject`, `:858` `dialog.exec()` — none exist on `QWidget` (the dialog is created
at ~line 849 as `QWidget(dialog_parent)`). Even after D-1 is repaired, desktop regeneration is
unreachable. Inside-fence: yes.

**D-3 — HIGH — Required "expandable worker/stage output" is not implemented in the dock.**
Minimal repro = grep from C-1: `_result_text.setPlainText` occurs only at lines 636 (`""`), 875
and 897 (`prepared.summary`); the row model carries `output_path` (line 159) but nothing consumes
it. The fence requires it (`MUTATION_FENCE.json` campaign_dock.py `why`: "expandable output") and
the design requires "expandable persisted output per attempt"
(`design/DESKTOP_SURFACE_AND_LIFECYCLE.md:68`). Headless `inspect` covers durable inspection, so
the shortfall is desktop-specific.

**D-4 — MEDIUM — Required "attempt switcher" is not operable even in principle.**
Evidence: `_render_projection` sets `setRowCount(len(rows))` with exactly one row per stage
(`campaign_dock.py:689-697`) and renders only the SELECTED attempt's number; `available_attempts`
is carried in the row model (`campaign_dock.py:153`) but never rendered as selectable choices, so
`_on_make_current` (`:811`) can only re-select the attempt that is already selected. Required by
`MUTATION_FENCE.json` campaign_dock.py `why` ("attempt switcher"). Dormant because of D-1.

**D-5 — HIGH — The desktop integration has effectively no test; two tests are vacuous.**
Evidence: `grep -rn "CampaignDockWidget\|campaign_dock" tests/` → only comment text inside
`test_window_construction_without_bridge_factory_creates_no_campaign_dock` and
`test_view_menu_unchanged_without_bridge_factory` (`tests/test_phase10_desktop_lifecycle.py:640/664`),
both of which construct neither a `MainWindow` nor a dock — they assert `bridge.job_path is None`
and even admit "(This is verified in the actual desktop code path)". `grep -rn "campaign_bridge_factory" tests/`
→ no hits, so the positive attach path (`window.py:382-393`) is also untested. This is why D-1/D-2
survived the falsification wave and both prior reviews. Requires: a widget-level test that loads a
job, validates, preflights, approves (with a counting provider factory) and drives
regenerate/rerun/cancel/New Job.

**D-6 — MEDIUM — No test for the bounded shutdown stage with an active campaign.**
Evidence: `grep -rn "_close_campaigns\|_campaign_bridge_factory" tests/` → no hits, although the
fence names "shutdown with active campaign" as a T0.8 lifecycle test
(`MUTATION_FENCE.json` test_phase10_desktop_lifecycle.py entry). In practice also unreachable (D-1).

**D-7 — MEDIUM (process evidence, not code) — No persisted T4 validation artifact yet.**
`.../pack/validation/` contains only `README.md` ("Record exact command, environment constraints,
collected count, exit code, candidate/seal identity…"); `grep -rln "1492" <pack>` → no hits. The
brief asserts a full run of 1492 passed / 1 skipped / exit 0 at this seal, but nothing in the
campaign record proves it. Contract step 14 requires it persisted after this oracle — the human
acceptor must refuse substantive acceptance until that artifact exists and names seal
`6db93b97cebcbd5e5087e564e1053cc135a82a6d7b115d9bb81657fd64a5d295`.

**D-8 — MEDIUM — Two CLI features have zero test coverage: `inspect --attempt` and `regenerate/rerun-synthesis --approval <file>`.**
Evidence: `grep -rn '"--attempt"' tests/` → no hits (exit 1); `grep -rn -- "--approval" tests/` →
no hits (exit 1). My probe C-9 shows `--attempt` behaves correctly (byte-exact attempt, fail-closed
exit 1 on a missing attempt), so this is an unguarded requirement, not a broken one. The
`--approval` path is fail-closed by construction (`cli.py:508` refuses unless the file's
`pricing_evidence` equals the freshly rebuilt evidence; the engine re-verifies every binding
regardless), but it is unexercised.

**D-9 — LOW — Dock status feedback goes to stdout, not to the operator.**
`_show_status` is `print(f"[CampaignDock] {message}")` with the comment "For now, just print"
(`campaign_dock.py:949-952`). Even after D-1/D-2 are fixed, the operator sees no status area
messages. Inside-fence: yes.

**D-10 — LOW (SUSPECTED, design-intent ambiguous) — Operator pricing entries need not affirm the route.**
`build_pricing_evidence` only cross-checks an operator-supplied `route`/`models` when the keys are
present (`runner.py`, `if source.get("route") is not None` / `if source.get("models") is not None`);
the durable evidence always records the engine-derived route and models, so HSF-1's "…together
with … route" recording requirement is met mechanically. Whether Mick intended the operator to
explicitly affirm the route is not settled by the adjudication text; flag for the acceptor rather
than calling it a violation.

**D-11 — LOW — Legacy-mode runs created after Phase 10 carry the additive `attempt_number` key.**
`StageRecord.to_dict` always emits `attempt_number` (`models.py:128+`), so a run created by
headless `bots5 run` (no `evidence_version` marker) writes `attempt_number: 1` into
`stages/<id>.json`, and `bots5 inspect` prints the whole metadata dict (`cli.py:219`). Retained
v1 directories are untouched (probe C6b) and design §3 mandates the key, so this is by design;
only the fence phrase "v1 outputs … unchanged" is strictly true for RETAINED evidence, not for
newly created legacy runs. CHECKED-AND-SOUND with this caveat.

**D-12 — LOW (SUSPECTED, durability window) — A stage can persist `state: succeeded` with an unreadable output artifact.**
Observed incidentally in probe C4: `persist_stage_attempt` writes the metadata JSON before the
`.md` mirror, so a failing output write leaves durable `state: succeeded` + an `output_path` that
cannot be read (`load_stage_view` then raises "cannot read stage output"). The run-level record
stays `FAILED` with `internal_error`, so no false success reaches the run state, but the stage
record advertises a result file that does not exist. Fragile, not falsified as a contract breach.

### REQUIREMENTS WITH NO TEST (beyond D-5/D-6/D-8)
- attempt switcher presentation (D-4) — no selector anywhere.
- expandable per-attempt output in the dock (D-3) — no selector.
- `DesktopRuntime` bounded `_close_campaigns` (D-6) — no selector.
- the two T0.10 window tests are named for requirements they do not execute (D-5).

### CHECKED-AND-SOUND (I tried to falsify these and failed)
one-shot durable approval replay (C-3), paid-approval pricing refusal at both layers (C-2),
O-2 sibling-cause truth (C-4, independent repro), O-1/UNVERIFIABLE classification (C-5),
version-1 read-only across every new verb (C-6), route/class lock (C-8), attempt renumbering
refusal (C-8 incidental), fence hygiene (C-7), seal chain (section A), F-A2-1/F-A3-1/F-A4-1/F-A5-1
closures (section B final table).



## E. LIMITATIONS AND HONEST UNKNOWNS

What this oracle did **not** verify:

1. **No repository-wide test run was executed** (explicitly forbidden by my brief). The claim
   "1492 passed, 1 skipped, exit 0 at this seal" is *asserted* by the campaign brief and is
   currently **unproven in the campaign record** (D-7). I ran only focused selectors (the single
   O-2 selector in C-4 plus the four selectors in C-10) plus my own ten probes. A human acceptor
   must see the persisted T4 artifact from contract step 14 before acceptance.
2. **No real GUI session.** I constructed `CampaignDockWidget` headlessly (offscreen QPA) and
   drove three slots directly; I never launched the full `MainWindow`/`DesktopRuntime` application,
   never rendered pixels, never clicked through menus, and never exercised a real Qt event loop
   end-to-end. D-1/D-2 are proven at widget level; other window-level interactions (menu toggling,
   multi-window docks, dock visibility/polling in the real loop) remain unverified by me.
3. **The five `asyncio.create_task(...)` calls inside Qt slots** (`campaign_dock.py:616/661/823/924/929`)
   were not executed under the application's real qasync loop. They are SUSPECTED to work under
   qasync (the loop is running while Qt dispatches), but I did not prove it; if qasync is not
   installed/active this is a second latent break (NEEDS CONFIRMATION).
4. **No network, no real providers, no real pricing sources.** Every probe used in-process spies
   subclassing the real provider classes; nothing contacted the internet; the "currentness" of
   operator-supplied rates (HSF-1) cannot be machine-verified by design and was not assessed.
5. **No concurrency/crash-durability stress.** I did not test multi-process writers, kill -9
   mid-write, fsync failure injection, or races between two operator sessions on one run
   directory (beyond reading the exclusive-create code).
6. **Pre-existing surfaces were only spot-checked.** Phase 1-9 desktop code, `window.py` outside
   its ~50-line Phase 10 delta, `bootstrap/desktop.py` outside its ~55-line delta, and the whole
   provider implementation were treated as out of scope except where the fence required zero-diff
   (which I verified with git).
7. **Historical evidence corpora** (`evidence/**`) were not re-hashed by me beyond the git
   zero-diff check; the golden-fixture backward-compat tests are the campaign's proof, and I ran
   none of them (they live inside the suite I was told not to run wholesale).
8. **Design-of-record conformance was checked at the obligation level**, not clause-by-clause
   against every design document paragraph; `design/` digests are sealed (section A) but I did not
   re-adjudicate design decisions themselves (that was the design oracle's job, seal
   `DESIGN_SEAL_v4.json`, whose digests I did recompute).
9. **Model-usage ledger / budget accounting** (4-launch MiMo cap) was not audited.

What a human acceptor should still confirm **by hand**, in order:
1. Open `src/bots5/desktop/campaign_dock.py` and run probe C-1 yourself (see F).
2. After any repair: actually launch the desktop app, load a job, validate, preflight with
   pricing, approve a paid run, watch live progress, expand a stage's output, switch attempts,
   regenerate with a model change, cancel, and press New Job — each step against the contract
   rows 1-11.
3. Demand the persisted T4 run record (exact command, collected count, exit code, seal identity,
   environment constraints) and confirm the seal id it names.
4. Confirm the pre-commit `COMMIT_BOUNDARY_REPORT.md` and that no staging/commit/push occurred
   (`git diff --cached` empty today).
5. Skim D-10/D-11/D-12 and decide whether they are acceptable for your release.



## F. ORACLE VERDICT

**FAIL.**

**Single strongest reason:** the sealed candidate's native-desktop surface is provably inert —
`CampaignDockWidget` stores its `bridge_factory` (`campaign_dock.py:252`) and never calls it, and
`self._bridge` is only ever assigned `None` (lines 253, 622) — so *none* of the Phase 10 operator
behavior (load → validate → preflight → approve → live progress → inspect output → attempt switch
→ regenerate → rerun → cancel → New Job) is reachable from the native desktop, which is the
contract's own completion criterion (CONTRACT.md lines 8–13: "the accepted Phase 10 behavior is
operator-reachable from the native desktop"), while the Regenerate control additionally crashes
with `AttributeError: 'QWidget' object has no attribute 'accept'` (`campaign_dock.py:855`) and
the fence-required "expandable output" and "attempt switcher" are not implemented at all.
Probe C-1 reproduces all of this in under 20 lines against the sealed bytes.

Mitigating context for the acceptor (does not change the verdict): the seal verifies perfectly
(section A), every adjudicated item HSF-1…HSF-5 and O-1…O-4 is genuinely implemented in the
engine (section B), ten adversarial probes found the engine layer sound (section C — pricing
refusal, one-shot replay, O-2 sibling truth, freshness classification, v1 read-only, route lock),
and the repair surface is a single in-fence file (`src/bots5/desktop/campaign_dock.py`) plus a
widget-level test.

**The one thing a human acceptor must look at first:** run probe C-1 against
`src/bots5/desktop/campaign_dock.py` — instantiate the widget, print `dock._bridge`, press Load
Job — and see for yourself that `self._bridge` is never assigned. Everything else in this report
is secondary to that fact: decide whether to authorize a bounded in-fence repair (bind the bridge
in the dock, use a real `QDialog`, render output/attempts), add the missing widget-level tests,
reseal, and rerun validation before any substantive acceptance.

