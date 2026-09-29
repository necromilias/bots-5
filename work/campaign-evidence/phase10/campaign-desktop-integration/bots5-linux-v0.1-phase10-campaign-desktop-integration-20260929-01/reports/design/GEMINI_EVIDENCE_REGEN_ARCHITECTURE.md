# Phase 10 Campaign Evidence, Regeneration, and Stale-Synthesis Architecture

**Author**: Gemini 3.8 Flash (Phase 10 Architecture Specialist)  
**Parcel**: `bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01`  
**Document Path**: `reports/design/GEMINI_EVIDENCE_REGEN_ARCHITECTURE.md`  
**Baseline HEAD**: `0756904481ae884bb9e864e8e1e11fc4a27a72ff` (tree `7fe879035a01a87339dfe6319a435fa0e84c94bd`)  
**Status**: Design Proposal (Pre-Mick Gate, Read-Only Parcel v1)  
**Authorities Consulted**:  
- `parcel/CONTRACT.md` (locked product obligations and exclusions)  
- `parcel/SCOPE.json`, `BASELINE.json`, `FINDINGS.md`, `VALIDATION.md`, `SOURCES.md`  
- `MODEL_ROUTING_EVIDENCE.md`  
- `reports/design/QWEN_CAMPAIGN_ARCHAEOLOGY.md` (authoritative engine reconstruction)  
- `reports/design/COMMAND_OPERATOR_AUTHORITY_UX.md` (operator authority and UX analysis)  
- Pinned repository bytes in `src/bots5/` and `tests/`

---

## 0. Executive Summary & Core Architectural Seams

### 0.1 The Architectural Mandate
Phase 10 introduces a thin native desktop operational surface over the existing B.O.T.S. campaign engine while strictly preserving independent headless CLI usability, zero-spend validation, append-only sibling worker regeneration, selected/current attempt identity, mechanical staleness evaluation for dependent synthesis, and historical run evidence readability.

The existing campaign engine (`src/bots5/models.py`, `storage.py`, `runner.py`, `usage.py`) encodes a strict **one-record-per-stage** model (`StageRecord`, `stages/<stage_id>.json`, `stages/<stage_id>.md`, `result.md`, `run.json["stages"]`). In-place overwrite of these files to support regeneration is **flatly forbidden** by `CONTRACT.md:47`, `OPERATING_PROCEDURE_V1.md:128-137`, and financial audit integrity: overwriting a stage record destroys the only durable proof of a dispatched, potentially billable provider request whose outcome may be uncertain (`provider_side_outcome_unknown = True`).

This architecture delivers a narrow, additive, append-only extension that:
1. Retains historical V0/V0.2 single-attempt runs as fully readable without mutation (`evidence_version: 1`).
2. Introduces versioned sibling attempt persistence (`evidence_version: 2`) within the same run directory (`stages/<stage_id>.att<N>.json` and `stages/<stage_id>.att<N>.md`).
3. Establishes `selection.json` as the sole, authoritative, filesystem-reconstructable representation of "currently selected attempt," completely eliminating UI memory or database coupling for campaign truth.
4. Records explicit provenance (`consumed_dependencies` and SHA-256 `dependency_digests`) in synthesis attempt records, making staleness a **mechanical, bidirectional set-comparison predicate**.
5. Resolves the Time-Of-Check to Time-Of-Use (TOCTOU) approval vulnerability by binding approval cryptographically to an immutable in-memory `PreflightSnapshot` and persisted `preflight.json`.
6. Enforces dual cost accounting in `usage.json` (Cumulative Financial Spend vs Selected Pipeline Cost) without fabricating live token-dollar streaming.
7. Closes the lifecycle gap where window close with an active campaign previously left `run.json` hanging in `state: "running"`.

### 0.2 System Seam Decomposition

```text
┌────────────────────────────────────────────────────────────────────────────────┐
│                       DESKTOP OPERATIONAL SURFACE                              │
│  ┌─────────────────────────┐  ┌───────────────────────┐  ┌──────────────────┐  │
│  │   CampaignDock / View   │  │ Preflight & Approval  │  │ Attempt Selector │  │
│  │  (Projection From Disk) │  │  Modal (TOCTOU Bound) │  │  & Staleness UI  │  │
│  └────────────▲────────────┘  └───────────▲───────────┘  └────────▲─────────┘  │
└───────────────┼───────────────────────────┼───────────────────────┼────────────┘
                │ 250ms Polling / Bridge    │ Approval Token        │ Selection Command
┌───────────────▼───────────────────────────▼───────────────────────▼────────────┐
│                    CAMPAIGN ENGINE & RUNNER CORE SEAM                          │
│                                                                                │
│  1. Zero-Spend Validation: manifest.load_job() + validate_referenced_files()   │
│  2. Preflight Snapshot: SHA-256 Digest of inputs, prompts, models, limits      │
│  3. Approval Assertion: runner verifies approval.digest == snapshot.digest     │
│  4. Execution: run_job() / regenerate_worker() / rerun_synthesis()             │
│  5. Shutdown Terminalizer: traps CancelledError -> writes FAILED/ABORTED       │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                        │ Atomic writes & Appends
┌───────────────────────────────────────▼────────────────────────────────────────┐
│               AUTHORITATIVE FILESYSTEM EVIDENCE DIRECTORY                      │
│                                                                                │
│  <run-id>/                                                                     │
│    ├── run.json                  [evidence_version: 2, aggregate state]        │
│    ├── job.resolved.json         [canonical absolute paths & routing config]   │
│    ├── preflight.json            [immutable snapshot, hashes, approval record] │
│    ├── selection.json            [authoritative selected attempt mapping]      │
│    ├── usage.json                [cumulative spend + selected pipeline cost]   │
│    ├── events.jsonl              [append-only timeline with fsync lock]        │
│    └── stages/                                                                 │
│         ├── w1.att1.json / .md   [immutable worker attempt 1]                  │
│         ├── w1.att2.json / .md   [sibling worker attempt 2 (regeneration)]     │
│         ├── synth.att1.json/.md  [synthesis attempt 1 (records consumed deps)] │
│         └── synth.att2.json/.md  [synthesis attempt 2 (rerun)]                 │
└────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Evidence Schema Evolution & Versioning (`evidence_version: 2`)

### 1.1 Directory Topology and Sibling Attempt Storage Layout
To support sibling attempts while strictly avoiding in-place overwrites, the run directory layout is extended additively. Historical run directories (`evidence_version: 1`, implicit) and Phase 10 run directories (`evidence_version: 2`, explicit) coexist under `<runs_dir>/<run_id>/`.

```text
<RUNS_DIR>/<run-id>/
  ├── run.json                    # Aggregated run record; carries "evidence_version": 2
  ├── job.resolved.json           # Canonical resolved job manifest
  ├── preflight.json              # NEW: Preflight snapshot, hashes, and approval binding
  ├── selection.json              # NEW: Authoritative mapping of stage_id -> selected attempt
  ├── usage.json                  # Enhanced: per-attempt breakdown + cumulative & selected totals
  ├── events.jsonl                # Append-only audit log; new event types added to EVENT_TYPES
  ├── result.md                   # Mirrored output of the currently selected synthesis attempt
  └── stages/
       ├── <worker_id>.att1.json  # Worker attempt 1 metadata
       ├── <worker_id>.att1.md    # Worker attempt 1 output markdown
       ├── <worker_id>.att2.json  # Sibling worker attempt 2 (regenerated) metadata
       ├── <worker_id>.att2.md    # Sibling worker attempt 2 output markdown
       ├── <synth_id>.att1.json   # Synthesis attempt 1 metadata (with consumed_dependencies)
       ├── <synth_id>.att1.md     # Synthesis attempt 1 output markdown
       ├── <synth_id>.att2.json   # Sibling synthesis attempt 2 (rerun) metadata
       └── <synth_id>.att2.md     # Sibling synthesis attempt 2 output markdown
