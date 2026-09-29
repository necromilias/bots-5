# Preflight / approval state machine

Scope: the exact state machine that binds operator approval to the consequential work that
will execute, refuses to spend when the approved bytes/configuration no longer match, and
makes an approval one-shot.
Authoritative sources: `parcel/CONTRACT.md` §"Preflight / approval integrity", §"Cost
truth"; `FINDINGS.md` F-03/F-06/F-13; OrgMem `core/approval-boundaries.md`; OPv1 §3–§5;
Qwen archaeology §3–§4; Gemini §4; Command §1–§3; MiniMax H-4/H-6; Step §3.1–3.3, §9.

> Revision note: this document was repaired after fresh GPT-6 Luna falsification
> (`reports/review/GPT6_LUNA_DESIGN_FALSIFICATION.md`) findings F-01, F-02, F-03, F-09,
> after the fresh MiMo final oracle (`reports/oracle/MIMO_FINAL_DESIGN_ORACLE.md`)
> finding M-3, and after the second fresh MiMo oracle
> (`reports/oracle/MIMO_FINAL_DESIGN_ORACLE_v3.md`) findings N-5 (exclusive-create scope)
> and N-6 (`api_key_env_name` origin). See
> `reports/design/DESIGN_REPAIR_RECORD_v1_to_v2.md`,
> `reports/design/DESIGN_REPAIR_RECORD_v2_to_v3.md` and
> `reports/design/DESIGN_REPAIR_RECORD_v3_to_v4.md`.

## 0. What the baseline actually does (grounded)

- `manifest.load_job` + `manifest.validate_referenced_files` are side-effect-free: no
  network, no run-directory creation (`cli.py:73-77`; `tests/test_cli_views.py:14-23`).
- `runner.run_job` re-reads every system prompt (`runner.py:264-266`) and input
  (`runner.py:267`) from disk, **after** validation and **before** `create_run_tree`
  (`runner.py:271`).
- There is no digest/hash of referenced bytes anywhere in the campaign package. The
  `CompletionRequest` is built fresh per stage (`runner.py:146-153`) and is not pinned.
- `run_job` accepts an independent `providers: Mapping[str, Provider]`
  (`runner.py:231-260`) and validates only presence + a callable `complete`; it never
  checks the concrete provider's route against the job.
- Synthesis input is built from **current in-memory worker outputs**
  (`runner.py:404-415`), not from a pinned selection.
- There is no preflight, approval, spend gate, confirmation or approval-consumption seam.

Conclusion: the TOCTOU window is real, and approval today would bind to pathnames only.
The design closes it engine-side and makes consent one-shot.

## 1. States

| State | Durable representation | Provider calls | Run directory |
|---|---|---|---|
| `UNLOADED` | none | none | none |
| `JOB_LOADED` | job file on disk (operator-supplied path) | none | none |
| `VALIDATED_ZERO_SPEND` | in-memory `ValidationResult` | none | **none** |
| `PREFLIGHT_PREPARED` | in-memory `PreflightSnapshot` + `OperationSnapshot` | none | none |
| `APPROVED` | in-memory `ApprovalRecord` bound to digest + target + `approval_id` | none | none |
| `SPEND_REFUSED` | typed refusal surfaced; no artifacts | none | none |
| `RUN_STARTED` | `<run>/preflight.json`, `<run>/approvals/<approval_id>.json`, `run.json`, attempt 1 stages | first request only after assertions | created |
| `APPROVAL_CONSUMED` | `<run>/approvals/<approval_id>.json` present (exclusive create) | — | as applicable |
| `REGEN_PREFLIGHT_PREPARED` | in-memory operation snapshot for one worker | none | run dir unchanged |
| `REGEN_APPROVED` | in-memory approval, scope `worker_regeneration:<id>` | none | run dir unchanged |
| `RERUN_PREFLIGHT_PREPARED` | in-memory operation snapshot for synthesis | none | run dir unchanged |
| `RERUN_APPROVED` | in-memory approval, scope `synthesis_rerun` | none | run dir unchanged |

