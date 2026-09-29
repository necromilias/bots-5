# Campaign evidence evolution (additive, versioned, append-only)

Scope: the exact durable evidence model Phase 10 adds, why it is safe for historical runs,
and how "current attempt" is reconstructed from the filesystem alone.
Sources: Qwen §1–§2, §6, §12; Gemini §1–§2, §6; MiniMax §5, §7; Step §4.2, §5.4, §10;
CONTRACT §"Central architectural problem", §"Backward compatibility".

> **Revision history.** v1 → v2: F-07 (derived selected cost; §4) and F-02 (approval
> markers; layout). v2 → v3: M-5 (headless CLI verbs + consent form; §6.4) and the
> `preflight.json` schema disambiguation (§2.4). v3 → v4: N-6 (`api_key_env_name` origin for
> openrouter is the CLI convention constant `OPENROUTER_API_KEY`, `cli.py:92-94`; the
> manifest declares provider config only for `local_openai`, `manifest.py:55`). See
> `reports/design/DESIGN_REPAIR_RECORD_v3_to_v4.md`.

## 1. Version marker

`run.json` gains exactly one key: `"evidence_version": 2`.

- absence of the key ⇒ version 1 (all existing V0/V0.2 runs, including the 76 tracked
  files under `evidence/**`);
- version 1 readers are unchanged;
- version 2 readers are selected by the marker only.

No other existing `run.json` key changes meaning. Existing keys are preserved with their
current shape: `run_id`, `state`, `started_at`, `ended_at`, `run_timeout_seconds`,
`synthesis_skipped_reason`, `stage_order`, `stages`, `usage`.

## 2. Layout

```text
<RUNS_DIR>/<run-id>/
  run.json                 # + "evidence_version": 2
  job.resolved.json        # unchanged
  preflight.json           # NEW (v2): frozen snapshot metadata + operation binding + approval record
  selection.json           # NEW (v2): authoritative stage_id -> selected attempt
  approvals/<approval_id>.json  # NEW (v2): exclusive-create one-shot approval consumption markers
  usage.json               # extended: cumulative + selected (derived cache) + per_attempt (aggregate unchanged)
  events.jsonl             # append-only; additive event kinds only
  result.md                # mirror of the currently selected synthesis attempt
  stages/
    <id>.att1.json/.md     # v2 attempt 1 (every stage)
    <id>.att2.json/.md     # v2 sibling attempt (regeneration / synthesis rerun)
    <id>.json/.md          # v1 legacy single attempt (never written by v2)
```

Attempt number `N >= 1`, sequential per stage. `stages/*.att<N>.md` is written only when
the attempt produced output text.

### 2.1 Containment

Attempt outputs live directly in `stages/`, so
`Path(run_dir / output_path).parent == (run_dir / "stages").resolve()` holds exactly as
today. `storage.load_stage_view`'s security predicate (`storage.py:196-198`) is **not
modified**; `paths.py` is unchanged.

### 2.2 Attempt resolution (deterministic, no scanning guesswork)

For a declared stage id `s`:

1. Read `selection.json` if present. `selected_attempts[s]` (default 1) is authoritative.
2. Attempt metadata path = `stages/<s>.att<N>.json`; output path is recorded inside the
   metadata as `stages/<s>.att<N>.md` (relative).
3. A missing selected-attempt file is a hard `ValidationError`. The reader never falls
   back to a different attempt and never scans for a "latest" file.
4. `selection.json` absent ⇒ every stage's selected attempt is 1 (the initial state of a
   run). A v2 directory that lacks `selection.json` but contains `.att2+` files is
   malformed and fails closed.

This removes the `w1` vs `w1.att2` stage-id ambiguity: resolution is driven by declared
ids + explicit selection, never by filename shape inference.

### 2.3 selection.json

```json
{
  "schema_version": 1,
  "run_id": "<run-id>",
  "updated_at": "<UTC ISO-8601>",
  "selected_attempts": {"<stage_id>": 1, "<other_stage>": 2}
}
```