```

### 1.2 Naming Grammar and Attempt Identification
1. **Stage Identity**: Validated by `paths.STAGE_ID_RE` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, `src/bots5/paths.py:11`).
2. **Attempt Number**: An integer $N \ge 1$, sequentially assigned per stage starting at 1.
3. **Attempt File Basenames**:
   - Metadata: `<stage_id>.att<N>.json` (e.g., `adversary.att1.json`, `adversary.att2.json`).
   - Output: `<stage_id>.att<N>.md` (e.g., `adversary.att1.md`, `adversary.att2.md`).
4. **Direct Containment Guarantee**:
   All attempt output markdown files reside directly within `stages/`.
   Therefore, for any attempt output:
   $$\text{Path}(run\_dir / output\_path).parent == (run\_dir / "stages").resolve()$$
   This **completely preserves** the path containment assertion in `storage.load_stage_view` (`storage.py:197`) without weakening or altering security path-traversal guards!

### 1.3 `StageRecord` Dataclass Evolution
`StageRecord` (`src/bots5/models.py:89-145`) is additively extended with four fields (all defaulting to backward-compatible values):

```python
@dataclass
class StageRecord:
    id: str
    provider: str
    requested_model: str
    state: StageState = StageState.QUEUED
    returned_model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None
    request_id: str | None = None
    output_path: str | None = None
    finish_reason: str | None = None
    completion_complete: bool | None = None
    error_type: str | None = None
    error_message: str | None = None
    provider_side_outcome_unknown: bool = False
    
    # Phase 10 additions:
    attempt_number: int = 1
    consumed_dependencies: dict[str, int] = field(default_factory=dict)
    dependency_digests: dict[str, str] = field(default_factory=dict)
    preflight_digest: str | None = None