`PREFLIGHT_PREPARED`/`APPROVED` are intentionally **not persisted** for an operation that
never starts: an abandoned approval leaves zero run-directory bytes. `preflight.json` and
the approval marker are written only once an operation actually begins, before the first
`request_sent`.

## 2. Snapshots (frozen, in models.py)

### 2.1 PreflightSnapshot — the job/configuration snapshot

Fields (frozen; no secrets):

- `job_name`, `schema_version`, `output_runs_dir`;
- `execution_limits` (`max_parallelism`, `run_timeout_seconds`,
  `stop_before_synthesis_if_known_cost_exceeds_usd`);
- `provider_routes`: per declared provider id, the frozen route
  `{kind, base_url, api_key_env_name, api_key_source}` — **no secret value**. For the
  `openrouter` kind the `api_key_env_name` is the CLI convention constant
  `OPENROUTER_API_KEY` (`cli.py:92-94`), because the manifest declares provider config only
  for `local_openai` (`manifest.py:55`, `PROVIDER_CONFIG_KEYS = {"local_openai"}`); for
  `local_openai` it comes from the job's provider config (N-6 clarification).
- `worker_specs` / `synthesis_spec`: `id, provider, model, temperature,
  max_output_tokens, timeout_seconds, system_prompt_path` (+ `depends_on`);
- `inputs`: `FileSnapshot(path, sha256, size_bytes, content_utf8)`;
- `contracts`: `FileSnapshot` for every worker/synthesis system prompt;
- `system_messages`: compiled contract text per stage id (frozen);
- `preflight_digest`: `sha256(canonical_json(fields))`.

### 2.2 OperationSnapshot — the operation-specific binding (F-01 repair)

A preflight alone is not sufficient consent for regeneration or rerun. Each operation gets
an explicit frozen snapshot whose digest is the approval digest for that operation:

```
OperationSnapshot:
  operation: "full_run" | "worker_regeneration" | "synthesis_rerun"
  target_run_id: str                 # the run this operation acts on
  target_stage_id: str | None        # worker id (regen) or synthesis id (rerun)
  target_attempt_number: int | None  # the attempt index this operation will create
  requested_model: str | None        # regen/rerun requested model
  provider_route: {...}              # the frozen route for target_stage_id
  system_message: str                # exact frozen prompt for the dispatched stage
  user_message: str                  # exact frozen rendered user payload
  dependency_attempts: {dep_id: int}         # rerun only (empty for full_run/regen)
  dependency_digests: {dep_id: sha256}       # rerun only
  inputs: [FileSnapshot, ...]        # referenced bytes re-verified
  preflight_digest: sha256(canonical_json(all above))
```

Rules:

- **full_run**: `dependency_attempts`/`dependency_digests` are empty; `user_message` is the
  rendered worker payload.
- **worker_regeneration**: binds `target_run_id`, the worker id, the exact next attempt
  number, and the requested model. `user_message` is the same rendered worker payload; the
  job/contracts are re-verified against the same job.
- **synthesis_rerun**: binds `target_run_id`, the synthesis id, the next synthesis attempt
  number, **each selected dependency attempt number and the SHA-256 of that attempt's exact
  output bytes**, and the **exact rendered synthesis user message** built from those bytes.

Dispatch uses the frozen `system_message`/`user_message` directly. **Zero re-read** after
approval. If, at execution time, the selected dependency attempts or their bytes differ from
the snapshot, the engine refuses before any provider request.

## 3. ApprovalRecord (in models.py)

```
{
  "approval_id": "<uuid7>",
  "approved_at": "<UTC ISO-8601>",
  "approved_by": "<local operator label>",
  "preflight_digest": "<64 hex>",
  "scope": "full_run" | "worker_regeneration:<stage_id>" | "synthesis_rerun",
  "target": {"run_id": "...", "stage_id": "..."|null, "attempt_number": N|null},
  "pricing_evidence": { ... see §5 ... }
}
```