Written with `storage.atomic_write_json` (temp + `os.replace` + directory fsync). It records
only current selection; it is never the place where attempt content lives.

### 2.4 preflight.json

```json
{
  "doc_schema_version": 1,
  "job_schema_version": 2,
  "job_name": "...",
  "run_id": "...",
  "operation": "full_run",
  "target": {"run_id": "...", "stage_id": null, "attempt_number": null},
  "prepared_at": "...",
  "execution_limits": { "...": "..." },
  "provider_routes": {"openrouter": {"kind": "openrouter", "base_url": "...",
                                     "api_key_env_name": "OPENROUTER_API_KEY",
                                     "key_source": "environment"}},
  "stages": [{"stage_id": "...", "provider": "...", "model": "...", "temperature": 0.0,
              "max_output_tokens": 0, "timeout_seconds": 0.0,
              "system_prompt_sha256": "...", "system_prompt_path": "..."}],
  "inputs": [{"path": "...", "sha256": "...", "size_bytes": 0}],
  "preflight_digest": "<64 hex>",
  "approval": {"approval_id": "<uuid7>", "approved_at": "...", "approved_by": "...",
               "preflight_digest": "...", "scope": "full_run",
               "target": {"run_id": "...", "stage_id": null, "attempt_number": null}},
  "pricing_evidence": {"source": "operator_entered_advertised" | "unknown", "...": "..."}
}
```

No secret is ever persisted: only the API-key environment variable **name** appears
(consistent with `manifest.job_to_dict`, `tests/test_runner.py:373-400`).

## 3. `StageRecord` additive fields (models.py)

| Field | Default | Meaning |
|---|---|---|
| `attempt_number: int` | `1` | which attempt this record is |
| `consumed_dependencies: dict[str,int]` | `{}` | for a synthesis attempt: declared dep → attempt number consumed |
| `dependency_digests: dict[str,str]` | `{}` | for a synthesis attempt: dep → sha256 of the consumed attempt's output bytes |
| `preflight_digest: str \| None` | `None` | digest of the approval under which this attempt executed |

`to_dict()` emits `attempt_number` always; emits `consumed_dependencies`/`dependency_digests`
only when non-empty; emits `preflight_digest` only when set. Version 1 documents remain
readable because every new field defaults.

## 4. usage.json — dual accounting

```json
{
  "evidence_version": 2,
  "cumulative_spend": {"cost_usd_known_sum": "...", "cost_status": "...",
                        "cost_complete": false,
                        "unknown_cost_stage_ids": ["<id>"], "total_tokens_known_sum": 0},
  "selected_spend":   {"cost_usd_known_sum": "...", "cost_status": "...",
                        "cost_complete": false,
                        "unknown_cost_stage_ids": ["<id>"], "total_tokens_known_sum": 0},
  "per_attempt": {"<id>.att<N>": {"prompt_tokens": null, "completion_tokens": null,
                                   "reasoning_tokens": null, "total_tokens": null,
                                   "cost_usd": null, "cost_known": false}},
  "stages": { "<stage_id>": { "... legacy per-selected-attempt shape ..." } },
  "aggregate": { "... mirrors selected_spend (legacy consumers) ..." }
}
```

Rules (repaired after F-07):

- `cumulative_spend` counts **every executed attempt** — financial truth; hidden sibling
  spend would deceive the operator.
- `selected_spend` describes the currently selected attempt per stage. It is **derived at
  read time** from `per_attempt` + `selection.json`; the copy stored in `usage.json` is a
  **cache**, never the authority.
- `aggregate` mirrors the derived `selected_spend` for legacy consumers.
- **Derivation rule.** Readers (including the pre-synthesis cost gate and every UI
  projection) compute the selected summary from `per_attempt` and the current
  `selection.json`. If the two disagree, the derived value wins and the stale cache is
  reported as an integrity warning. The cost gate therefore never reads a stale cache.
