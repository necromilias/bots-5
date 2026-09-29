# HUMAN_SEMANTIC_FORK register

These are genuine product/authority choices that current authority does **not** settle. They
are presented for Mick's adjudication, not resolved by implementation preference. Each
entry states the evidence, the options, the consequences, the fence impact, and the
supervisor's recommendation. The design is implemented only after these are adjudicated at
the design gate.

Authority basis: `parcel/CONTRACT.md` (notably §"Preflight/approval integrity", §"Cost
truth"); `parcel/SCOPE.json` `human_stop_conditions`; `parcel/FINDINGS.md` F-03/F-04/F-06/
F-13/F-15; OrgMem `core/approval-boundaries.md`; OPv1 §§3–5; Qwen [X] register; MiniMax §8;
Step §9; Command §2; Gemini §5.

---

## HSF-1 — Preflight pricing authority

**Status:** genuine product fork (contract-designated). No pricing authority exists in code.

**Evidence.** `src/bots5/` contains no pricing registry, lookup, table or cache; `grep` for
`sha256|digest|pric` in the campaign package finds no pricing code. Historical
`evidence/v0.1-final-worker-boundary/*/preflight.json` pricing blocks were produced
out-of-band by a human and are read by no Python. OPv1 §4 requires a conservative preflight
upper bound immediately before a paid run. CONTRACT §"Cost truth" forbids inventing an
unapproved pricing authority.

**Options.**

| Option | Mechanism | Consequences | Fence impact |
|---|---|---|---|
| **A (recommended)** operator-entered **currently advertised** rates | Preflight modal collects input/output $/1M **for each selected model/provider route**, records observation time and the operator-cited rate source, and computes the conservative upper bound from prompt size + stage count + configured output ceilings | No network; the only branch that satisfies OPv1 §4 without a waiver; operator bears rate responsibility; auditable bound basis | none (fields on existing snapshot/approval records) |
| **B** static catalogue | Bundled `pricing.json` for known models | Offline; convenient; rates decay; needs a maintained tracked asset and policy | **expands fence** (new tracked asset) |
| **C** live provider lookup | Query provider model pricing during preflight | Always current; **violates zero-network preflight**; fails offline | crosses zero-network guarantee |
| **D** unknown + approval | Show exact token ceilings; mark dollar cost unknown; explicit approval confirms spend without a bound | **Does not satisfy OPv1's dollar-bound requirement.** Available only under an explicit Mick waiver/supersession of that requirement for this workflow | none |

**Recommendation:** A. D is **not** an equivalent compliant branch: OPv1 §4
(`docs/OPERATING_PROCEDURE_V1.md:62-75`) requires an advertised-rate observation and a
conservative upper bound; presenting D as compliant would silently relax an applicable paid
obligation. If Mick prefers D, the decision must be recorded as an explicit waiver of the
OPv1 preflight dollar bound for the desktop campaign workflow. B and C are not adopted.

**Blocking:** yes for the paid preflight UI. The design's structural shape (a
`pricing_evidence` block) is fixed either way, and paid approval UI is not implemented until
Mick decides.

---

## HSF-2 — Restart persistence of the loaded campaign/result

**Status:** genuine product fork (F-15).

**Evidence.** Obligation 8 says a completed/failed result remains the current result until
explicit `New Job`. The architecture permits desktop presentation state in the app DB while
campaign evidence stays filesystem-authoritative. The existing workspace-state seam
(`session.save_window` → `application.save_workspace_window` → `WorkspaceWindowState` →
store, migration head `0012_phase9_archive_import`) could persist a campaign pointer, but
`VALIDATION.md:62-65` warns that adding durable desktop selection state to the app DB makes
migration affected. Nothing in authority states whether "current" must survive an
application restart.

**Options.**

| Option | Mechanism | Consequences | Fence impact |
|---|---|---|---|
| **2a (recommended)** filesystem-only | Desktop keeps loaded job/run in memory for the session; on restart it opens fresh (`New Job` equivalent); evidence is never lost and is re-openable by path/id | Minimal, no migration; result is not automatically "current" across a restart | none |
| **2b** app-DB pointer | Persist loaded job path + run id + viewed attempt via the workspace-state seam | Restores the exact view after restart; requires a migration and honest migration tests | **expands fence** (migration); crosses Phase 9 migration authority |

**Recommendation:** 2a for Phase 10. If Mick wants restart restoration, it becomes an
explicitly listed fence addition in parcel-v2 with migration validation.