This is a **local operator consent record**, not a portable OrgMem approval reference. It
binds a local human action to the exact digest and the exact target. It does not claim
external attribution, expiry or delegation semantics the desktop cannot truthfully provide.

### 3.1 One-shot consumption (F-02 repair)

Consent is consumed engine-side exactly once:

1. Immediately after all assertions pass and **before** the first `request_sent`, the engine
   atomically creates `<run_dir>/approvals/<approval_id>.json` using
   exclusive-create semantics (`os.open(..., O_CREAT|O_EXCL)`); the marker records the
   consumed digest, scope, target, timestamp.
2. If the marker already exists, the engine raises `ApprovalInvalidatedError` and makes no
   provider request. This is durable across process restarts and applies equally to
   non-UI callers.
3. Additional independent guard: attempt payloads are written with exclusive-create
   semantics (`persist_stage_attempt` refuses to overwrite an existing attempt), and
   `create_run_tree` already refuses an existing run id (`storage.py:109-110`). Because the
   approval binds `target_run_id` + `target_attempt_number` for regeneration/rerun, and
   `target_run_id` for a full run, a replayed approval hits an existing artifact and fails
   closed even if the marker were removed.
   **Exclusive-create scope (N-5 clarification).** Exclusive-create governs **creating a new
   attempt** (and the run tree, and the approval marker). The ordinary lifecycle of that same
   attempt — `queued` → `running` → terminal — is a normal in-place state update of the file
   just created, and is not forbidden by it.
4. A consumed approval is never re-armed. A new operator action produces a new
   `approval_id` and a new digest.

## 4. Execution assertions (engine-side, before the first provider request)

In `runner.run_job` / `regenerate_worker` / `rerun_synthesis`, when snapshot and approval
are supplied:

1. **Identity**: `approval.preflight_digest == snapshot.preflight_digest`.
2. **Scope + target**: scope matches the operation; `target.run_id` matches the run being
   operated on; `target.stage_id`/`target.attempt_number` match the operation.
3. **One-shot**: the approval marker does not already exist; create it exclusively.
4. **Disk consistency**: re-read every referenced path, recompute SHA-256, compare with
   `snapshot.inputs`/`snapshot.contracts`.
5. **Configuration consistency**: job worker/synthesis specs, provider routes and execution
   limits canonicalize to the same digest input as the preflight snapshot.
6. **Selection/bytes consistency (rerun)**: the currently selected dependency attempts and
   the SHA-256 of their output bytes equal `snapshot.dependency_attempts` /
   `snapshot.dependency_digests`; otherwise `ApprovalInvalidatedError` before dispatch.
7. **Provider-object route validation (F-03 repair; restated after M-3)**: for every
   provider id used by the operation, assert that the concrete provider instance actually
   passed to the engine matches the frozen route. The check is **kind-specific**, because
   the live providers do not expose an identical surface
   (`openrouter.py:61-62` exposes only `base_url`; `openai_compatible.py:77-83` exposes
   `base_url` and `api_key_env`):
   - always: the provider mapping key set for the operation equals the snapshot's provider
     set;
   - always: `getattr(provider, "base_url", None)` equals the frozen `base_url`, and the
     instance's class/module corresponds to the frozen `kind`
     (`openrouter` vs `local_openai`);
   - where the instance **exposes** `api_key_env`, it must equal the frozen
     `api_key_env_name`;
   - for a provider kind that does **not** expose a key-env property (openrouter), the
     key-source binding is **by construction**: the bridge resolves the provider from the
     frozen route (env-var name + base_url + kind) and the engine asserts the instance is
     the bridge-resolved object for that route. This is an explicit, documented limitation:
     independently re-deriving the openrouter key source from the provider object would
     require a provider-contract change, which the `providers/**` zero-diff guard forbids.
     A swap test proves the assertion catches a different route/kind instance.

   Mismatch ⇒ `ApprovalInvalidatedError` before dispatch. The UI may not pass an
   independently mutable provider map whose route is outside the approved digest; the
   bridge resolves providers from the frozen route snapshot.
8. **Zero-reread dispatch**: the frozen messages are used; no provider input path is read
   again.