- **Write ordering.** An attempt completion writes `per_attempt` + `cumulative_spend`, then
  refreshes the selected cache. A selection change writes `selection.json` first, then
  refreshes the selected cache. If the cache refresh fails or the process dies between the
  two writes, readers still derive the correct selected summary from the durable
  `per_attempt` + `selection.json`; no permanent contradiction is possible.
- Unknown cost is never interpolated. `cost_status ∈ {zero, known, partial, unknown}` and
  `unknown_cost_stage_ids` remain truthful.
- Version 1 runs keep the current single-shape `usage.json`; the v2 reader tolerates both.

## 5. events.jsonl — additive vocabulary

`EVENT_TYPES` gains, without removing or renaming anything:

- `attempt_selected` — `{stage_id, attempt_number, previous_attempt}`
- `synthesis_stale` — `{stage_id, dependency, dependency_attempt}`
- `run_cancelled` — `{reason}` (subject to HSF-4)
- `worker_regeneration_started` / `worker_regeneration_finished`
- `synthesis_rerun_started` / `synthesis_rerun_finished`

The writer stays fail-closed (`events.py:41-42`): writing an unknown type still raises.
No historical `events.jsonl` is rewritten. Nothing reads the log programmatically today, so
the extension has no reader-compatibility shim to preserve; tests assert v1 logs remain
parseable line-by-line and that new kinds append.

## 6. Readers

### 6.1 load_run_view(run_dir)

- version 1: byte-identical behavior to today (`stages/<id>.json`).
- version 2: resolve selection, then read `stages/<id>.att<N>.json` per declared stage.
- Returns the same `(run, stages_list, usage)` shape; `stages_list` order follows
  `stage_order` unchanged.

### 6.2 load_stage_view(run_dir, stage_id, attempt_number=None)

- `attempt_number` supplied ⇒ read that attempt exactly.
- `attempt_number=None` ⇒ read the selected attempt (v2) or `stages/<id>.json` (v1).
- Containment predicate unchanged.

### 6.3 Selection reconstruction / staleness helpers

`storage` (or a small helper module within the existing file) exposes a pure function that
returns, for a run directory: per-stage selected attempt, available attempt numbers,
per-stage staleness of the selected synthesis, and per-stage uncertainty inference. These
are computed from disk only.

### 6.4 CLI

- `status` — same shape/exit code; may append attempt/stale markers as additional text.
- `inspect RUN_ID STAGE_ID [--attempt N]` — reads attempt N (default selected).
- `regenerate RUN_ID STAGE_ID --model M` and `rerun-synthesis RUN_ID` — explicit headless
  parity verbs. **Headless consent form (M-5 repair):** both verbs perform zero-spend
  validation + preflight by default and **refuse to spend** unless given explicit consent
  via `--approve --actor <label>` (which constructs the `ApprovalRecord` in-process from the
  just-prepared operation snapshot) or `--approval <path>` (a pre-built record). Without
  consent they print the preflight and exit without any provider request.
- `validate`/`run` behavior and exit codes unchanged; `run` retains its legacy
  no-snapshot/no-approval path exactly.

## 7. Historical safety argument (why nothing is rewritten)

1. `persist_stage` keeps its legacy signature and behavior; a new
   `persist_stage_attempt` writes `<id>.att<N>.*`. `os.replace` remains the atomic
   primitive, but the target filename is always a **new** name for a new attempt.
2. Version 1 directories are only ever read.
3. `evidence/**` (76 tracked files, with SHA-256 pins cited in
   `docs/V0_1_FINAL_CONFORMANCE_REPORT.md:100-103`) is not touched.
4. `result.md` is rewritten **only** when selection intentionally moves to a different
   synthesis attempt — the same single-leaf contract as today, now defined as "mirror of
   the selected synthesis", not "last synthesis that ran".
5. Backup/restore, search, and app-DB authority are untouched.

## 8. Forward-compatibility obligations

- Every new document carries `schema_version`; readers reject unknown future versions
  rather than misreading them.
- New fields are optional-with-defaults; a future v3 reader must be able to read v1/v2.
- No new competing store is introduced: all campaign truth stays under the run directory.
