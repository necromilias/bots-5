# MIMO FINAL IMPLEMENTATION ORACLE v2 — B.O.T.S. Linux v0.1 Phase 10

RESERVED FRESH FINAL IMPLEMENTATION ORACLE (MiMo launch #5), independent and adversarial.
Reproduced everything from the worktree; **nothing in the prior reviews or the green suites was
taken on trust.**

**STATUS: complete — verdict `FAIL` (section V).**

> **Effort statement (required by brief):** the requested effort was
> `provider_maximum_if_exposed`. The harness exposes **no per-child (and no per-request)
> reasoning-effort control** — `workflow.agent()` explicitly rejects an `effort` option and the
> `subagent` tool exposes none — so **effective effort is NOT APPLIED**. This is a harness
> limitation recorded here, not a silent substitution.

---

## 0. SCOPE OF WORK: WHAT I EXECUTED vs WHAT I ONLY READ

### EXECUTED (my own code, run by me against the sealed bytes)

| Probe | File (scratch, `.../logs/demo-scratch/.oracle-v2/`) | Result |
|---|---|---|
| Independent seal/fence/git recomputation (no use of `make_candidate_seal.py`) | `independent_seal_check.py` | all green |
| Full-suite `--collect-only` (NOT a test run) | `collect_only_full.txt` | 1496 collected |
| **P1 — full operator path through the real `CampaignDockWidget`** | `p1_dock_operator_path.py` | P1a/b/e/f/h/i/j **PASS**, **P1g DEFECT** |
| **P3 — the five `asyncio.create_task` sites under the real qasync loop** | `p3_qasync_create_task.py` | all five **OK** |
| **P4 — rerun-synthesis approve, pricing refusal, local-only, legacy run, dual cost, cancellation** | `p4_adversarial.py` | mixed (see below) |
| **P5 — corrected/extended adversarial probes** | `p5_adversarial2.py` | all green |
| **P6 — the coverage the record claims but v4 does not test** (window attach, shutdown-with-active-campaign, cancelled-run durable records) | `p6_coverage_gaps.py` | behaviours **correct** |
| Three focused suites on `.venv314` | pytest | 87 / 62 / 84 passed, exit 0 |
| Byte-level proof that the recovered lifecycle file equals the sealed original | `sha256` script | **proved** |

Full raw outputs are preserved beside this report in
`work/campaign-evidence/phase10/campaign-desktop-integration/bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01/logs/demo-scratch/.oracle-v2/`
(`p1_output.txt`, `p3_output.txt`, `p4_output.txt`, `p5_output.txt`, `p6_output.txt`,
`collect_only_full.txt`).

### READ ONLY (not executed)

- The design documents, `MUTATION_FENCE.json`, the four candidate seals, the design seals,
  `COMMIT_BOUNDARY_REPORT.md`, `PHASE10_T4_VALIDATION_RECORD.md`, the first oracle
  `MIMO_FINAL_IMPLEMENTATION_ORACLE.md`, `IMPLEMENTATION_LOG.md`, `MODEL_USAGE_LEDGER.md`,
  `BUDGET_AMENDMENT_2026-09-30.md`.
- Source of the nine fenced `modify` files and the eight `add` files.
- DSH-derived recovery evidence and two pre-existing `__pycache__/*.pyc` snapshots
  (decompiled *structurally* — code-object names/line numbers/string constants — never executed).

### NOT DONE, and why

- **The repository-wide suite was not run** (explicitly forbidden; ~90 min; T4 evidence exists).
  Consequently I could not *independently* reproduce "1495 passed". I instead reconciled that
  number five independent ways (section 4).
- No real GUI session, no pixel rendering, no network, no real provider, no real pricing source.
  Every provider was an in-process subclass of the real provider class so the engine's
  route/kind/object checks would pass without a socket. **No network was touched at any point.**
- No kill -9 / fsync-failure / multi-writer stress (the D-12 window is therefore code-inspected,
  not fault-injected).

### ENVIRONMENT (used exactly as mandated)

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src:. .venv314/bin/python …
```

`.venv314` = Python 3.14.7 throughout. `.venv` (3.12.13) was used **once**, read-only, to decode a
3.12 `.pyc` header (section 3.2) — never to run tests. **Zero `RootedVfsUnsupported` failures were
observed**, which is itself the confirmation that the canonical interpreter was used.

---

## 1. MANDATED CHECK 1 — D-1: IS THE BRIDGE FACTORY ACTUALLY INVOKED AND THE DOCK OPERATIONAL? (**EXECUTED**)

Probe `p1_dock_operator_path.py`, driven end-to-end against the sealed bytes.

### P1a — counting factory over a real job tree, press Load Job

```
  dock type                      : CampaignDockWidget
  dock._bridge BEFORE Load Job   : None
  bridge_factory call count      : 0
  validate enabled before load   : False
  bridge_factory call count AFTER: 1  -> ['/tmp/oracle-v2-p1-…/.bots5/runs']
  dock._bridge                   : CampaignBridge
  isinstance CampaignBridge      : True
  job identity label             : Name: test-job | Version: 1 | Runs dir: … | Workers: 2 | Synthesis: yes | Parallel: 2
  status                         : Job loaded: …/job.json
  validate enabled               : True
  approve enabled (preflight)    : True
  runs dir exists before run     : False
  P1a RESULT: PASS
```

**The factory is called EXACTLY ONCE and a real `CampaignBridge` is bound.** D-1's original
finding (factory stored at `campaign_dock.py:252`, never called) is **repaired**.

### P1b — Validate (zero spend)

```
  status                         : Validation successful (zero spend)
  runs dir exists after validate : False
  provider constructions so far  : 0
  P1b RESULT: PASS
```

### P1c/P1d — full run with COMPLETE operator pricing evidence, approved against a deterministic
in-process provider, driven to a terminal state (NO NETWORK)

The preflight panel the operator sees carries the full binding: all three stages, execution limits,
the frozen `provider_routes`, a complete `pricing_evidence` entry
(`rate_source`, `observed_at`, `input_usd_per_1m`, `output_usd_per_1m`, derived `route`, derived
`models`, `conservative_upper_bound_usd=0.003962`, the fixed `basis` string), the
`preflight_digest`, the `approval_draft` and the spend notice.

```
  status after Approve           : Run started
  provider factory constructions : 1
  provider.complete() calls      : 3  models=['model-w1', 'model-w2', 'model-synth']
```

### P1e — projection renders stage rows, live cost line with its explicit unknown set, freshness

```
  --- stages table: rows=3
    stage=w1 | state=succeeded | attempt=1 | model=model-w1 | duration=0.0s | tokens=30 | cost=0.01 | outcome=Complete
    stage=w2 | state=succeeded | attempt=1 | model=model-w2 | duration=0.0s | tokens=30 | cost=0.01 | outcome=Complete
    stage=synth | state=succeeded | attempt=1 | model=model-synth | duration=0.0s | tokens=33 | cost=— | outcome=Incomplete
  LIVE COST LINE                 : cost: 0.02 + 1 unknown (synth) (partial)
  FRESHNESS LABEL                : Synthesis freshness: FRESH
  integrity warnings             : ''
  cancel button enabled (terminal): False
  projection.display_state       : succeeded
  projection.evidence_version    : 2
  P1e RESULT: PASS (rows=True, unknown_set=True, freshness=True)
```

The explicit unknown set is non-empty and correctly attributed (I made the synthetic provider
report `known_cost_usd=None` for the synthesis stage on purpose) — `+ 1 unknown (synth) (partial)`.

### P1f — Regenerate control no longer raises AttributeError (D-2)

```
  dialog capture  : {"dialog_class": "QDialog", "dialog_is_qdialog": "True",
                     "has_accept": "True", "has_reject": "True", "accept_call": "ok",
                     "line_edits": "1"}
  status after Regenerate prepare: Regeneration prepared for w1 with model model-w1-alt
  D-2 AttributeError             : NONE — control did not raise
  P1f RESULT: PASS
```

I patched `QDialog.exec` only to avoid blocking on a modal loop; the patch itself calls
`self.accept()` (the exact attribute that used to raise
`AttributeError: 'QWidget' object has no attribute 'accept'`) and it succeeded. **D-2's behaviour
is genuinely repaired.**

### P1g — the operator now presses Approve for that prepared regeneration — **DEFECT**

```
  status shown to operator       : "Approval failed: approve_and_start requires a full_run
                                    operation; got 'worker_regeneration'"
  attempt-2 file created         : False  ([])
  run tree changed by Approve    : False
  provider calls                 : 3   (unchanged — the three from the original full run)
  P1g RESULT: REGENERATION NOT EXECUTED BY THE DESKTOP
```

Independently reproduced for synthesis rerun in `p4_adversarial.py`:

```
  status after Rerun synthesis : 'Synthesis rerun prepared'
  prepared operation           : synthesis_rerun
  approve enabled              : True
  status after Approve         : "Approval failed: approve_and_start requires a full_run operation;
                                  got 'synthesis_rerun'"
  synthesis attempt files      : ['synth.att1.json']
  run tree changed             : False
  provider calls delta         : 0
  VERDICT: SYNTHESIS RERUN NOT EXECUTED BY THE DESKTOP <-- DEFECT
```

**This is the CRITICAL finding.** See `V-1`.

### P1h — Make current selects a DIFFERENT attempt → freshness reads STALE

```
  attempt switcher widget on row 0: QComboBox
  combo choices=['1', '2']
  selected stage after combo change: {'attempt_number': 2, …}
  FRESHNESS AFTER MAKE CURRENT   : Synthesis freshness: STALE
  durable selection.json         : {"selected_attempts": {"synth": 1, "w1": 2, "w2": 1}, …}
  P1h RESULT: PASS
```

(The second attempt had to be created through `bridge.approve_and_regenerate(...)` directly,
because the dock's Approve cannot — that is precisely the defect.)

### P1i — selected-attempt output rendered from durable storage through the bridge

```
  _result_text (first 200 chars) : 'W1-ATTEMPT-2-OUTPUT'
  durable attempt artifacts      : ['w1.att1.md', 'w1.att2.md']
  bridge.read_stage_output(...)  : 'W1-ATTEMPT-2-OUTPUT'
  P1i RESULT: PASS
```

The dock's rendering is byte-equal to `bridge.read_stage_output(stage, attempt)` — the desktop
does not parse run files itself. **D-3 is repaired in substance.**

### P1j — New Job clears the view, every run-directory file byte-identical

```
  files in run dir               : 17
  UI before: job='Name: test-job | …' run='Run: … | State: succeeded' rows=3 cost='cost: 0.02 + 1 unknown (synth) (partial)'
  UI after : job='No job loaded' run='No run active' rows=0
             cost='cost: unknown (no run active)' freshness='Synthesis freshness: N/A'
             status='' result=''
  dock._bridge after New Job     : None
  buttons: validate=False approve=False clear=False make_current=False regen=False
  run-dir files byte-identical   : True  (17 files)
  P1j RESULT: PASS
```

### Mandated check 1 — conclusion

Everything in the brief's D-1 paragraph is reachable and live **EXCEPT** the approval of a
prepared regeneration/synthesis rerun, which is unreachable (P1g). That meets the brief's own
trigger: *"If any of this is unreachable or inert, that is a candidate-changing defect."*

---

## 2. MANDATED CHECK 2 — DO THE NEW WIDGET TESTS REALLY EXERCISE THE DOCK? (**READ + JUDGED**)

All three added tests are in `tests/test_phase10_desktop_lifecycle.py`. I read each in full.

| Test | Lines | Verdict |
|---|---|---|
| `test_campaign_dock_construction_and_load_job` | 685–724 | **ADEQUATE.** Constructs a real `CampaignDockWidget`, a real counting `factory`, a real `make_job_tree` job, patches only `QFileDialog.getOpenFileName` (the file-picker, not the object under test), presses `_on_load_job()`, and asserts `len(calls) == 1`, `dock._bridge is not None`, `isinstance(dock._bridge, CampaignBridge)`, `dock._validate_button.isEnabled()`. This is a genuine exercise of the previously-escaping path. The same shape as my independent P1a, which passed. |
| `test_status_shown_in_ui_not_stdout` | 749–811 | **ADEQUATE BUT WEAK-ASSERTED.** Constructs a real dock, real job, real factory, presses Load Job, and asserts `dock._status_label.text() != ""`. The object under test is real; the assertion would also pass on a wrong message, but the D-9 behaviour (status in the UI, not stdout) is genuinely exercised. Corroborated by `grep -n "print(" src/bots5/desktop/campaign_dock.py` → **no `print()` calls remain**, so D-9 is genuinely repaired. |
| `test_regenerate_dialog_uses_qdialog_not_qwidget` | 727–746 | **INADEQUATE — it only proves the file contains the words.** |

### Honest judgement on `test_regenerate_dialog_uses_qdialog_not_qwidget`

The entire test body is:

```python
source = inspect.getsource(CampaignDockWidget._on_regenerate)
assert "QDialog" in source
assert "QDialogButtonBox" in source
assert "dialog.exec()" in source or "dialog.exec_()" in source or "dialog.open()" in source
assert "dialog.accept" in source
assert "dialog.reject" in source
```

**It never constructs a dock, never selects a row, never calls `_on_regenerate`, never creates a
dialog, and never executes a single line of the code it claims to test.** It is a source-text
assertion, i.e. exactly the "asserting on source text" anti-pattern the brief tells me to reject.

Worse, three of the five assertions would have passed on the *broken* pre-repair code: the first
oracle's repro shows the original raised at `ok_button.clicked.connect(dialog.accept)` — so
`dialog.accept`, `dialog.reject` and `dialog.exec()` were all **already present as source text**.
The only discriminating tokens are `"QDialog"` and `"QDialogButtonBox"` — and note that the string
`QDialogButtonBox` *contains* `QDialog`, so even the first assertion adds no independent signal.
A comment containing `QDialogButtonBox` inside `_on_regenerate` would satisfy the whole test while
the control crashed on every press.

**Decision: it does not constitute adequate evidence for the D-2 fix; it proves only that the file
contains those words.** I therefore rate it a test gap. It did **not** save the candidate, because
I executed D-2 myself (P1f) and the behaviour *is* correct — but the automated regression guard is
hollow, and the hollow guard replaced a behavioural one (see V-4).

None of the three tests mocks the object under test; none restates a constant.

---

## 3. MANDATED CHECK 3 — THE RECOVERED LIFECYCLE TEST FILE (**EXECUTED**)

`tests/test_phase10_desktop_lifecycle.py`, 14 tests, sha256
`f24ff5881552c19f84e6695de7dedc925935278bb09faf8ae73f994db2d27a33` — **matches the brief.**

### 3.1 The recovery is byte-perfect — proved against the seal itself

I did not trust the recovery narrative. I reconstructed the *sealed* original's identity from two
independent places:

1. `seals/implementation/PHASE10_CANDIDATE_SEAL_v2.json` →
   `tests/test_phase10_desktop_lifecycle.py = e739f3f68edb81372126ccd776480e44fb2288bc7a20ffa48f2a8d2be3314298`
   (identical in seal **v1**, in seal **v2**, and in the T0 row of `IMPLEMENTATION_LOG.md` — so the
   file never changed from T0 to v2).
2. `tests/__pycache__/test_phase10_desktop_lifecycle.cpython-312-pytest-8.4.2.pyc`, mtime
   `2026-09-29 19:58:43`, records source size **27773** and exactly the **11** original test names.

The recovered file is **27774** bytes, sha `54c6d694…`. Testing the one-byte hypotheses:

```
rec[:-1]         size=27773  sha=e739f3f68edb81372126ccd776480e44fb2288bc7a20ffa48f2a8d2be3314298  MATCH=True
rec.rstrip(b'\n') size=27772  sha=d8f9392cb…  MATCH=False
```

> **The recovered original minus its final `newline` byte is byte-identical to the file sealed in
> candidate v2.** The recovery is verbatim; the only difference is one added trailing newline.

### 3.2 The merge did not weaken a single assertion

I extracted the eleven-test block from the recovered file and from the current file and diffed them:

```
recovered 11-test block: 632 lines, 26227 chars
current   11-test block: 633 lines, 26228 chars
--- recovered
+++ current
@@ -631,2 +631,3 @@
     # when not properly configured
+
```

The only difference is one added blank line at the end (the merge separator). **Every assertion of
all eleven original tests is preserved exactly — never softened, never deleted.**
`diff .../MERGED_14_tests.py tests/test_phase10_desktop_lifecycle.py` → **IDENTICAL**.

### 3.3 All eleven required contract tests are present, by name

| Required prior contract coverage (brief) | Present |
|---|---|
| durable running stage after a crash not rendered successful, stays resumable-classified | line 52 `test_durable_running_after_crash_not_succeeded_resumable` |
| truncated stage record never rendered successful | line 100 |
| missing synthesis stage never rendered succeeded | line 149 |
| desktop cancellation persists a terminal record, not running | line 210 |
| operator cancellation not labelled a run timeout | line 302 |
| `cancelled_pending` reclassified in-process, never written as final | line 391 |
| durable `cancelled_pending` after hard kill reads interrupted/uncertain | line 482 |
| New Job clears working context without deleting historical evidence | line 581 |
| New Job does not modify run-directory bytes | line 615 |
| window construction without a bridge factory creates no campaign dock | line 641 |
| View menu unchanged without a factory | line 665 |
| **the three new tests** | `test_campaign_dock_construction_and_load_job` (685), `test_regenerate_dialog_uses_qdialog_not_qwidget` (727), `test_status_shown_in_ui_not_stdout` (749) |

Collection independently confirms **14** (`pytest --collect-only`), and the file passed **14/14**
in my focused run.

### 3.4 WERE THE TWO PREVIOUSLY-VACUOUS WINDOW TESTS STRENGTHENED OR REPLACED? — **NO**

**This mandated sub-check FAILS.** The two tests at lines 641–682 are byte-identical to the sealed
original (proved in 3.2) and are still vacuous — they construct neither a `MainWindow` nor a dock.
Line 641 still asserts only `bridge.job_path is None`, and the body still contains the admission
`(This is verified in the actual desktop code path)`.

They were **not strengthened**, **not replaced**, and (contrary to the risk the brief names) **not
silently deleted** — they are still there, still testing nothing.

### 3.5 …and I found the replacements that were lost

`tests/__pycache__/test_phase10_desktop_lifecycle.cpython-314.pyc` (plain compile, source mtime
`2026-09-30 00:33:29`, source size **46433**) survives from **before** the 00:50 truncation. It
contains **19** test code-objects — the 11 originals at *identical* line numbers (51, 99, 148, 209,
301, 390, 481, 580, 614, 640, 664, i.e. a clean append) plus **eight** appended tests:

```
  689  test_campaign_dock_construction_and_load_job     <- present in v4
  733  test_full_campaign_lifecycle_with_dock           <- ABSENT from v4
  815  test_make_current_switches_attempts             <- ABSENT from v4
  913  test_regenerate_dialog_does_not_raise           <- ABSENT (behavioural; see 3.6)
  990  test_new_job_clears_ui_without_deleting_run      <- ABSENT from v4
 1079  test_window_with_bridge_factory_creates_dock     <- ABSENT from v4  (positive attach path!)
 1107  test_window_without_bridge_factory_creates_no_dock <- ABSENT from v4 (the strengthening of the vacuous test!)
 1124  test_drain_with_active_campaign_reaches_terminal_record <- ABSENT from v4 (fence T0.8 / D-6!)
```

Structural decompilation of those code-objects confirms they were real behavioural tests, e.g.
`test_window_with_bridge_factory_creates_dock` has `co_names = ('PySide6.QtWidgets','QApplication',
'bots5.desktop.window','MainWindow','instance','findChildren','QDockWidget','len','isVisibleTo',
'isVisible')` and `test_drain_with_active_campaign_reaches_terminal_record` drives
`_on_load_job → _on_validate → _on_approve → asyncio.run(dock.drain)`.

Corroboration: `grep -rn campaign_bridge_factory tests/ --include=*.py` → **no hits**, while
`grep -c campaign_bridge_factory tests/__pycache__/test_phase10_desktop_lifecycle.cpython-314.pyc`
→ **3 hits**; likewise the drain test name appears **5 times** in that pyc and **0 times** in the
v4 pytest pyc.

`.pytest_cache/v/cache/lastfailed` (mtime 00:34) additionally records
`tests/test_phase10_desktop_lifecycle.py::test_full_campaign_lifecycle_with_dock` as a **failure**,
so at least one of the eight was not green when it existed.

**Net: seven of those eight tests are absent from candidate v4.** The v4 file = the 11 sealed
originals + 1 carried + 2 brand-new (`…_uses_qdialog_not_qwidget`, `…_status_shown_in_ui_not_stdout`)
that never existed pre-clobber.

Honest qualification: those eight were appended **after** the first oracle (00:33 > 00:25), were
**never sealed** (v3 was sealed at 00:53, after the 00:50 truncation), and were **never gated**.
So they are not "sealed coverage that was lost" — they are **intended coverage that repair wave 3
wrote, then destroyed, and the recovery did not restore.** The consequence that matters is not the
file count: it is that (a) the two vacuous window tests were never fixed, (b) the positive attach
path is still untested, and (c) the fence's own named T0.8 "shutdown with active campaign" still has
no test at all.

### 3.6 The D-2 guard was **downgraded** from behavioural to source-text

The pre-clobber `test_regenerate_dialog_does_not_raise` (line 913, docstring *"Regenerate dialog
uses QDialog and doesn't raise AttributeError"*) has
`co_names = (…, 'setRowCount','setItem','QTableWidgetItem','selectRow','_on_regenerate')` — it
built a table, selected a row and **called `_on_regenerate`**. Its v4 replacement never calls
anything. Whatever that test's own fate, the regression guard for D-2 went from *execute* to
*read source*.

---

## 4. MANDATED CHECK 4 — IS THE 1495-TEST T4 EVIDENCE ATTRIBUTABLE TO EXACT CANDIDATE V4? (**EXECUTED where permitted**)

### 4.1 What the record says

`validation/PHASE10_T4_VALIDATION_RECORD.md` "Run 4 — final T4 on corrected candidate v4
(authoritative)": command identical to run 1
(`QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest --no-header -p no:cacheprovider`),
candidate `PHASE10_CANDIDATE_SEAL_v4`, candidate_id `0b046836…fc17`, seal sha256
`02fbcce4…002`, **1495 passed, 1 skipped, 42591 warnings in 5298.92s (1:28:18), exit 0**.
`logs/IMPLEMENTATION_LOG.md:401` and `reports/COMMIT_BOUNDARY_REPORT.md:51` restate the same
numbers. Run 3 (v3, 1484) is explicitly marked *"SUPERSEDED, do not cite"*.

### 4.2 Five independent reconciliations

1. **Seal identity.** All three documents name the **same** candidate_id and seal sha256 as the
   brief and as the seal file I hashed myself. Nothing in any document names a different seal for
   run 4.
2. **Timeline.** Seal v4 written `2026-09-30 02:33:55.920 +1000`. Run duration `5298.92s`
   = 1 h 28 m 18.9 s → ends ≈ `04:02:14`. The three records were written at `04:03:40`,
   `04:03:43`, `04:04:14` and `EVIDENCE_INDEX_FINAL.json` at `04:04:17`. The run is sandwiched
   exactly between sealing and recording. Consistent, not fabricable by accident.
3. **Zero drift across the run window.** I stat'ed all 17 fenced files: **the latest mtime is
   `02:33:44` (the lifecycle test file, 11 s before the seal)**. *No fenced file was touched after
   the run started*, and the seal verifies with zero drift **now** — so the bytes tested at
   02:33–04:02 are the bytes I verified at review time.
4. **Arithmetic.** I diffed the seal file maps myself:
   ```
   v2 -> v3 changed: ['src/bots5/core/campaign.py', 'src/bots5/desktop/campaign_dock.py',
                      'tests/test_phase10_desktop_lifecycle.py']
   v3 -> v4 changed: ['tests/test_phase10_desktop_lifecycle.py']
   v2 -> v4 changed: ['src/bots5/core/campaign.py', 'src/bots5/desktop/campaign_dock.py',
                      'tests/test_phase10_desktop_lifecycle.py']
   ```
   **No other test file differs between v2 and v4.** The lifecycle file was 11 tests at v2 (its
   digest is the one I proved equals `rec[:-1]`) and 14 at v4. Therefore
   **1492 + 3 = 1495** exactly, with the three deltas being exactly the three added widget tests.
5. **Independent collection.** `pytest --collect-only -q` (a collection, not a run) over the whole
   tree gives per-file counts summing to **1496 = 1495 passed + 1 skipped**. The six Phase 10 files
   sum to **87** (15+16+14+17+10+15), matching the record's "87 passed".

### 4.3 Seal recomputation

```
$ python3 …/seals/implementation/make_candidate_seal.py verify …/PHASE10_CANDIDATE_SEAL_v4.json
seal sha256 match: True
file drift: none
EXIT=0
```

My **independent from-scratch script** (does not import the campaign script) additionally confirms:

| Item | Result |
|---|---|
| seal self-sha256 (recomputed) | `02fbcce47185dd83645febe14faab5808027f68593de3edfd806441626a29902` == sidecar == brief ✅ |
| all 17 `files` digests recomputed | **drift: none** ✅ |
| `candidate_id` = sha256(canonical JSON of `files`) | `0b04683646f14ab66b71aa381aa506ffc82e5aa9fbb4e3a29ec9ca65346ffc17` == brief ✅ |
| fence path set (9 `modify` + 8 `add`) vs seal `files` keys | identical, 17 paths, symmetric diff = none ✅ |
| `design_seal_v4_sha256` | `9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1` ✅ |
| `mutation_fence_sha256` | `602e6d5d3a88eb893d11d064dfc28c70e8f692c52cb823cd0c8b2711907cf452` ✅ |
| `head_commit` | `0756904481ae884bb9e864e8e1e11fc4a27a72ff` ✅ |
| `tracked_modified_out_of_fence` / `fence_violations` | `[]` (recomputed from live `git status`) ✅ |
| all four design seal sidecars | `sha256sum -c` → OK, OK, OK, OK ✅ |

### 4.4 Verdict for check 4

**CONFIRMED.** The 1495/1/exit-0 evidence is attributable to exact candidate v4; the arithmetic
reconciles; the seal verifies with zero drift after that run. Two honest caveats:

- **There is no persisted raw pytest transcript** for run 4 — only the prose record (which is what
  `validation/README.md` actually asks for: command, environment constraints, collected count,
  exit code, seal identity, applicability). The collected count is given as `passed + skipped`
  rather than explicitly. I could not re-run the suite to re-derive 1495 (forbidden), so the *pass*
  claim itself remains an attestation — but every independent check I could perform agrees with it.
- **`logs/IMPLEMENTATION_LOG.md:404` and `PHASE10_T4_VALIDATION_RECORD.md:70` say "all eight
  destroyed lifecycle tests were restored"** while line 55 of the same document correctly says
  **eleven** were deleted. Eight is the *net* delta (−11 +3). Documentation error only, but it is
  in the document that carries the authoritative gate arithmetic (finding V-11).

---

## 5. MANDATED CHECK 5 — D-10, D-11 AND D-12, IN THEIR EXACT RECORDED FORM

Reproduced verbatim from `reports/oracle/MIMO_FINAL_IMPLEMENTATION_ORACLE.md` §D, then inspected
the actual code (and executed where noted).

### D-10 — *recorded:* "LOW (SUSPECTED, design-intent ambiguous) — Operator pricing entries need not affirm the route."
> `build_pricing_evidence` only cross-checks an operator-supplied `route`/`models` when the keys are
> present (`runner.py`, `if source.get("route") is not None` / `if source.get("models") is not None`);
> the durable evidence always records the engine-derived route and models, so HSF-1's "…together
> with … route" recording requirement is met mechanically. Whether Mick intended the operator to
> explicitly affirm the route is not settled by the adjudication text; flag for the acceptor rather
> than calling it a violation.

**Code inspected** — `src/bots5/runner.py:472` and `:474`, both guarded by `is not None`.
**Executed**: my P1c preflight panel shows the durable `pricing_evidence` entry carrying the
engine-derived `route` and `models` regardless; P5b shows a *supplied but tampered* `route` **is**
refused (`pricing route identity does not match live route 'openrouter'`, refused at prepare,
nothing written); P5b also shows a supplied-but-mismatched `models` would be refused by the same
mechanism. The route is additionally locked at dispatch by the provider-object/kind/base_url checks
(`runner.py:352-373` "(7) Provider-object route validation", `_require_operation_provider_route` at `:1478-1523`) and shown to the operator in the preflight panel before Approve.

**Classification: `DOCUMENTED_LIMITATION_ACCEPTABLE` (LOW).**
Not an operator-visible defect: the operator *sees* the route in the preflight panel, the durable
record *contains* the route and models, mismatch is refused when supplied, and the dispatch-time
object lock is independent of operator affirmation. Requiring an affirmative operator route field
would change accepted semantics (HSF-1's recording requirement is met mechanically by design), so
this is a deliberate accept-or-change question for Mick, not a defect I can repair.

### D-11 — *recorded:* "LOW — Legacy-mode runs created after Phase 10 carry the additive `attempt_number` key."
> `StageRecord.to_dict` always emits `attempt_number` (`models.py:128+`), so a run created by
> headless `bots5 run` (no `evidence_version` marker) writes `attempt_number: 1` into
> `stages/<id>.json`, and `bots5 inspect` prints the whole metadata dict (`cli.py:219`). Retained
> v1 directories are untouched (probe C6b) and design §3 mandates the key, so this is by design;
> only the fence phrase "v1 outputs … unchanged" is strictly true for RETAINED evidence, not for
> newly created legacy runs. CHECKED-AND-SOUND with this caveat.

**EXECUTED (P4d)** — `run_job(job, providers)` with **no snapshot and no approval**:

```
  run_job(no snapshot/approval) result : RunResult state=succeeded
  run.json evidence_version present    : False
  complete() calls                     : 1
  artifacts                            : ['w1.json', 'w1.md']
  stage has attempt_number key         : True value=1
  stage has pricing keys               : []
  preflight.json written on legacy run : False
  VERDICT: legacy run executed with no approval gate required: YES
```

**Classification: `DOCUMENTED_LIMITATION_ACCEPTABLE` (LOW).**
The behaviour is real, but: (a) the design of record *mandates* the key unconditionally
(`models.py:121-123` cites `CAMPAIGN_EVIDENCE_EVOLUTION.md §3`, and `IMPLEMENTATION_LOG.md`
records it as a deliberate conformance correction); (b) it is purely **additive** — no existing
`to_dict` key changed, so every legacy reader still parses the record; (c) **retained** v1 evidence
is untouched, which the zero-diff guards plus `test_phase10_backward_compat.py` (15/15, run by me)
corroborate; (d) no `preflight.json`, no pricing fields and no approval gate leak into the legacy
path (P4d). Resolving the literal fence phrase would require changing an accepted design §3
decision — i.e. it is a caveat on the fence wording, not a defect.

### D-12 — *recorded:* "LOW (SUSPECTED, durability window) — A stage can persist `state: succeeded` with an unreadable output artifact."
> `persist_stage_attempt` writes the metadata JSON before the `.md` mirror, so a failing output
> write leaves durable `state: succeeded` + an `output_path` that cannot be read … the stage record
> advertises a result file that does not exist. Fragile, not falsified as a contract breach.

**Code inspected — the premise is TRUE.** `src/bots5/storage.py:240-250`:

```python
payload = _dumps_json(stage.to_dict(), meta_path)
if create:  _exclusive_write_json(meta_path, payload)   # line 244
else:       _atomic_write(meta_path, payload)           # line 248
if text is not None:
    atomic_write_text(dirs.root / output_rel, text)     # line 250  <-- can fail AFTER the meta says succeeded
```

`stage.output_path` is set at line 239, i.e. it is already inside `payload` when line 244/248 runs.

**Judgement: this IS a real, narrow durability defect** — a durable stage record can be internally
inconsistent (`state: succeeded` + `output_path` pointing at a file that does not exist), and the
operator then sees a succeeded stage whose output panel reports
`Cannot read stage output: …`. I did **not** fault-inject it (that is outside what I was asked to
do), so I confirm the *mechanism* from code, not from observation.

Mitigations that cap the severity at LOW: the run-level record does not adopt the lie (the first
oracle observed the run staying `FAILED`/`internal_error`), `load_stage_view` fails closed rather
than returning fabricated content, and it requires an I/O failure in a narrow window. Note also
that the naive fix (write `.md` first) would break the **intentional** invariant documented at
lines 242-243 — for `create=True` the metadata claim must precede any output so that an overwrite
refusal leaves the directory byte-identical. A correct in-fence fix is therefore constrained to the
`create=False` (in-place terminal update) path, or to a post-write verification/removal.

**Classification: `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` (LOW).** It is a real defect, and
`storage.py` is a fenced `modify` file, so a careful fix stays inside the existing fence — but at
LOW severity it would **not** on its own block acceptance.

---

## 6. MANDATED CHECK 6 — NO TRACKED DRIFT AFTER CANDIDATE SEALING (**EXECUTED**)

Exact commands and output:

```
$ git rev-parse HEAD
0756904481ae884bb9e864e8e1e11fc4a27a72ff
                                                          == fence baseline_head / seal head_commit / brief ✅

$ git diff --name-only HEAD
src/bots5/bootstrap/desktop.py
src/bots5/cli.py
src/bots5/desktop/window.py
src/bots5/errors.py
src/bots5/events.py
src/bots5/models.py
src/bots5/runner.py
src/bots5/storage.py
src/bots5/usage.py
$ git diff --name-only HEAD | wc -l
9
                                                          exactly the nine fenced `modify` files ✅

$ git diff --cached --name-only
[empty]                                                   nothing staged ✅

$ git stash list
[empty]                                                   nothing hidden ✅

$ git status --porcelain
 M src/bots5/bootstrap/desktop.py
 M src/bots5/cli.py
 M src/bots5/desktop/window.py
 M src/bots5/errors.py
 M src/bots5/events.py
 M src/bots5/models.py
 M src/bots5/runner.py
 M src/bots5/storage.py
 M src/bots5/usage.py
?? .audit-tmp4/
?? .verifyrun/
?? .verifyrun2/
?? AUDIT-2026-09-28-evidence-only-review.md
?? src/bots5/core/campaign.py
?? src/bots5/desktop/campaign_dock.py
?? tests/test_phase10_backward_compat.py
?? tests/test_phase10_cross_cutting.py
?? tests/test_phase10_desktop_lifecycle.py
?? tests/test_phase10_desktop_preflight.py
?? tests/test_phase10_desktop_projection.py
?? tests/test_phase10_evidence_regeneration.py
?? work/
                                                          tracked M == 9, all in fence; tracked-modified-out-of-fence == [] ✅
```

The four pre-existing untracked scratch/audit entries (`.audit-tmp4/`, `.verifyrun/`,
`.verifyrun2/`, `AUDIT-2026-09-28-evidence-only-review.md`, plus the `work/` evidence tree) were
already untracked at `HEAD` — they are not modifications to any tracked path.

**The eight fenced `add` files exist:**

```
OK  src/bots5/core/campaign.py                    66466 bytes
OK  src/bots5/desktop/campaign_dock.py            45473 bytes
OK  tests/test_phase10_backward_compat.py         24726 bytes
OK  tests/test_phase10_cross_cutting.py           32050 bytes
OK  tests/test_phase10_desktop_lifecycle.py       32389 bytes
OK  tests/test_phase10_desktop_preflight.py       30246 bytes
OK  tests/test_phase10_desktop_projection.py      15626 bytes
OK  tests/test_phase10_evidence_regeneration.py   38919 bytes
```

**Zero-diff guards byte-identical to HEAD:**

```
$ git diff --name-only HEAD -- pyproject.toml db/ evidence/ examples/ src/bots5/providers/ \
      src/bots5/manifest.py src/bots5/paths.py src/bots5/core/application.py \
      src/bots5/core/execution.py src/bots5/core/import_queue.py src/bots5/core/events.py
[empty]                                                     ✅

$ git diff --name-only HEAD -- src/bots5/desktop/session.py src/bots5/desktop/bridge.py \
      src/bots5/desktop/widgets.py src/bots5/desktop/theme.py src/bots5/desktop/profile.py \
      src/bots5/desktop/phase9.py src/bots5/desktop/phase9_dialogs.py \
      src/bots5/desktop/phase9_imports.py src/bots5/desktop/phase9_queue_dock.py
[empty]                                                     ✅  (Phase 9 desktop untouched; only the fenced window.py changed)
```

`git ls-files src/bots5/desktop/` confirms the Phase 9 set is `__init__, bridge, phase9,
phase9_dialogs, phase9_imports, phase9_queue_dock, profile, session, theme, widgets, window` — ten
of eleven untouched, `window.py` fenced.

Final re-run of `make_candidate_seal.py verify` at the end of my review:
`seal sha256 match: True / file drift: none / EXIT=0`, and
`tests/test_phase10_desktop_lifecycle.py` still hashes to `f24ff588…27a33`.

**I modified, staged, committed or deleted nothing.** My only writes are inside the permitted
scratch directory and this report.

---

## 7. ADVERSARIAL PROBES

### 7.1 The five `asyncio.create_task(...)` sites under the real qasync loop — **FALSIFICATION ATTEMPTED, SURVIVED**

`qasync` is a hard dependency (`pyproject.toml:14`) and `bootstrap/desktop.py:1021-1031` really
does `from qasync import QEventLoop; asyncio.set_event_loop(QEventLoop(qt_application))`.
`qasync/__init__.py:390-410` calls `asyncio.events._set_running_loop(self)` **before** `app.exec()`
and clears it in `finally` — so the loop is marked running for the whole Qt dispatch.

I did not take that on faith. `p3_qasync_create_task.py` builds a real `QEventLoop`, `dock.show()`s
the widget offscreen (`isVisible() == True`), and invokes each of the five sites **from Qt's own
event dispatch** via `QTimer.singleShot(0, …)` — i.e. exactly how a button click reaches them:

```
  running loop inside coroutine               : QSelectorEventLoop
  646 _on_approve create_task                 : OK status='Run started'   (hosted task running: True)
  698 _on_poll_timeout create_task            : OK (no RuntimeError)
  1045 _on_visibility_changed create_task     : OK poll_timer_active=True
  918 _on_make_current create_task            : OK status='Made w1 attempt 1 current'
  1040 _on_cancel create_task                 : OK status='Cancel requested'
  SITES FAILING UNDER QASYNC: NONE
```

(The "FAILING" line in the first run of this probe was a **false positive of my own matcher** — I
tested for the substring `RuntimeError` inside a value that read `"OK (no RuntimeError)"`. I fixed
the matcher and re-ran.)

**Verdict: the first oracle's suspicion (E-3) was wrong — this is NOT a defect and NOT even a test
gap in practice.** The mechanism is benign: qasync installs the running loop for the entire
`app.exec()`, and every slot executes inside it. I will note one *residual* nuance: none of this is
asserted by any repository test, but since the behaviour is an asyncio/qasync framework guarantee
rather than candidate logic, I do not raise it as a finding.

*Side observation from the same run:* my first P3 attempt produced `run state: failed` rather than
`cancelled`. That turned out to be **my own probe's fault** — `make_job_tree` sets a per-stage
`timeout_seconds: 1.0` and my synthetic synthesis slept 1.2 s, so the stage timed out before I
cancelled. I re-ran cancellation in P5f/P6 with per-stage timeouts raised, and the cancellation
matrix is truthful (7.3).

### 7.2 Approve with INCOMPLETE pricing evidence on a paid route — **REFUSED, NOTHING WRITTEN** (EXECUTED)

`p5_adversarial2.py`, six distinct incomplete/tampered inputs:

```
  no evidence block at all                  : refused at approve -> paid approval requires complete operator pricing evidence
  entries present but rate_source missing   : refused at prepare -> pricing rate_source is required for 'openrouter'
  entries present but observed_at missing   : refused at prepare -> pricing observed_at is required for 'openrouter'
  empty entries list                        : refused at prepare -> pricing evidence must cover exactly every paid route in use
  route identity mismatch (tampered base_url): refused at prepare -> pricing route identity does not match live route 'openrouter'
  negative rate                             : refused at prepare -> pricing rates must be finite non-negative numbers for 'openrouter'
  runs dir exists                           : False
  whole tmp tree byte-identical             : True
  attempt files anywhere                    : []
  run.json files anywhere                   : []
  provider factory constructions            : 0
  provider.complete() calls                 : 0
  approval markers written                  : []
  VERDICT: REFUSED BEFORE ANY RUN TREE / ATTEMPT / PROVIDER CALL, NOTHING WRITTEN
```

Five of the six are refused **during `prepare_full_run`** (before Approve is even possible); the
no-evidence case is refused at Approve, still before any provider construction. **Confirmed.**

### 7.3 Local-only operation exempt; legacy plain run verb untouched — **CONFIRMED** (EXECUTED)

P5c (schema-v2 `local_openai` job, `pricing_evidence=None`):
`approval.pricing_evidence = None`, `paid=False`, run `state=succeeded`,
`provider.complete() calls = 2` (1 worker + 1 synthesis), and `preflight.json` contains
`"pricing_evidence": null` — **no pricing/rate fields are invented for a local route.**

P4d (legacy path, section 5/D-11): `run_job` with **no snapshot and no approval** executes to
`succeeded`, writes **no** `evidence_version`, **no** `preflight.json`, **no** pricing keys.

`cli.py`'s `--attempt` (line 65) and `--approval` (lines 89, 113) exist but **still have zero test
coverage** (V-10).

### 7.4 Dual cost accounting — **CONSISTENT; the stored selected copy really is only a cache** (EXECUTED)

P5e: run attempt 1 at $0.01, then regenerate the same worker at $0.50.

```
per_attempt costs          : {'w1.att1': '0.01', 'w1.att2': '0.50'}
cumulative_spend (durable) : 0.51     projection: 0.51
selected_spend stored cache : 0.01
selected_spend DERIVED      : 0.01
aggregate (legacy mirror)   : 0.01
integrity warnings          : []
selection.json              : {"selected_attempts": {"w1": 1}, …}
cumulative(0.51) > selected(0.01) because attempt 2 (0.50) is NOT selected : True

--- after Make current -> attempt 2 (expensive) ---
cumulative_spend (durable) : 0.51     projection: 0.51
selected_spend stored cache : 0.01                       <-- lags, as designed
selected_spend DERIVED      : 0.50                       <-- follows the selection
integrity warnings          : ['usage.json selected_spend cache disagrees with the derived
                                selected spend; the derived value is authoritative']

--- after tampering ONLY usage.json["selected_spend"] to "999.99" ---
projection DERIVED          : 0.50                       <-- derived wins
integrity warnings          : ['…the derived value is authoritative']
VERDICT: DERIVED SELECTED SPEND IS AUTHORITATIVE; STORED COPY IS ONLY A CACHE
```

`cumulative_spend` is invariant under selection (financial truth across every attempt);
`selected_spend` is derived from `per_attempt` + `selection.json` and **overrides a tampered stored
copy while emitting an integrity warning**. `aggregate` mirrors the stored selected summary for
legacy consumers. **F-06/F-07 behave exactly as designed.**

### 7.5 Cancellation matrix and durable `cancelled_pending` truthfulness — **CONFIRMED** (EXECUTED)

P5f (operator cancel while stages are in flight) and P6b/P6c:

```
run.json state                     : cancelled                    ✅ terminal, not running
event kinds                        : [… stage_failed(cancelled_pending) … stage_failed(cancelled)
                                      run_cancelled]              ✅ pending → reclassified in-process
lines mentioning cancelled_pending : only in the append-only event narrative
durable attempt records            : failure.type = "cancelled" for all three stages
                                       w1/w2: started_at != null → provider_side_outcome_unknown = true
                                       synth: started_at == null → provider_side_outcome_unknown = false
any stage labelled run_timed_out   : False                        ✅ operator cancel ≠ run timeout
display_state                      : cancelled
hard-kill: run.json forced to "running", stages forced to "cancelled_pending"
display_state                      : interrupted_uncertain        ✅ never succeeded/resumable/failed
VERDICT: cancellation matrix truthful: YES
```

`bridge.cancel()` → `_verify_durable_terminal` is reached from disk and raised nothing.
`cancelled_pending` appears **only** in the append-only event log (a narrative, never the
correctness source) — the final durable stage label is `cancelled`.

**One truthfulness defect in this area** (V-5): for that same cancelled run —

```
display_state             : cancelled
synthesis_freshness       : UNVERIFIABLE
integrity warnings        : ['dispatched synthesis attempt has absent provenance
                              (consumed_dependencies/dependency_digests)']
synth record              : started_at = null, failure.type = "cancelled"
```

— the warning asserts a **dispatched** attempt while `started_at` is `null`, and the design says
the opposite (`design/REGENERATION_AND_STALE_SYNTHESIS.md:11` *"skipped / never-dispatched
synthesis attempts are NOT_APPLICABLE, not UNVERIFIABLE"*; `:183` *"… **no integrity warning**"*).
`storage.py:918` only short-circuits on `state == "skipped"`, so a run cancelled before synthesis
dispatch falls through to Rule 3. This happens on **every** operator cancellation that occurs while
synthesis is still pending.

---

## 8. DISPOSITION OF THE FIRST ORACLE'S DEFECTS (what I actually verified)

| ID | First oracle | My status (executed unless noted) |
|---|---|---|
| **D-1** | CRITICAL — dock never binds a bridge | **REPAIRED** (P1a: factory called once, real `CampaignBridge` bound, full operator path runs) |
| **D-2** | HIGH — Regenerate crashes (`QWidget.accept`) | **REPAIRED behaviourally** (P1f) — but its regression guard is a source-string assertion (V-3) |
| **D-3** | HIGH — no expandable output | **REPAIRED in substance** (P1i: per-attempt durable output rendered via `bridge.read_stage_output`, byte-equal). The panel is capped at 200 px (V-9) |
| **D-4** | MEDIUM — attempt switcher not operable | **REPAIRED** (P1h: `QComboBox ['1','2']`, Make current → durable `selection.json` → freshness `STALE`) |
| **D-5** | HIGH — no real desktop test; two vacuous | **PARTIALLY REPAIRED** — 2/3 widget tests are genuine; the D-2 guard is not (V-3); the two window tests are still vacuous and the positive attach path still untested (V-4) |
| **D-6** | MEDIUM — no shutdown-with-active-campaign test | **STILL OPEN** — behaviour correct (P6b: `dock.drain()` → `succeeded`; `DesktopRuntime._close_campaigns` → `cancelled` in 0.002 s, bridge set emptied) but **still zero test coverage** (V-4) |
| **D-7** | MEDIUM — no persisted T4 artifact | **CLOSED** — `validation/PHASE10_T4_VALIDATION_RECORD.md` exists, names seal v4, gives command/env/counts/exit code; independently reconciled (section 4) |
| **D-8** | MEDIUM — `inspect --attempt`, `--approval` untested | **STILL OPEN** (`grep -rn -e '--attempt' -e '--approval' tests/*.py` → no hits) — V-10 |
| **D-9** | LOW — status to stdout | **REPAIRED** — `grep -n "print(" src/bots5/desktop/campaign_dock.py` → **no `print()` calls**; dock shows status in `_status_label` (P1/P3) |
| **D-10** | LOW | accepted — V-7 |
| **D-11** | LOW | accepted — V-8 |
| **D-12** | LOW | real, narrow durability defect, in-fence — V-6 |
| **E-3** | unknown — create_task under qasync | **FALSIFIED** — all five sites work (7.1) |

---

## V. ORACLE VERDICT

# **FAIL**

**Single strongest reason:** the sealed candidate's native-desktop surface still cannot execute two
of the operations it exists to expose. `CampaignDockWidget._on_approve`
(`src/bots5/desktop/campaign_dock.py:643`) unconditionally calls
`self._bridge.approve_and_start(self._prepared_operation)`; `approve_and_start` refuses anything
whose `operation != "full_run"` (`src/bots5/core/campaign.py:1241-1243`); yet `_on_regenerate`
(`:964`) and `_on_rerun_synthesis` (`:987`) both store their `worker_regeneration` /
`synthesis_rerun` prepared operation in that same field and then **enable the Approve button**.
The bridge's correct entry points, `approve_and_regenerate` (`campaign.py:1268`) and
`approve_and_rerun_synthesis` (`campaign.py:1312`), are **never called from any desktop code** —
`grep -rn "approve_and_regenerate\|approve_and_rerun_synthesis" --include=*.py src/ tests/` hits
only `campaign.py` itself and two engine-level tests.

I executed it: prepare a regeneration → status `"Regeneration prepared for w1 with model …"`,
Approve enabled → press Approve → operator sees
`"Approval failed: approve_and_start requires a full_run operation; got 'worker_regeneration'"`,
**zero** provider calls, **zero** new bytes, no attempt file. Identical for synthesis rerun.
Combined with V-2 (both controls are also *disabled* whenever the selected stage is `succeeded`),
**obligations 9 and 11 — selected-worker regeneration and synthesis rerun — are not reachable from
the native desktop in any state.** The contract's completion criterion ("the accepted Phase 10
behavior is operator-reachable from the native desktop") is therefore not met, which is the same
class of failure that made the first oracle fail this candidate.

This is repairable **inside the existing fence** (`campaign_dock.py` is a fence `add` file):
dispatch on `prepared.operation` in `_on_approve`.

**Strongly mitigating, and stated plainly:** everything else I executed passed. The bridge factory
really is invoked exactly once; validate/preflight/approve/terminal-render/cost-unknown-set/
freshness/Make-current→STALE/durable-output/New-Job-with-zero-byte-changes all work; D-2 does not
crash; the qasync `create_task` suspicion was wrong; pricing refusal writes nothing; the local-only
exemption and the legacy run verb are intact; dual cost accounting is sound with the stored copy
proved to be a cache; the cancellation matrix is truthful; the recovered lifecycle file is
**byte-identical to the sealed original minus one newline**; the seal recomputes with zero drift and
zero fence violations; HEAD is exactly `0756904481ae884bb9e864e8e1e11fc4a27a72ff` with exactly the
nine fenced modifications, nothing staged, all eight adds present, and every zero-diff guard
byte-identical to HEAD; and the 1495/1/exit-0 T4 evidence reconciles to candidate v4 five
independent ways.

**Per `logs/BUDGET_AMENDMENT_2026-09-30.md`:** launch #5 returns FAIL, therefore **no launch #6 is
authorized** by that amendment as written ("If launch #6 finds another candidate-changing defect …
STOP FOR MICK" presupposes a #5 that found an in-fence repairable defect and returned
PASS/PASS_WITH_LIMITATIONS). Finding a CRITICAL in-fence repairable defect under a FAIL verdict
exceeds the amendment's granted authority, so **STOP FOR MICK** at the same pre-commit boundary.
Nothing was staged, committed, pushed, ref-mutated, OrgMut-mutated, or dependency-changed; the
mutation fence was never expanded.

---

## FINDINGS LIST

> Severity scale (as recorded by the first oracle): **CRITICAL** = blocks the contract's completion
> criterion; **HIGH** = a required behavior is broken or its proof is false; **MEDIUM** = required
> behavior/proof missing but recoverable; **LOW** = cosmetic, fragile, or documentation-grade.
> Each finding carries exactly one classification.

### V-1 — **CRITICAL** — The desktop Approve button never dispatches a regeneration or a synthesis rerun; both operations are prepared, enabled, then always refused.

- **File / line:** `src/bots5/desktop/campaign_dock.py:636-654` (esp. **:643**
  `self._bridge.approve_and_start(self._prepared_operation)`), `:964` (`_on_regenerate` stores a
  `worker_regeneration` op), `:987` (`_on_rerun_synthesis` stores a `synthesis_rerun` op),
  `:970`/`:993` (Approve enabled for those ops); against
  `src/bots5/core/campaign.py:1241-1243` (refuses non-`full_run`),
  `:1268` `approve_and_regenerate`, `:1312` `approve_and_rerun_synthesis` (never called from the
  desktop).
- **Exact reproduction (EXECUTED, `p1_dock_operator_path.py`):**
  1. `CampaignDockWidget(bridge_factory=…)` over a real `make_job_tree` job; load; validate;
     supply complete pricing evidence.
  2. `dock._stages_table.selectRow(0)`; patch `QDialog.exec`; `dock._on_regenerate()`; type
     `model-w1-alt`; return Accepted.
  3. Observe `status = "Regeneration prepared for w1 with model model-w1-alt"`,
     `dock._prepared_operation.operation == "worker_regeneration"`,
     `dock._approve_button.isEnabled() == True`.
  4. `dock._on_approve()`.
  5. Observe `status = "Approval failed: approve_and_start requires a full_run operation; got
     'worker_regeneration'"`; `stages/w1.att2.json` does **not** exist; run tree hash unchanged;
     provider call count unchanged (3).
  Reproduced identically for synthesis rerun (`p4_adversarial.py`, `p4_output.txt`): status
  `"Approval failed: approve_and_start requires a full_run operation; got 'synthesis_rerun'"`,
  `synthesis attempt files = ['synth.att1.json']`, `run tree changed = False`,
  `provider calls delta = 0`.
  Static corroboration: `grep -rn "approve_and_regenerate|approve_and_rerun_synthesis" --include=*.py src/ tests/`
  → only `campaign.py` and two engine-level tests (`test_phase10_evidence_regeneration.py:215/217,
  790/792`), never `campaign_dock.py`.
- **Why it matters to an operator:** the campaign exists to let an operator regenerate a worker
  with a different model, or re-run synthesis after staleness, from the desktop. The operator does
  everything right, sees "prepared", sees Approve light up, presses it, and gets a refusal naming an
  internal contract — no attempt is created, no provider is called, no evidence is written, and
  there is no desktop path that works instead. The expensive attempt they just authorised is
  silently never executed. Obligations 9 and 11 are dead at the surface.
- **Classification:** `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` — the fix is a dispatch in
  `_on_approve` (`campaign_dock.py` is a fence `add` file); the correct bridge methods already
  exist in the same sealed tree.

### V-2 — **HIGH** — Regenerate and Rerun-synthesis are disabled whenever the selected stage is `succeeded`, so after a normal successful campaign neither control can be pressed at all.

- **File / line:** `src/bots5/desktop/campaign_dock.py:840-841`
  (`setEnabled(selected["state"] not in ("running", "succeeded"))`), reached from
  `_sync_stage_controls` (`:829`) at the end of every `_render_projection`.
- **Exact reproduction (EXECUTED, `p1_f`/`p1_output.txt`):**
  ```
  regenerate button enabled      : False  (stage state = succeeded)
  rerun-synthesis button enabled : False
  engine prepare_regeneration on a SUCCEEDED stage: ACCEPTED (operation=worker_regeneration, attempt=2)
  ```
  i.e. the **engine has no such precondition** — `prepare_regeneration` and `regenerate_worker`
  accept a `succeeded` target (their documented preconditions are evidence version, stage
  membership, job match, approval scope, route identity, byte match, one-shot — none is stage
  state) — while the **UI refuses to offer the control**. After a fully successful run every row is
  `succeeded`, so both controls are dead on every row. The coupling of "Rerun synthesis" to the
  *selected worker row's* state is also incoherent: after a successful regeneration the selected
  attempt for that worker is still `succeeded`, so the desktop cannot perform the very rerun its
  own staleness indicator now demands.
- **Why it matters to an operator:** the natural Phase 10 workflow is *run succeeded → try a
  different model for one worker* / *staleness now says STALE → rerun synthesis*. Both are blocked
  by a UI gate the design does not contain (`design/DESKTOP_SURFACE_AND_LIFECYCLE.md` §2.2 lists
  "explicit 'Regenerate worker' … explicit 'Rerun synthesis' with stale/fresh indication" with no
  state precondition), while the headless CLI offers both. It also masks V-1 for the common case.
- **Classification:** `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` — one/two lines in
  `campaign_dock.py`, inside the fence; does not touch accepted engine semantics.

### V-3 — **MEDIUM** — The D-2 regression guard is an `inspect.getsource` string assertion; it proves only that `_on_regenerate`'s source contains the words "QDialog"/"QDialogButtonBox".

- **File / line:** `tests/test_phase10_desktop_lifecycle.py:727-746`
  (esp. `:737` `inspect.getsource(CampaignDockWidget._on_regenerate)`, `:740-746` five substring
  assertions).
- **Exact reproduction / evidence (READ, then judged):** the test body quoted in full in section 2.
  It never instantiates `CampaignDockWidget`, never selects a row, never calls `_on_regenerate`,
  never constructs a dialog. Three of its five assertions (`"dialog.exec()"`, `"dialog.accept"`,
  `"dialog.reject"`) are present **verbatim in the broken pre-repair source** — the first oracle's
  repro raised *at* `ok_button.clicked.connect(dialog.accept)`, so those strings already existed;
  and `"QDialog"` is a substring of `"QDialogButtonBox"`, so assertion 1 adds no independent
  signal. A comment containing `QDialogButtonBox` would satisfy the entire test while the control
  crashed on every press. The pre-clobber version of this test
  (`test_regenerate_dialog_does_not_raise`, pyc line 913, `co_names` ends `… 'selectRow',
  '_on_regenerate'`) actually **called** `_on_regenerate`; v4 replaced execution with source
  reading.
- **Why it matters to an operator:** nothing directly — **I executed D-2 myself (P1f) and the
  behaviour is genuinely correct** (`dialog_class=QDialog`, `accept` callable and callable-able,
  no `AttributeError`). The risk is to the next change: the guard that is supposed to keep the
  Regenerate control working will pass against a reintroduced `QWidget`, so a crash can return
  without any test failing. This is the same failure mode (a proof that does not test the thing)
  that let D-1 survive two reviews and a green 1492-test suite.
- **Classification:** `TEST_GAP` (behaviour is correct but unverified by the automated guard).

### V-4 — **MEDIUM** — The two previously-vacuous window tests were neither strengthened nor replaced; the positive attach path and the fence-named T0.8 "shutdown with active campaign" still have no test; seven tests that existed before the clobber were not restored.

- **File / line:**
  - `tests/test_phase10_desktop_lifecycle.py:641-682` — the two vacuous tests, byte-identical to
    the sealed original (proved: the eleven-test block differs by exactly one trailing blank line,
    and `recovered[:-1]` sha256 == seal v1/v2 digest `e739f3f6…`).
  - Missing: `test_window_with_bridge_factory_creates_dock`,
    `test_window_without_bridge_factory_creates_no_dock`,
    `test_drain_with_active_campaign_reaches_terminal_record`,
    `test_full_campaign_lifecycle_with_dock`, `test_make_current_switches_attempts`,
    `test_regenerate_dialog_does_not_raise`, `test_new_job_clears_ui_without_deleting_run`.
  - Untested seams: `src/bots5/desktop/window.py:381-393` (positive attach),
    `src/bots5/bootstrap/desktop.py` `_close_campaigns`.
- **Exact reproduction / evidence (EXECUTED where stated):**
  - *Read:* `tests/__pycache__/test_phase10_desktop_lifecycle.cpython-314.pyc`, source mtime
    `2026-09-30 00:33:29`, source size **46433**, contains **19** test code-objects — the 11
    originals at identical line numbers plus the 8 listed above (section 3.5). Their `co_names`
    prove they were behavioural (`MainWindow`/`findChildren`/`QDockWidget`; `_on_approve` →
    `asyncio.run(dock.drain)`).
  - *Read:* `.pytest_cache/v/cache/lastfailed` records
    `…::test_full_campaign_lifecycle_with_dock` as a **failure**, so at least one of the eight was
    never green.
  - *Grep:* `grep -rn campaign_bridge_factory tests/ --include=*.py` → **no hits**; the same token
    appears 3× in the pre-clobber pyc. `grep -rn "_close_campaigns|_campaign_bridge_factory" tests/`
    → **no hits**.
  - *Executed (proving the behaviour is nonetheless correct):* `p6_coverage_gaps.py` →
    ```
    WITHOUT factory: campaignDock children = 0 ; View menu = ['Import Queue'] ; no campaign_dock_action
    WITH factory   : campaignDock children = 1 ; visible = [False] ; type = CampaignDockWidget
                       View menu = ['Import Queue', 'Campaign'] ; menu removed/changed = []
    VERDICT: WINDOW ATTACH BEHAVIOUR CORRECT (but untested in v4)

    b1 durable run state after dock.drain() : succeeded   (drain with a hosted run)
    b2 _close_campaigns elapsed             : 0.002s (budget 30s)
    b2 durable run state                    : cancelled   ; bridge set emptied = True
    VERDICT: bounded close stage drains to a durable terminal record
    ```
  - *Executed:* the Phase 10 six-file suite passes **87/87**, including 14/14 in this file.
- **Why it matters to an operator:** no operator is harmed *today* — I executed both missing
  behaviours and they are correct. What is missing is the guard: `MUTATION_FENCE.json` names
  "shutdown with active campaign" as T0.8 coverage **of this exact file**, and the first oracle's
  D-5 explicitly required the vacuous tests to be fixed; v4 leaves both requirements unmet while
  its test count rises 1492 → 1495, so the *rising* count reads as proof of the repair. A future
  regression in the attach seam or the bounded close stage would be invisible.
- **Classification:** `TEST_GAP` (behaviour is correct but unverified).

### V-5 — **MEDIUM** — A run cancelled before synthesis dispatch is reported as `UNVERIFIABLE` with an integrity warning that falsely asserts a *dispatched* attempt.

- **File / line:** `src/bots5/storage.py:915-947`; the short-circuit at **:918**
  `if meta.get("state") == "skipped":` only; the warning text emitted at **:941-944** —
  `"dispatched synthesis attempt has absent provenance (consumed_dependencies/dependency_digests)"`.
- **Exact reproduction (EXECUTED, `p6_coverage_gaps.py` / `p5_adversarial2.py`):** start a paid run
  with 2 workers + synthesis, cancel at t=0.6 s (workers in flight, synthesis never started), then
  `project_run(run_dir)`:
  ```
  synth.att1.json : {"started_at": null, "state": "failed",
                     "failure": {"type": "cancelled",
                                 "message": "run cancelled before the stage reached a terminal state",
                                 "provider_side_outcome_unknown": false},
                     "consumed_dependencies": <absent>, "dependency_digests": <absent>}
  display_state             : cancelled
  synthesis_freshness       : UNVERIFIABLE
  integrity warnings        : ['dispatched synthesis attempt has absent provenance
                                (consumed_dependencies/dependency_digests)']
  ```
  The dock renders exactly that (`_freshness_label`, `_integrity_warnings_label`,
  `campaign_dock.py:782-793`).
- **Why it matters to an operator:** they pressed Cancel, and the surface then tells them there is
  an **evidence-integrity problem** — while asserting the synthesis attempt was dispatched when
  `started_at` is `null` and no provider was ever called for it. Both statements are false. The
  design of record says the opposite:
  `design/REGENERATION_AND_STALE_SYNTHESIS.md:11` *"skipped / never-dispatched synthesis attempts
  are NOT_APPLICABLE, not UNVERIFIABLE"* and `:183` *"synthesis never ran, so provenance is not
  applicable — **no integrity warning**"*; `design/DESKTOP_SURFACE_AND_LIFECYCLE.md:160` likewise
  keys on *"skipped / never dispatched"*. This is the N-3 repair of design v3→v4 implemented only
  for the literal `skipped` state, so it misses the most common never-dispatched case of all.
  It never fabricates freshness or success, which is why it is MEDIUM and not HIGH.
- **Classification:** `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` — `storage.py` is a fenced `modify`
  file; keying Rule 1 on `started_at is None` (or at minimum correcting the false "dispatched"
  wording) conforms to, rather than changes, the accepted design.

### V-6 — **LOW** — D-12 confirmed: a stage can persist `state: succeeded` with an unreadable output artifact (metadata written before the `.md` mirror).

- **File / line:** `src/bots5/storage.py:239-250` — `stage.output_path` set at `:239`, metadata
  written at `:244`/`:248`, output written at `:250`.
- **Exact reproduction / evidence:** code inspection (the premise the first oracle recorded is
  accurate). **Not fault-injected** — I did not simulate an I/O failure, so I confirm the mechanism,
  not an observation. The comment at `:242-243` documents why the claim must precede output for
  `create=True` (an overwrite refusal must leave the directory byte-identical), which is why a
  naive reorder is not available.
- **Why it matters to an operator:** if the `.md` write fails, the durable stage advertises
  `state: succeeded` with an `output_path` that cannot be read, so the dock shows a green stage and
  then `Cannot read stage output: …` when they expand it. The run-level record does not adopt the
  lie (it stays `FAILED`/`internal_error`) and `load_stage_view` fails closed, which is what caps
  this at LOW.
- **Classification:** `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` — `storage.py` is fenced; a correct
  fix is available on the `create=False` in-place path or via post-write verification. LOW: would
  **not** on its own block acceptance.

### V-7 — **LOW** — D-10 accepted: operator pricing entries need not affirm the route/models.

- **File / line:** `src/bots5/runner.py:472`, `:474` (both guarded by `is not None`).
- **Evidence (EXECUTED):** P1c preflight panel shows the durable entry carries the **engine-derived**
  `route` and `models` regardless of what the operator supplied; P5b shows a supplied-but-tampered
  `route` is refused at prepare with `pricing route identity does not match live route 'openrouter'`
  and nothing written; the dispatch-time object/kind/`base_url`/`api_key_env` lock
  (`runner.py:352-373` "(7) Provider-object route validation", `_require_operation_provider_route` at `:1478-1523`) is independent of operator affirmation.
- **Why it matters to an operator:** it does not. The operator sees the route in the preflight panel
  before approving; the durable evidence records it; a mismatch is refused when supplied.
- **Classification:** `DOCUMENTED_LIMITATION_ACCEPTABLE`.

### V-8 — **LOW** — D-11 accepted: legacy-mode runs created after Phase 10 carry the additive `attempt_number` key.

- **File / line:** `src/bots5/models.py:167` (unconditional), comment at `:121-123` citing design
  §3; surfaced by `src/bots5/cli.py` `inspect`.
- **Evidence (EXECUTED, `p4_d`):** `run_job(job, providers)` with no snapshot/approval →
  `state=succeeded`, `evidence_version` **absent**, `attempt_number: 1` present, **no** pricing
  keys, **no** `preflight.json`. Retained v1 evidence untouched — zero-diff guards verified with
  git (section 6) and `tests/test_phase10_backward_compat.py` 15/15 by me.
- **Why it matters to an operator:** only if they diff `inspect` output between an old run and a
  newly created legacy run; the extra key is additive, every legacy reader still parses the record,
  and the design of record mandates it unconditionally. Strictly, the fence phrase *"v1 outputs …
  unchanged"* holds for **retained** evidence but not for **newly created** legacy runs — a caveat
  on the fence wording, not a defect. Resolving it would mean reversing design §3.
- **Classification:** `DOCUMENTED_LIMITATION_ACCEPTABLE`.

### V-9 — **LOW** — D-13 (disposition of first-oracle D-3): output is rendered per attempt, but the panel is hard-capped at 200 px, so the fence's "expandable output" is scroll-only.

- **File / line:** `src/bots5/desktop/campaign_dock.py:475-478`
  (`QTextEdit`, `setReadOnly(True)`, **`setMaximumHeight(200)`**), rendered at `:866`/`:1020`.
- **Evidence (EXECUTED):** P1i proves the content is genuinely the durable per-attempt output via
  `bridge.read_stage_output` (`'W1-ATTEMPT-2-OUTPUT'` for attempt 2) — so the substance of D-3 is
  repaired. The widget's `maximumHeight(200)` means the operator cannot enlarge the panel; they can
  only scroll inside it.
- **Why it matters to an operator:** a long worker output is readable only 200 px at a time in the
  desktop; the headless `inspect` verb remains the full-fidelity reader. Reading "expandable" as
  "revealable on demand per attempt" (which the design's §2.2 phrasing also supports), this is
  satisfied; reading it literally as resizable, it is not.
- **Classification:** `DOCUMENTED_LIMITATION_ACCEPTABLE` (flagged because the fence uses the literal
  word "expandable"; not inflated beyond LOW).

### V-10 — **MEDIUM** — D-8 still open: `inspect --attempt` and `regenerate|rerun-synthesis --approval <file>` have zero test coverage.

- **File / line:** `src/bots5/cli.py:65` (`--attempt`), `:89` and `:113` (`--approval`);
  coverage check `grep -rn -e '--attempt' -e '--approval' tests/*.py` → **NO HITS** (unchanged
  since the first oracle).
- **Evidence (READ):** the flags exist in the CLI; no test exercises them. The first oracle's
  probe showed `--attempt` behaves correctly (byte-exact attempt, fail-closed exit 1 on a missing
  attempt), and the `--approval` path is fail-closed by construction, so this remains an *unguarded*
  requirement rather than a broken one.
- **Why it matters to an operator:** the headless parity surface the campaign added can regress with
  a green suite. Low blast radius (the engine re-verifies every binding regardless), which is why it
  stays MEDIUM rather than HIGH.
- **Classification:** `TEST_GAP`.

### V-11 — **LOW** — The authoritative gate documents contradict themselves on how many lifecycle tests were destroyed ("eight" vs "eleven").

- **File / line:** `logs/IMPLEMENTATION_LOG.md:404` (*"all eight destroyed lifecycle tests were
  restored"*) and `validation/PHASE10_T4_VALIDATION_RECORD.md:70` (*"confirming that the eight
  destroyed tests were restored"*) vs `PHASE10_T4_VALIDATION_RECORD.md:55` (*"deleting **eleven**
  contract tests"*).
- **Evidence (READ):** `PHASE10_CANDIDATE_SEAL_v3` covers a 3-test lifecycle file (collected total
  1484) while v2 covers 11 (1492): 11 removed, 3 added → net −8. Eleven were destroyed; eight is
  the net delta. Both statements appear in the same document.
- **Why it matters to an operator:** it is documentation-grade, but it sits in the document that
  carries the authoritative T4 arithmetic, and it understates the loss by three tests — which is
  exactly the number whose fate section 3 of this report had to reconstruct from a `.pyc`.
- **Classification:** `DOCUMENTED_LIMITATION_ACCEPTABLE` (documentation accuracy only; no code or
  behaviour involved).

---

### Summary table

| ID | Severity | File:line | Classification |
|---|---|---|---|
| **V-1** | **CRITICAL** | `campaign_dock.py:643` (+`:964`, `:987`) vs `campaign.py:1241-1243` | `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` |
| **V-2** | **HIGH** | `campaign_dock.py:840-841` | `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` |
| **V-3** | MEDIUM | `tests/test_phase10_desktop_lifecycle.py:727-746` | `TEST_GAP` |
| **V-4** | MEDIUM | `tests/test_phase10_desktop_lifecycle.py:641-682`; missing attach/drain tests | `TEST_GAP` |
| **V-5** | MEDIUM | `storage.py:918`, `:941-944` | `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` |
| **V-6** | LOW | `storage.py:239-250` (D-12) | `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE` |
| **V-7** | LOW | `runner.py:472`,`:474` (D-10) | `DOCUMENTED_LIMITATION_ACCEPTABLE` |
| **V-8** | LOW | `models.py:167` (D-11) | `DOCUMENTED_LIMITATION_ACCEPTABLE` |
| **V-9** | LOW | `campaign_dock.py:478` (D-3 residue) | `DOCUMENTED_LIMITATION_ACCEPTABLE` |
| **V-10** | MEDIUM | `cli.py:65`,`:89`,`:113` (D-8) | `TEST_GAP` |
| **V-11** | LOW | `IMPLEMENTATION_LOG.md:404`, `PHASE10_T4_VALIDATION_RECORD.md:70` | `DOCUMENTED_LIMITATION_ACCEPTABLE` |

Counts: 1 CRITICAL, 1 HIGH, 4 MEDIUM, 5 LOW. Four `CANDIDATE_CHANGING_IN_FENCE_REPAIRABLE`
(V-1, V-2, V-5, V-6 — all inside existing fence paths), three `TEST_GAP` (V-3, V-4, V-10), four
`DOCUMENTED_LIMITATION_ACCEPTABLE` (V-7, V-8, V-9, V-11). **No `SEMANTIC_OR_FENCE_PROBLEM`
findings: every defect I raise is repairable without touching the fence or reversing an accepted
design decision.**

### Mandated checks — completion statement

| # | Mandated check | Completed? |
|---|---|---|
| 1 | D-1 bridge factory invoked + dock operational, full operator path | **YES — EXECUTED** (P1a–P1j). One sub-step (approve-after-regenerate) is unreachable → V-1 |
| 2 | New widget tests really exercise the dock; judge the `inspect.getsource` test | **YES** — 2/3 adequate; the `inspect.getsource` test judged **inadequate** → V-3 |
| 3 | Recovered lifecycle file: full prior contract + 3 new, no loss, no softening; vacuous tests strengthened/replaced | **PARTIAL — 3 of 4 sub-checks PASS, 1 FAIL.** Recovery proved byte-perfect against the seal; all 11 originals + 3 new present with zero assertion change; **the "strengthened or replaced" sub-check FAILS** → V-4 |
| 4 | 1495 T4 attributable to candidate v4; arithmetic; seal verifies after the run | **YES** — five independent reconciliations + independent seal recomputation. Caveat: no raw pytest transcript exists (stated, not hidden) |
| 5 | D-10 / D-11 / D-12 in exact recorded form, classified | **YES** — V-7, V-8, V-6 |
| 6 | No tracked drift after sealing | **YES — EXECUTED** — HEAD, 9 modified, nothing staged, 8 adds, all zero-diff guards byte-identical |
| — | Adversarial: qasync `create_task` ×5 | **YES — EXECUTED under a real `QEventLoop`; first oracle's suspicion FALSIFIED** |
| — | Adversarial: incomplete pricing / local-only / legacy verb | **YES — EXECUTED** |
| — | Adversarial: dual cost accounting | **YES — EXECUTED** |
| — | Adversarial: cancellation matrix / `cancelled_pending` | **YES — EXECUTED** (one defect found → V-5) |

**Nothing was modified, staged, committed or deleted in the repository. The seal is unchanged and
re-verifies. The campaign remains at the sealed pre-commit boundary, now requiring Mick's decision.**