Assertions run **before** `create_run_tree` for a full run. A refused approval therefore
leaves **no run directory** and makes **no provider call**. When snapshot/approval are
omitted, `run_job` preserves today's headless behavior exactly.

## 5. Pricing evidence — HSF-1 branch (repaired after F-09)

The design records pricing evidence but invents no pricing authority. OPv1 §4
(`docs/OPERATING_PROCEDURE_V1.md:62-75`) requires, immediately before a paid run: the
highest applicable currently advertised input/output pricing for each selected
model/provider route, and a conservative expected upper bound computed from prompt size,
stage count and configured output ceilings.

- **Branch A — operator-entered advertised rates (the only branch this design calls
  OPv1-compliant by default).** `pricing_evidence` must record:
  `{"source":"operator_entered_advertised","observed_at":"<UTC>","route":"<provider/model>",
    "input_usd_per_1m":<dec>,"output_usd_per_1m":<dec>,"rate_source":"<operator-cited
    source>","estimated_upper_bound_usd":<dec>,"bound_basis":"prompt_bytes + stage_count
    + max_output_tokens"}`. The bound calculation basis is fixed in the design
    (`prompt_tokens_upper`, `n_stages`, `sum(max_output_tokens)`) so it is auditable.
- **Branch D — explicit unknown cost.** `{"source":"unknown", "token_ceilings":{...},
  "note":"provider-reported cost only at terminal completion"}`. **D does not satisfy the
  OPv1 dollar-bound requirement.** It is available only if Mick *explicitly waives or
  supersedes* that requirement for this workflow; the register records it as a waiver, not
  as an equivalent compliant branch.
- **Branch B (static catalogue)** and **Branch C (live lookup)** remain outside this fence:
  B adds a maintained tracked asset; C violates the zero-network preflight guarantee.

Paid approval UI must not be implemented until Mick adjudicates this fork. Live progress
remains known subtotal + explicit unknown set under every branch.

## 6. Invalidation and re-approval

- Any change to referenced bytes, prompt/contract bytes, model/provider selection, token
  ceilings, execution limits, dependency selection, or output target after preflight
  invalidates approval: the engine refuses with `ApprovalInvalidatedError`.
- Regeneration and synthesis rerun are new consequential operations: each performs its own
  zero-spend validation, operation-specific snapshot and explicit approval.
- An approval is consumed once ( §3.1 ). There is no auto-approval, no remembered consent,
  no silent reuse, and no re-arm.
- Awaiting an operator decision never blocks the event loop; preflight preparation is
  cancellable and writes nothing.

## 7. Transition table

| From | Event | To | Durable effect |
|---|---|---|---|
| UNLOADED | load job (explicit path) | JOB_LOADED | none |
| JOB_LOADED | validate | VALIDATED_ZERO_SPEND | none |
| VALIDATED_ZERO_SPEND | prepare preflight+operation snapshot | PREFLIGHT_PREPARED | none |
| PREFLIGHT_PREPARED | operator approves | APPROVED | none |
| APPROVED | assertions pass; consume approval | APPROVAL_CONSUMED → RUN_STARTED | `preflight.json`, `approvals/<id>.json`, run tree, queued attempts |
| APPROVED | assertions fail (bytes/limits/target/route/selection) | SPEND_REFUSED | none (typed refusal) |
| RUN_STARTED | approve another operation with a new approval | REGEN/RERUN_APPROVED | new attempt only |
| any | replay a consumed approval | SPEND_REFUSED | none (typed refusal, no request) |

## 8. Zero-spend proof points required by T0

- loading + validating a job constructs no provider and creates no run directory;
- a refused approval (digest mismatch, changed bytes, changed prompt/contract, changed
  provider route, changed dependency selection or bytes, wrong scope/target) creates no run
  directory and calls no provider;
- no `provider.complete(...)` is reachable before an explicit approval in the desktop path;
- regeneration/rerun each re-validate, re-preflight and re-approve;
- replaying a consumed approval produces no second provider request.

See `VALIDATION_PLAN.md` §T0.