```

In `StageRecord.to_dict()`:
- `"attempt_number": self.attempt_number` is emitted.
- If `consumed_dependencies` is non-empty (synthesis), `"consumed_dependencies"` and `"dependency_digests"` are emitted.
- If `preflight_digest` is present, `"preflight_digest"` is emitted.

### 1.4 Backward-Compatibility Contract with Legacy V0 / V0.2 Runs
Existing historical runs (`evidence/v0.1-*`) lack `evidence_version`, `selection.json`, and `.att<N>` suffixes. The engine readers (`src/bots5/storage.py`) are evolved with a strict fallback discipline:

```python
def load_run_view(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    run = read_json(run_dir / "run.json")
    usage = read_json(run_dir / "usage.json")
    version = run.get("evidence_version", 1)
    
    order = run.get("stage_order", [])
    stages: list[dict[str, Any]] = []
    
    if version == 1:
        # Legacy V0/V0.2 reader: exactly matches pinned baseline logic
        for stage_id in order:
            validate_stage_id(stage_id)
            stages.append(read_json(run_dir / "stages" / f"{stage_id}.json"))
    else:
        # Phase 10 (version >= 2): load selected attempts
        selection = load_selection(run_dir)
        for stage_id in order:
            validate_stage_id(stage_id)
            att_num = selection.get(stage_id, 1)
            stage_path = run_dir / "stages" / f"{stage_id}.att{att_num}.json"
            if not stage_path.exists():
                # Fallback to legacy filename if mixed
                stage_path = run_dir / "stages" / f"{stage_id}.json"
            stages.append(read_json(stage_path))
            
    return run, stages, usage
```

`load_stage_view(run_dir, stage_id, attempt_number=None)` operates identically:
- If `attempt_number` is explicitly passed: reads `stages/<stage_id>.att<attempt_number>.json`.
- If `attempt_number` is `None`: reads the selected attempt from `selection.json` for Phase 10 runs, or `stages/<stage_id>.json` for legacy V0 runs.
- **Result**: Zero regression on all existing tests (`tests/test_runner.py:127-137`, `tests/test_cli_views.py:86-103`), and all 76 historical tracked evidence files remain 100% readable.

### 1.5 Grounded Proof: Why Historical Evidence Cannot Be Overwritten
1. **Financial and Accounting Immutability**:
   `provider_side_outcome_unknown` (`src/bots5/models.py:110`) flags requests that timed out or encountered network failures after dispatch. Overwriting `stages/<stage_id>.json` with a retry would destroy the forensic record of token consumption and potential provider billing.
2. **Audit Verification Integrity**:
   `evidence/**` files have SHA-256 digests permanently committed and cited in `docs/V0_1_FINAL_CONFORMANCE_REPORT.md:100-103` and `docs/V0_1_LIVE_CONFORMANCE_REPORT.md:231-236`. Any in-place mutation would invalidate past audit reports and violate `OPERATING_PROCEDURE_V1.md:128-137` §9.
3. **Normative Contract Rule**:
   `CONTRACT.md:47` explicitly commands: *"Do not rewrite historical stage output in place."*

---

## 2. Authoritative Attempt Selection (`selection.json`)

### 2.1 The Durable Representation of Selection
"Currently selected attempt" must not exist only in ephemeral GUI memory, nor may it be relegated to an application SQLite database where headless CLI tools cannot see it.

The authoritative selection state lives exclusively on the filesystem at:
`<RUNS_DIR>/<run-id>/selection.json`

```json
{
  "evidence_version": 2,
  "run_id": "bots5-v0.1-live-conformance-canary-20260828T125203Z-7214f83a",
  "updated_at": "2026-09-29T14:32:00.123Z",
  "selected_attempts": {
    "extractor": 1,
    "analyst": 1,
    "adversary": 2,
    "synthesis": 1
  }
}
```

### 2.2 Reconstructing Current Attempt from Filesystem Evidence Alone
When reading any run directory, the selection mapping $\sigma: \text{stage\_id} \to \text{attempt\_number}$ is reconstructed by the following deterministic algorithm:
1. If `<run-dir>/selection.json` exists:
   Read and validate `selection.json`. Return `selected_attempts`.
2. If `<run-dir>/selection.json` is absent (legacy V0 run, or initial run prior to selection persistence):
   - Enumerate all `stages/<stage_id>*.json` files.
   - For each `stage_id` in `run.json["stage_order"]`:
     - If `stages/<stage_id>.att<N>.json` files exist, select the highest completed attempt number $\max(N)$, falling back to $\max(N)$ of any state.
     - Else if `stages/<stage_id>.json` exists, select attempt 1.
3. This reconstruction is pure filesystem inspection: zero database state, zero session state, zero network calls.

### 2.3 Selection Mutation Protocol
When an operator changes selection in the desktop UI, or when a regeneration completes:
1. `selection.json` is updated atomically using `storage.atomic_write_json` (`src/bots5/storage.py:86`, write-to-temp + `os.replace` + `_fsync_dir`).
2. An event is appended to `events.jsonl`:
   `events.write("attempt_selected", stage_id, attempt_number=new_attempt, previous_attempt=old_attempt)`
3. If synthesis depends on `stage_id`, the engine checks whether the newly selected attempt changes synthesis freshness. If staleness changes, an event is appended:
   `events.write("synthesis_stale", synthesis_id, reason="dependency_attempt_changed", dependency=stage_id)`
4. `result.md` at the run root is updated to mirror the output of the currently selected synthesis attempt (if that synthesis attempt produced text and succeeded).

### 2.4 Crash and Restart Interpretation
If the process crashes or is killed:
- `selection.json` was written atomically via `os.replace`; it is never left half-written.
- Upon desktop or CLI restart, reading `selection.json` yields the exact selection established prior to the crash.
- If a crash occurs *while* a sibling attempt $N+1$ is executing:
  `selection.json` still safely points to attempt $N$. Sibling attempt $N+1$'s metadata file reflects `state: "running"` or interrupted failure, completely preserving both the historical attempt $N$ and the crash evidence of $N+1$.

---

## 3. Dependency Provenance, Mechanical Staleness & Explicit Synthesis Rerun

### 3.1 Provenance Capture in Synthesis Records
In the baseline engine (`src/bots5/runner.py:404-405`), synthesis simply consumes in-memory strings from `outputs[dep]`. When sibling worker attempts exist, synthesis must permanently record exactly which attempt of each dependency it consumed.

When synthesis attempt $M$ is executed:
1. The runner inspects `selection.json` to identify the selected attempt $N_w$ for each dependency $w \in \text{synth.depends\_on}$.
2. For each dependency $w$, the runner reads `stages/<w>.att<Nw>.md` and computes its cryptographic hash:
   $$H_w = \text{sha256\_hex}(\text{output\_bytes}(w, N_w))$$
3. The runner populates `StageRecord`:
   - `record.consumed_dependencies = {w: Nw for w in synth.depends_on}`
   - `record.dependency_digests = {w: Hw for w in synth.depends_on}`
4. These maps are written directly into `stages/<synth_id>.att<M>.json`.

### 3.2 Formal Mechanical Staleness Predicate
Staleness is not a boolean flag stored in GUI memory. It is a **deterministic, purely mathematical function** of the filesystem state.

Let:
- $\sigma(w)$ be the currently selected attempt number for stage $w$ from `selection.json`.
- $S_M$ be synthesis attempt $M$ (metadata in `stages/<synth_id>.att<M>.json`).
- $D = \text{synth.depends\_on}$ be the set of declared dependencies.

Synthesis attempt $S_M$ is **FRESH** if and only if:
$$\forall w \in D: \quad \sigma(w) = S_M.\text{consumed\_dependencies}[w]$$
$$\text{AND} \quad \text{sha256\_hex}(\text{output\_bytes}(w, \sigma(w))) = S_M.\text{dependency\_digests}[w]$$

If $\exists w \in D$ such that $\sigma(w) \neq S_M.\text{consumed\_dependencies}[w]$ or the digest does not match, then $S_M$ is **MECHANICALLY STALE**.

### 3.3 Bidirectionality of Freshness
Because staleness is evaluated purely against the selected attempts:
- Suppose Worker 1 has Attempts 1 and 2.
- Synthesis Attempt 1 consumed Worker 1 Attempt 1.
- Synthesis Attempt 2 consumed Worker 1 Attempt 2.
- If the operator selects Worker 1 Attempt 2:
  - Synthesis Attempt 1 is **STALE**.
  - Synthesis Attempt 2 is **FRESH**.
- If the operator toggles selection back to Worker 1 Attempt 1:
  - Synthesis Attempt 1 becomes **FRESH** again!
  - Synthesis Attempt 2 becomes **STALE**!
- Zero work is lost. Both synthesis attempts remain intact, durable, and inspectable on disk.

### 3.4 Explicit Synthesis Rerun Protocol
CONTRACT item 11 dictates: *"synthesis rerun is explicit and produces a new synthesis attempt while preserving earlier synthesis evidence."*

Synthesis rerun proceeds as follows:
1. **Trigger**: Operator explicitly clicks "Rerun Synthesis" in the desktop UI, or executes `bots5 rerun-synthesis <run-id>` via CLI.
2. **Zero Auto-Retry**: Synthesis rerun is never triggered automatically by worker completion.
3. **Preflight & Spend Approval**:
   - The engine validates that all currently selected dependencies $\sigma(w)$ are in state `SUCCEEDED` with `completion_complete == True`.
   - Preflight computes the token ceilings and estimated cost for the single synthesis stage.
   - Operator grants explicit approval before network dispatch.
4. **Execution**:
   - Next attempt number is determined: $M_{\text{next}} = \max(\{M\}) + 1$.
   - Synthesis runs, consuming the outputs of $\{w: \sigma(w)\}$.
   - Output written to `stages/<synth_id>.att<M_next>.md`.
   - Metadata written to `stages/<synth_id>.att<M_next>.json` containing `consumed_dependencies` and `dependency_digests`.
5. **Selection & Root Mirror**:
   - `selection.json` is updated with `synth_id: M_next`.
   - `result.md` is atomically updated to match the new synthesis output.
   - Events `stage_succeeded` and `attempt_selected` are appended to `events.jsonl`.

---

## 4. Preflight Binding, Execution Snapshot & The TOCTOU Solution

### 4.1 Root Cause Analysis: The Baseline TOCTOU Window
In the baseline engine (`src/bots5/runner.py:259-271` and `cli.py:73-112`):
1. `validate_referenced_files(job)` reads all prompt and input paths to verify syntax and UTF-8 compliance.
2. In the desktop UI, validation occurs when a job is loaded. Minutes or hours may elapse before the operator clicks "Approve & Run".
3. At execution time, `run_job()` re-reads all referenced files (`runner.py:265-267`).
4. If an external process or malicious actor modifies `contracts/analyst.md` or an input file between UI validation/approval and `run_job()`, the provider will execute with **different bytes than those the operator approved**.
5. Furthermore, `run_job()` does not verify whether `job.execution` limits, models, or provider URLs were altered in memory.

### 4.2 The Immutable Preflight Execution Snapshot (`PreflightSnapshot`)
To bind approval irrevocably to the exact consequential work that will execute, Phase 10 introduces the content-addressed `PreflightSnapshot`:

```python
@dataclass(frozen=True)
class FileSnapshot:
    path: str
    sha256: str
    size_bytes: int
    content_utf8: str  # Frozen in memory

@dataclass(frozen=True)
class PreflightSnapshot:
    job_name: str
    schema_version: int
    output_runs_dir: str
    execution_limits: dict[str, Any]
    provider_routes: dict[str, dict[str, Any]]  # base_url, api_key_env name (no secrets)
    worker_specs: tuple[dict[str, Any], ...]
    synthesis_spec: dict[str, Any] | None
    inputs: tuple[FileSnapshot, ...]
    contracts: tuple[FileSnapshot, ...]
    system_messages: dict[str, str]             # Pre-compiled system prompts
    worker_user_message: str                    # Pre-rendered worker user payload
    preflight_digest: str                       # SHA-256 over canonical JSON of above fields
```

### 4.3 Cryptographic Preflight Digest Algorithm
The `preflight_digest` is computed as:
$$\text{preflight\_digest} = \text{sha256\_hex}(\text{canonical\_json}(\text{snapshot\_fields}))$$
where `canonical_json` serializes sorted keys, UTF-8 encoded, without whitespace padding (`separators=(',', ':')`, `allow_nan=False`).

### 4.4 Approval Binding and Assertion Protocol
1. **Preflight Stage (Zero Spend)**:
   - When the operator loads a job or prepares a regeneration, `PreflightSnapshot` is constructed in memory. All files are read into `FileSnapshot` instances.
   - The preflight dialog displays:
     - The exact `preflight_digest` (first 16 hex characters in UI, full 64 in tooltip/inspect).
     - Declared stages, requested models, provider routing names, token limits, and timeouts.
     - Known cost ceilings or pricing disclosure (see §5).
2. **Approval Record**:
   When the operator clicks "Approve", an immutable `ApprovalRecord` is generated:
   ```json
   {
     "approved_at": "2026-09-29T14:30:15.000Z",
     "approved_by": "operator@desktop-gui",
     "preflight_digest": "4f9b2c8a...64hex",
     "scope": "full_run"
   }
   ```
3. **Execution Gating (TOCTOU Invalidation)**:
   - `run_job()` accepts `(snapshot: PreflightSnapshot, approval: ApprovalRecord)`.
   - **Assertion 1 (In-Memory Identity)**: The runner asserts `approval.preflight_digest == snapshot.preflight_digest`.
   - **Assertion 2 (Disk Consistency Check)**: The runner reads the current disk bytes for all referenced files. If any file's current SHA-256 does not match `snapshot.inputs` or `snapshot.contracts`, execution is immediately refused with `ApprovalInvalidatedError("Filesystem bytes modified after approval")`.
   - **Assertion 3 (Zero Reread Execution)**: The runner uses `snapshot.system_messages` and `snapshot.worker_user_message` directly for dispatch, guaranteeing that the exact bytes approved are the exact bytes transmitted.
   - `preflight.json` (containing the snapshot metadata and approval record) is written into `<run-dir>/preflight.json` before `run_started`.

---

## 5. Spend & Pricing Authority (HUMAN_SEMANTIC_FORK)

### 5.1 The Pinned Baseline Reality vs Product Mandates
- **Grounded Fact**: As proven by Qwen archaeology §4.1 and source inspection, `src/bots5/` contains **no campaign pricing registry, lookup, table, or cache**. Historical `preflight.json` files in `evidence/` were created manually by human operators scraping `openrouter.ai/api/v1/models` out-of-band.
- **Product Mandate**: `CONTRACT.md:70-75` and `OPERATING_PROCEDURE_V1.md:62-75` require conservative preflight pricing before spend for paid execution, while explicitly forbidding the invention of an unapproved pricing authority.

### 5.2 HUMAN_SEMANTIC_FORK Classification: Preflight Pricing Authority
Because no pricing authority exists in code, selecting how Phase 10 establishes preflight cost estimates is a genuine product-semantic choice that cannot be invented by a specialist. It is hereby formally classified as **HUMAN_SEMANTIC_FORK: PREFLIGHT_PRICING_SOURCE**.

The supervisor and Mick are presented with four explicit options:

| Option | Semantic Mechanism | Operational Impact / Tradeoffs | Authority Status |
|---|---|---|---|
| **Option A (Recommended)<br>Operator-Entered Rates** | Desktop preflight modal presents rate fields (`prompt $/1M`, `completion $/1M`) populated by operator, echoing historical human practice. Rates are stamped into `preflight.json`. | Zero external network calls; fully truthful; preserves zero-spend validation; places rate responsibility on operator. Requires operator input. | Matches historical evidence in `evidence/v0.1-final-worker-boundary/*/preflight.json`. |
| **Option B<br>Static Catalogue** | Bundle a static JSON pricing table in `src/bots5/resources/pricing.json` for known models. | Zero network calls; convenient for operator; but rates decay rapidly and risk stale/underestimated cost estimates over time. | Requires creating a new tracked asset and maintenance policy. |
| **Option C<br>Live Provider API Lookup** | Query `https://openrouter.ai/api/v1/models` during preflight to fetch current rates. | Rates always current; but violates zero-network preflight rule; adds external network failure mode; fails completely in air-gapped/offline local environments. | Conflicts with offline zero-spend validation guarantee. |
| **Option D<br>Token Ceilings Only (Unknown Cost)** | Preflight displays exact max token limits (`prompt_tokens`, `max_output_tokens`), but marks dollar cost as `"Unknown (provider-reported post-completion)"`. Explicit approval confirms spend without dollar estimate. | 100% honest; zero maintenance; zero network; but provides no conservative dollar upper bound before execution. | Allowed by CONTRACT.md "live cost progress means truthful known + unknown". |

### 5.3 Regeneration Provider Route Semantics
- `CONTRACT.md:27-32` Item 9: Worker regeneration *"permits an explicit model change; does not silently change provider route."*
- **Policy**: When regenerating a worker, the desktop UI permits the operator to choose a different model string, but **locks the provider route** to the worker's original provider (e.g. `openrouter`).
- **HUMAN_SEMANTIC_FORK: CROSS_PROVIDER_REGENERATION**:
  - *Option 1 (Default)*: Provider route is strictly immutable during regeneration. If a worker was declared under `openrouter`, sibling attempts must use `openrouter`.
  - *Option 2*: Permit operator to switch provider route (e.g., from `openrouter` to `local_openai`) during regeneration, provided the target provider is configured in `job.providers`.
  - *Recommendation*: Adopt Option 1 for Phase 10 to keep the mutation fence narrow.

---

## 6. Cost & Token Accounting Across Sibling Attempts

### 6.1 The Dual-Accounting Principle
When multiple attempts exist for a stage, single-sum cost models break down:
1. **Cumulative Financial Spend ($\text{Cost}_{\text{cumulative}}$)**:
   The total money billed by external providers across all executed attempts ($Att_1 + Att_2 + \dots$). This is accounting truth. Hiding prior failed/regenerated attempt costs would deceive the operator about wallet spend.
2. **Selected Pipeline Cost ($\text{Cost}_{\text{selected}}$)**:
   The cost of the currently active configuration ($\sum_{w} \text{Cost}(w, \sigma(w)) + \text{Cost}(synth, \sigma(synth))$). This is the cost evaluated against the synthesis gate threshold `stop_before_synthesis_if_known_cost_exceeds_usd`.

### 6.2 Structure of `usage.json` with Sibling Attempts
`usage.json` (`src/bots5/usage.py:36-68`) is evolved to present both metrics unambiguously:

```json
{
  "evidence_version": 2,
  "run_id": "bots5-v0.1-live-conformance-canary-20260828T125203Z-7214f83a",
  "cumulative_spend": {
    "cost_usd_known_sum": "0.01625780",
    "cost_status": "known",
    "cost_complete": true,
    "unknown_cost_attempt_ids": [],
    "total_tokens_known_sum": 6853
  },
  "selected_spend": {
    "cost_usd_known_sum": "0.01210805",
    "cost_status": "known",
    "cost_complete": true,
    "unknown_cost_stage_ids": [],
    "total_tokens_known_sum": 5360
  },
  "per_attempt": {
    "extractor.att1": {
      "prompt_tokens": 419,
      "completion_tokens": 248,
      "cost_usd": "0.0003814",
      "cost_known": true
    },
    "adversary.att1": {
      "prompt_tokens": 483,
      "completion_tokens": 1010,
      "cost_usd": "0.00414975",
      "cost_known": true
    },
    "adversary.att2": {
      "prompt_tokens": 483,
      "completion_tokens": 1200,
      "cost_usd": "0.00500000",
      "cost_known": true
    },
    "synthesis.att1": {
      "prompt_tokens": 975,
      "completion_tokens": 1495,
      "cost_usd": "0.0063375",
      "cost_known": true
    }
  },
  "aggregate": {
    "comment": "Mirrors selected_spend for 100% backward compatibility with legacy tools",
    "cost_usd_known_sum": "0.01210805",
    "cost_status": "known",
    "cost_complete": true,
    "unknown_cost_stage_ids": []
  }
}
```

### 6.3 Truthful Live Cost Presentation
1. As confirmed by Qwen archaeology §5.1, campaign providers are non-streaming for the harness. Provider-reported costs arrive **only upon terminal completion of each stage**.
2. Live cost display in the desktop UI reflects:
   $$\text{Known Accrued Subtotal} + \text{Set of Active/Unknown Stages}$$
   Example UI label: `Cost: $0.0045 (+ 2 stages running / unknown)`.
3. Fabricating intermediate token-dollar streaming is strictly forbidden by CONTRACT.md ("Do not implement provider streaming merely for UI animation").

---

## 7. Engine Lifecycle, Desktop Integration Seams & Process Shutdown

### 7.1 Separation of Concerns: Desktop Surface vs Campaign Engine
The desktop is **not** a second campaign engine. The relationship is strictly unidirectional:
- **Engine**: Headless, authoritative execution substrate. It alone creates run directories, executes provider requests, updates `usage.json`, writes `stages/*.json`, and logs to `events.jsonl`.
- **Desktop**: Operational surface and projection. It validates jobs, captures operator approval, requests the engine to start runs/regenerations, and projects live state by reading disk evidence.

### 7.2 Live Progress Projection Seam
Rather than coupling the GUI thread to asynchronous engine coroutines via shared mutable memory:
1. `CampaignProgressBridge` polls the active run directory at `POLL_INTERVAL_MS = 250ms` (matching the Phase 9 import queue discipline in `phase9_queue_dock.py:136`).
2. On each poll, it reads `run.json`, `events.jsonl` (tail lines), and `stages/*.json`.
3. The UI ViewModel (`CampaignViewModel`) is updated via Qt signals emitted across the bridge.
4. If the GUI crashes or freezes, the engine continues executing and persisting evidence to disk unhindered.

### 7.3 Desktop Shutdown with Active Campaign (Resolving Qwen Gap [X] 1)
Qwen archaeology §7.3 and §13 Item 1 uncovered a critical defect: `run_job()`'s outer `except Exception` (`src/bots5/runner.py:465`) does **not** catch `asyncio.CancelledError`. If a desktop window close cancels the task, `run.json` would remain in `state: "running"`.

**The Phase 10 Lifecycle Fix**:
1. When the operator closes the desktop window while a campaign is running:
   `MainWindow.closeEvent` prompts the operator with an explicit consequence modal:
   *"A campaign is currently executing provider requests. Cancelling will stop execution. In-flight requests may still incur provider charges whose outcome is unknown."*
2. If confirmed, the desktop initiates controlled cancellation.
3. In `runner.py`, the outer block is wrapped in `try ... except (Exception, asyncio.CancelledError) as exc:`.
4. On `CancelledError`:
   - All active worker tasks are cancelled and awaited with `asyncio.gather(*tasks, return_exceptions=True)`.
   - In-flight stages are swept:
     ```python
     record.state = StageState.FAILED
     record.error_type = "aborted"
     record.error_message = "cancelled by operator shutdown"
     record.provider_side_outcome_unknown = (record.started_at is not None)
     record.ended_at = now_iso()
     persist_stage(dirs, record)
     events.write("stage_failed", record.id, error_type="aborted")
     ```
   - Terminal run state is persisted:
     `persist_run(dirs, state=RunState.FAILED, ended_at=now_iso(), ...)`
     `events.write("run_failed", reason="cancelled")`
   - Only after disk fsync completes does the task exit and the desktop shutdown proceed.
5. If the OS terminates the process abruptly (SIGKILL/power outage):
   `run.json` remains in `state: "running"`. Upon restart, the reader inspects process liveness, detects the orphaned run, and truthfully displays:
   `Interrupted / Incomplete (durable state: running; provider outcome unknown)`.
   It **never** silently marks the run as resumable or succeeded.

---

## 8. Complete State & Transition Forensic Matrix

The table below specifies the durable representation, crash semantics, reader behavior, attempt selection, and historical immutability guarantees for every state and transition in Phase 10.

| # | State / Transition | Authoritative Durable Representation | Crash / Restart Interpretation | Old-Run Reader Behavior | Current Attempt Selection Effect | Historical Immutability Guarantee |
|---|---|---|---|---|---|---|
| **1** | `UNLOADED` | No run directory exists. Application state may hold last-opened job path in settings. | App restarts in clean unloaded state. | N/A | None. | No files created or modified. |
| **2** | `JOB_LOADED_UNVALIDATED` | Job file on disk at user-selected path. Zero run-directory bytes. | Reloads job path from UI session if configured. | N/A | None. | Zero provider calls, zero run-directory creation. |
| **3** | `VALIDATED_ZERO_SPEND` | In-memory `ValidationResult`. Zero disk artifacts in runs directory. | If crashed, re-validates upon reload. | N/A | None. | Zero network requests, zero run-directory creation. |
| **4** | `PREFLIGHT_PREPARED` | In-memory `PreflightSnapshot` containing file SHA-256s, compiled prompts, and `preflight_digest`. | Recomputed on demand; no durable run state yet. | N/A | None. | Zero run-directory writes; referenced files read-only. |
| **5** | `APPROVED_PENDING_RUN` | In-memory `ApprovalRecord` bound to `preflight_digest`. | If process dies before run starts, approval is discarded. Re-approval required. | N/A | None. | Zero spend incurred; no disk state created. |
| **6** | `RUN_STARTING` | Run tree created: `<runs>/<id>/`. Initial `job.resolved.json`, `preflight.json`, `usage.json`, and `run.json` (`state: "running"`). | Crash here leaves `run.json` in `running` with zero completed stages. Correctly read as interrupted run. | Old readers fail-safe: see valid `run.json` in `running`. | Initialized to attempt 1 for all declared stages. | Run directory created with unique ID; cannot collide with past runs. |
| **7** | `WORKERS_RUNNING` | `events.jsonl` logs `stage_started`. Per-stage `stages/<w>.att1.json` written with `state: "running"`. | Crash leaves active stages in `running`. Outcome marked `provider_side_outcome_unknown = True` upon restart inspection. | Old readers see running stages via `load_run_view`. | Attempt 1 active. | Each stage writes to its own `.att1.json` file. |
| **8** | `WORKER_COMPLETED` | Output written to `stages/<w>.att1.md`. Metadata written to `stages/<w>.att1.json` (`SUCCEEDED` or `FAILED`). `usage.json` updated. | Completed worker output is already fsynced on disk. Survives crash completely. | Old reader reads `stages/<w>.json` (or `.att1.json`). | Worker attempt 1 is selected. | Attempt 1 output and metadata are immutable from this point forward. |
| **9** | `SYNTHESIS_EVALUATION` | Runner checks gates. If blocked: `stages/<synth>.att1.json` written (`state: "skipped"`, reason). | If blocked, run terminates cleanly with `state: "failed"`. | Old reader reads skipped synthesis record. | Synthesis attempt 1 is selected (skipped). | Blocked evidence preserved durably. |
| **10** | `SYNTHESIS_RUNNING` | `stages/<synth>.att1.json` written (`state: "running"`). Consumed dependency map recorded. | Crash leaves synthesis in `running`, marked `provider_side_outcome_unknown = True`. | Old reader sees running synthesis stage. | Synthesis attempt 1 active. | Previous worker attempts remain untouched. |
| **11** | `RUN_TERMINAL_SUCCESS` | `run.json` (`state: "succeeded"`), `selection.json` persisted, `result.md` written, `run_succeeded` in `events.jsonl`. | Completed run loaded by desktop or CLI; result displayed durably until `New Job`. | Fully inspectable by legacy CLI `status` and `inspect`. | All stages attempt 1 selected. | Entire run directory is now sealed immutable evidence. |
| **12** | `RUN_TERMINAL_FAILED` | `run.json` (`state: "failed"` or `"timed_out"`), `selection.json` persisted, `run_failed` in `events.jsonl`. | Failed run loaded and inspected. Eligible for worker regeneration. | Fully inspectable by legacy CLI. | All stages attempt 1 selected. | All partial outputs preserved for inspection. |
| **13** | `WORKER_REGEN_PREFLIGHT` | In-memory preflight snapshot for single worker. Optional model change validated. | Discarded on crash; original run evidence untouched. | Run directory still reflects state 11 or 12. | Selection unchanged. | Original attempt 1 untouched. |
| **14** | `WORKER_REGEN_APPROVED` | In-memory `ApprovalRecord` bound to worker preflight digest. | Discarded on crash; no spend incurred. | Run directory unchanged. | Selection unchanged. | Original attempt 1 untouched. |
| **15** | `WORKER_REGEN_RUNNING` | Sibling files created: `stages/<w>.att2.json` (`state: "running"`). `events.jsonl` logs `stage_started`. | Crash leaves attempt 2 in `running`. Selection still points to attempt 1! | Old reader reads selected attempt 1. Ignores `.att2.json`. | Attempt 1 remains selected during execution. | Attempt 1 files remain completely untouched and valid. |
| **16** | `WORKER_REGEN_COMPLETED` | Output written to `stages/<w>.att2.md`, metadata to `stages/<w>.att2.json`. `usage.json` updated with cumulative spend. | Both attempt 1 and attempt 2 are fully durable on disk. | Old reader sees selected attempt. | `selection.json` updated to point to attempt 2. | Both attempt 1 and attempt 2 evidence preserved forever. |
| **17** | `SYNTHESIS_STALE_TRIGGER` | Selection of worker attempt 2 triggers mechanical staleness check. Synthesis 1 marked `STALE`. Event `synthesis_stale` logged. | Reading disk evidence immediately reproduces `STALE` status. | Old reader does not compute staleness; Phase 10 reader shows stale. | Worker: attempt 2; Synthesis: attempt 1 (stale). | Historical synthesis attempt 1 output remains intact. |
| **18** | `SYNTHESIS_RERUN_APPROVED` | In-memory preflight snapshot and approval for synthesis rerun. | Discarded on crash; no spend incurred. | Run directory unchanged. | Worker: attempt 2; Synthesis: attempt 1. | Synthesis attempt 1 untouched. |
| **19** | `SYNTHESIS_RERUN_RUNNING` | Sibling files created: `stages/<synth>.att2.json` (`state: "running"`). Consumes worker attempt 2. | Crash leaves synthesis 2 in `running`. Synthesis 1 remains selected (stale). | Old reader sees synthesis 1. | Worker: attempt 2; Synthesis: attempt 1 (stale). | Synthesis attempt 1 files completely untouched. |
| **20** | `SYNTHESIS_RERUN_COMPLETED` | `stages/<synth>.att2.md` and `.json` written. `selection.json` updated to `synth: 2`. `result.md` updated. Synthesis 2 is `FRESH`. | Run is now succeeded with fresh synthesis. Both synthesis attempts viewable. | Old reader sees selected synthesis attempt 2. | Worker: attempt 2; Synthesis: attempt 2 (fresh). | Both synthesis attempt 1 and attempt 2 preserved in full. |
| **21** | `NEW_JOB_REQUESTED` | Desktop UI clears working context. Zero disk mutation in run directory. | UI opens fresh blank campaign window. | Historical run directory remains in `.bots5/runs/` forever. | Resets UI state; filesystem untouched. | Complete audit trail of past runs preserved on disk. |

---

## 9. Defect & Risk Register for Supervisor Integration

The supervisor must integrate the following top 5 architectural risks and defects during design reconciliation:

### Risk 1: Window Close Abruptly Leaving Orphaned `running` State
- **Defect/Mechanism**: `runner.py:465` catches `Exception`, not `asyncio.CancelledError`. If the desktop window is closed during campaign execution, task cancellation bypasses error handling and leaves `run.json` permanently stating `state: "running"`.
- **Mitigation**: Update `run_job()` to catch `(Exception, asyncio.CancelledError)`, sweep running stages, mark `provider_side_outcome_unknown = True`, write terminal `state: "failed"`, and fsync before yielding to desktop shutdown.

### Risk 2: TOCTOU Vulnerability via Referenced File Mutation
- **Defect/Mechanism**: The runner re-reads prompt contracts and inputs from disk at execution time (`runner.py:265-267`). Modifying these files after preflight approval causes the provider to execute unauthorized bytes.
- **Mitigation**: Freeze referenced file contents in an immutable in-memory `PreflightSnapshot` during validation. Pass this frozen snapshot directly to the runner and verify file content hashes immediately before network dispatch.

### Risk 3: Double-Counting vs Underreporting Spend on Sibling Regeneration
- **Defect/Mechanism**: If `usage.json` sums all stage records naively, regenerating a worker double-counts token spend against synthesis gating thresholds. If it only counts the selected attempt, the operator's total billed financial spend is underreported.
- **Mitigation**: Implement strict dual-accounting in `usage.json`: `cumulative_spend` tracks total financial accounting truth across all attempts, while `selected_spend` tracks the active pipeline for synthesis gating and display.

### Risk 4: Containment Check Rejection of Attempt Files
- **Defect/Mechanism**: `storage.load_stage_view()` (`storage.py:197`) enforces `candidate.parent != (run_dir / "stages").resolve()`. If attempts were placed in subdirectories (`stages/adversary/1.md`), existing containment checks would raise `ValidationError("stage output path escapes run directory")`.
- **Mitigation**: Keep attempt filenames flat within `stages/` (`stages/<id>.att<N>.md`). This guarantees `candidate.parent == (run_dir / "stages").resolve()` with zero modification to path traversal security logic.

### Risk 5: Preflight Pricing Authority Absence (HUMAN_SEMANTIC_FORK)
- **Defect/Mechanism**: OPv1 demands conservative preflight pricing, but the engine has no pricing registry. Fabricating a price or token-dollar streaming violates product truth and `CONTRACT.md`.
- **Mitigation**: Halt for Mick on **HUMAN_SEMANTIC_FORK: PREFLIGHT_PRICING_SOURCE** with Option A (Operator-Entered Rates) recommended to match historical precedent. Live progress must display known subtotal + unknown active stage count, never interpolated dollars.

---

## 10. File Mutation & Implementation Seam Inventory

When authorized by Mick in Parcel v2, implementation is bounded strictly to the following files:

| Subsystem | File Path | Nature of Planned Evolution |
|---|---|---|
| **Models** | `src/bots5/models.py` | Add `attempt_number`, `consumed_dependencies`, `dependency_digests`, `preflight_digest` to `StageRecord`. Add `PreflightSnapshot` and `ApprovalRecord` dataclasses. |
| **Storage** | `src/bots5/storage.py` | Add versioned reader support (`evidence_version: 2`) in `load_run_view` and `load_stage_view`. Add `load_selection` and `persist_selection`. Add attempt-aware `persist_stage_attempt`. |
| **Runner** | `src/bots5/runner.py` | Accept `PreflightSnapshot` and `ApprovalRecord`. Assert hash integrity before execution. Trap `CancelledError` for clean terminalization. Record dependency provenance in synthesis. Support `regenerate_worker()` and `rerun_synthesis()`. |
| **Events** | `src/bots5/events.py` | Add `"attempt_selected"`, `"synthesis_stale"`, and `"run_cancelled"` to `EVENT_TYPES`. |
| **Usage** | `src/bots5/usage.py` | Evolve `usage_document()` to output dual-accounting structure (`cumulative_spend` and `selected_spend`). |
| **CLI** | `src/bots5/cli.py` | Add `--attempt` argument to `inspect`. Update `status` to display attempt counts and stale synthesis markers. Add `rerun-synthesis` verb. |
| **Desktop Core** | `src/bots5/core/campaign.py` *(new)* | Bridge between Desktop UI and Campaign Runner: manages background execution tasks, preflight generation, and disk polling. |
| **Desktop UI** | `src/bots5/desktop/campaign_dock.py` *(new)* | Campaign dock widget: job loading, zero-spend validation view, preflight/approval modal, truthful progress bars, attempt switcher, and expandable markdown viewer. |

---

## 11. Conclusion & Certification

This architecture satisfies every locked obligation in `parcel/CONTRACT.md`:
- Filesystem evidence remains the sole, authoritative source of truth.
- Worker regeneration is append-only sibling evidence; historical attempts are never overwritten.
- Synthesis staleness is a mechanical predicate derived from recorded dependency attempt numbers and content digests.
- Preflight approval is cryptographically bound to execution bytes, eliminating the TOCTOU window.
- Headless CLI and legacy V0/V0.2 runs remain 100% compatible.
- All genuine semantic forks are explicitly surfaced for operator adjudication.