**Blocking:** yes — it determines whether the fence includes a migration.

---

## HSF-3 — Run discovery seam

**Status:** genuine product/UX fork (Qwen [X]5, MiniMax H-3, Step fork 5).

**Evidence.** `paths.locate_run_dir` requires an operator-supplied id/base; nothing in
`src/` enumerates a runs directory. Obligation 1 is "load an existing campaign job";
obligation 6 requires durable result inspection. No authority text specifies browsing.

**Options.**

| Option | Mechanism | Consequences | Fence impact |
|---|---|---|---|
| **3a (recommended)** explicit path/id | Operator supplies job path and, for inspection, run id/dir; existing `locate_run_dir` reused (absolute-path discipline) | Minimal; no new enumeration; slightly higher operator effort | none |
| **3b** browse runs dir | Read-only enumeration of a chosen runs directory, presented as recent runs | Better UX; introduces a new enumeration seam and a directory-listing surface | adds a read-only function (small fence addition) |

**Recommendation:** 3a. 3b is a deferred UX enhancement, not required for Phase 10
compliance.

**Blocking:** low; either satisfies the obligations.

---

## HSF-4 — Cancellation terminal vocabulary

**Status:** genuine product-semantic choice. The cancellation *mechanism* is fixed
(engine-side terminalization before the event loop tears down), but the durable outcome
vocabulary is a product choice.

**Evidence.** `runner.py:465` catches `Exception`, not `asyncio.CancelledError`
(`BaseException` in Python ≥3.8); a UI close can therefore leave `run.json` durably
`running`. `RunState` today is `running|succeeded|failed|timed_out`. Obligation 7 names
truthful "successful, failed, timed-out and partial" outcomes; it does not name
"cancelled". MiniMax argues the display must not fabricate an error type for a crash, but a
desktop-*initiated* cancel is a harness action, so recording it is truthful either way.

**Options.**

| Option | Mechanism | Consequences | Fence impact |
|---|---|---|---|
| **4a (recommended)** distinct `RunState.CANCELLED` | Add the enum value + `run_cancelled` event; in-flight stages flagged `provider_side_outcome_unknown=True`; CLI status exit 1 | Truthful distinct outcome; additive durable vocabulary; new enum value must be handled by every state consumer | `models.py`, `events.py`, `runner.py`, CLI display |
| **4b** reuse `FAILED` + `error_type="cancelled"` | No enum change | Simpler; conflates cancel with failure, but the error_type preserves the distinction | smaller, same files |

**Recommendation:** 4a — a cancellation is not a failure, and obligation 7's spirit is
truthful typed outcomes. Either branch is inside the fence.

**Blocking:** T0.6/T0.8 cannot be fully validated until this is adjudicated.

---

## HSF-5 — Explicit provider change on regeneration

**Status:** optional-expansion fork. The **default is settled**: provider route is locked,
model string changeable.

**Evidence.** CONTRACT item 9 permits an explicit **model** change and forbids a *silent*
provider-route change. F-04 requires preserving the original provider route by default and
presenting provider change as a human fork if it is desirable and not mechanically implied.

**Options.**

| Option | Mechanism | Consequences | Fence impact |
|---|---|---|---|
| **5a (recommended, default)** route locked | Regeneration may change `model` only; provider route is shown and immutable | Matches authority; narrow; no provider-contract change | none |
| **5b** explicit provider switch | Operator may choose another provider declared in the job's providers block | More flexible; requires route-consistency validation and UI; expands behavioural surface | `runner.py` + dock selector; `providers/**` still unchanged |

**Recommendation:** 5a for Phase 10.

**Blocking:** no.

---

## Not classified as forks (with reasoning)

- **Approval binding substrate** (MiniMax H-6, Step fork 2). CONTRACT explicitly requires an
  immutable or mechanically reverified execution snapshot before the first provider request
  and says to classify a fork only *if this cannot be done inside existing accepted
  semantics without a product choice*. It can: an additive frozen `PreflightSnapshot` +
  digest assertion + zero-reread dispatch requires no product choice. Therefore it is
  **mechanically settled**, not a fork. Details in `SPECIALIST_DISAGREEMENTS.md` D-1.
- **Attempt path layout** (MiniMax H-2, GLM fork). The product requirement is append-only
  siblings; the filename grammar is engineering. Flat `<id>.att<N>` satisfies the
  requirement without weakening the security containment predicate. **Mechanically
  settled**; see D-2.
