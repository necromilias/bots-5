# Phase 10 campaign-engine archaeology (Qwen3.8 Flash, parcel-v1 design-only)

Role: repository/campaign-engine archaeologist.
Model routing: configured route `qwen/qwen3.8-flash`; effective route observed by this agent is the
same harness-provided model identity (`qwen/qwen3.8-flash`). No effort knob is exposed to this agent;
requested `provider_maximum_if_exposed` therefore records as **not exposed / not applied**. No silent
substitution occurred.

Baseline verified live before inspection:

- `git rev-parse HEAD` → `0756904481ae884bb9e864e8e1e11fc4a27a72ff` (matches `parcel/BASELINE.json` `expected_head`).
- `git diff --quiet` → tracked tree clean; untracked drift present and preserved: `.audit-tmp4/`,
  `.verifyrun/`, `.verifyrun2/`, `AUDIT-2026-09-28-evidence-only-review.md`, `work/`.
- No mutation performed by this report. Only file written: this report path.

Zero-provider commands actually run (all offline, socket-blocked pytest, no provider contact):

- `.venv/bin/python -m pytest tests/test_storage_events.py tests/test_manifest.py tests/test_runner.py
  tests/test_cli_views.py tests/test_audit_blockers.py tests/test_provider.py tests/test_local_provider.py
  tests/test_prompts.py tests/test_rendering.py` → **115 passed in 0.48s**, exit 0 (Python 3.14.7 venv).
- Same selection with `--collect-only -q` → non-zero collection confirmed.
- `python3 -c` JSON reads of retained historical run directories under `evidence/**` (read-only).

Legend: **[F]** = fact read from pinned bytes at the baseline commit. **[I]** = inference from those
bytes (explicitly labelled). **[X]** = cross-source conflict or gap requiring supervisor attention.

---

## 0. Executive orientation

**[F]** The repository contains two execution surfaces that do **not** touch each other in code:

1. the closed V0/V0.2 headless campaign harness — `src/bots5/{manifest,models,runner,storage,events,usage,paths,prompts,rendering,cli}.py`
   plus `src/bots5/providers/*` (`docs/ARCHITECTURE.md:5-11`, `docs/DEVELOPMENT.md:5-9`);
2. the Linux v0.1 native desktop product — `src/bots5/{core,desktop,bootstrap,domain,infrastructure}/*`.

The only campaign-module symbols imported by any desktop/core/bootstrap module are
`bots5.providers.base.ReasoningEffort`, `bots5.providers.openai_compatible.OpenAICompatibleProvider`,
and `bots5.providers.discovery` (`grep` over `src/bots5/desktop src/bots5/core src/bots5/bootstrap`:
`window.py:47`, `application.py:73`, `bootstrap/desktop.py:36-37`,
`infrastructure/generation/openai_compatible.py:17`). **[F]** There is **no** import of
`bots5.runner`, `bots5.storage`, `bots5.manifest`, `bots5.models`, `bots5.usage`, `bots5.events`,
`bots5.paths`, `bots5.rendering` or `bots5.prompts` anywhere outside `src/bots5/cli.py` and the
campaign test files. **[F]** Consequently there is currently **zero** campaign surface in the desktop:
`grep -n "campaign\|jobs" src/bots5/desktop/window.py` → no matches.

**[I]** Phase 10 is therefore a genuinely new seam, not a modification of an existing integration. The
existing engine is fully reconstructable from filesystem artifacts alone, and the CLI is its only
in-process consumer today.

---

## 1. Exact `StageRecord` / stage-output model — where one-record-per-stage is encoded

### 1.1 The record itself

**[F]** `src/bots5/models.py:89-145` — `@dataclass class StageRecord` (mutable, not frozen):

- identity fields `id`, `provider`, `requested_model` (lines 91-93);
- lifecycle `state: StageState = StageState.QUEUED` (line 94), `started_at/ended_at/duration_seconds`
  (96-98);
- telemetry `prompt_tokens/completion_tokens/reasoning_tokens/total_tokens` (99-102),
  `known_cost_usd: Decimal | None` (103), `request_id` (104);
- **`output_path: str | None = None`** (line 105) — exactly one output path per record;
- completion truth `finish_reason`, `completion_complete` (106-107);
- failure truth `error_type`, `error_message`, `provider_side_outcome_unknown` (108-110).

**[F]** `to_dict()` (lines 112-145) emits key `"stage_id"` (not `"id"`), a nested `"usage"` object,
`"cost_usd"` as a decimal **string** or `null` paired with `"cost_known": bool` (128-129),
`"provider_request_id"`, `"output_path"`, and optional nested `"completion"` / `"failure"` objects
(132-144). There is **no** attempt index, no sequence number, no parent/sibling pointer, and no
schema/version field inside the stage document.

**[F]** `RunResult` (models.py:164-170) carries `stages: tuple[StageRecord, ...]` — a flat tuple keyed
implicitly by order, one entry per declared stage.

### 1.2 Where "one record per stage id" is constructed

**[F]** `runner._all_stage_records(job)` (runner.py:218-228): builds exactly `len(job.workers)` records
plus one synthesis record, keyed by `spec.id`. Nothing can produce two records for one id.

**[F]** `runner.run_job` line 275: `record_by_id = {record.id: record for record in records}` — a dict
keyed by stage id. Later lookups (`runner.py:313`, `:338`, `:345`, `:364`, `:382`, `:419`, `:422`,
`:436`, `:446`) all assume `record_by_id[<id>]` is unique and total.

**[F]** Manifest validation enforces uniqueness upstream: duplicate worker ids rejected
(`manifest.py:309-312`), synthesis id may not collide with a worker id (`manifest.py:317-318`).

### 1.3 Where it is written

**[F]** `storage.persist_stage(dirs, stage, text=None)` (storage.py:119-125):

```
validate_stage_id(stage.id)
if text is not None:
    rel = Path("stages") / f"{stage.id}.md"
    stage.output_path = str(rel)          # mutates the record in place
    atomic_write_text(dirs.root / rel, text)
atomic_write_json(dirs.stages / f"{stage.id}.json", stage.to_dict())
```

Both target filenames are derived **only** from `stage.id`. A second write for the same id
**overwrites** the previous JSON via `os.replace` (storage.py:71). This is the single most important
Phase 10 constraint: the storage layer has no notion of "attempt"; append-only sibling attempts cannot
be expressed through `persist_stage` unchanged.

**[F]** `storage.persist_run(...)` (storage.py:134-159) writes `run.json` containing
`"stage_order": [stage.id for stage in stages]` (line 155) and
`"stages": {stage.id: stage.to_dict() for stage in stages}` (line 156) — again a **dict keyed by stage
id**, so a duplicate id would silently collapse in the aggregate document even if per-file writes were
changed.

**[F]** `storage.persist_usage` (storage.py:128-131) + `usage.usage_document` (usage.py:36-68) build
`per_stage[stage.id]` (usage.py:43) and aggregate sums over the list; unknown-cost ids come from
`aggregate_cost` (usage.py:9-33). Same one-key-per-stage assumption.

**[F]** `result.md` is written once from the synthesis output: `runner.py:416-417`
(`persist_result(dirs, synthesis_output)` → `storage.py:162-163`, fixed leaf name `result.md`).

### 1.4 Where it is read

**[F]** `storage.load_run_view(run_dir)` (storage.py:175-185): requires `run.json` + `usage.json`, then
walks `run["stage_order"]` and reads `stages/<stage_id>.json` for each id (lines 180-184). It validates
each id with `validate_stage_id` before path construction (line 183). It returns
`(run, stages_list, usage)`; the list order is authoritative display order.

**[F]** `storage.load_stage_view(run_dir, stage_id)` (storage.py:188-203): reads
`stages/<stage_id>.json`, then follows `meta["output_path"]` and asserts containment
(`candidate.parent != (run_dir/"stages").resolve()` → `ValidationError("stage output path escapes run
directory")`, lines 196-198). Note the containment rule is **exactly one directory level**: a Phase 10
attempt layout such as `stages/w1/attempts/a2.md` would be **rejected** by this check as it stands.
**[I]** Any attempt-path redesign must either keep outputs directly in `stages/` with distinct filenames
or change this predicate deliberately and version-gated.

**[F]** CLI readers: `cli._cmd_status` (cli.py:115-140) iterates the `stages` list from `load_run_view`
and prints one line per stage; `cli._cmd_inspect` (cli.py:143-165) takes exactly one `(run_id, stage_id)`
pair and prints `meta['stage_id']`, `meta['requested_model']`, `meta['state']`, completion, the whole
`metadata` dict, the output body, and the failure block.

### 1.5 Where it is assumed by tests

**[F]** Direct one-key-per-stage assertions in the campaign tests:

- `tests/test_runner.py:127-137` `test_completion_metadata_survives_disk_reconstruction` —
  `run["stages"]["w1"]["completion"] == expected`, `stages[0]["completion"]`, `stage["completion"]`,
  i.e. three independent readers of the same single record.
- `tests/test_runner.py:140-177` `test_incomplete_empty_output_persists_metadata_and_fails_run` —
  reads `stages/w1.json` and asserts `stages/w1.md` exists and is empty.
- `tests/test_runner.py:49-59` — asserts `stages/w1.md`, `stages/w2.md`, `result.md` contents.
- `tests/test_runner.py:243-258` `test_worker_failure_preserves_sibling_and_blocks_synthesis` reads
  `run.json` state; `:259-282` `dependency_incomplete`; `:284-301` cost-threshold skip.
- `tests/test_runner.py:321-332` per-stage timeout, `:334-342` overall timeout.
- `tests/test_cli_views.py:86-103` `test_status_and_inspect_from_disk_only` — status exit 1 for failed
  persisted run, inspect exit 0 and prints `--- output ---`.
- `tests/test_storage_events.py:9-22` — `new_run_id` uniqueness and `events.jsonl` validity only.

**[F]** `tests/helpers.py:29-63` `FakeProvider` is the deterministic zero-spend provider used everywhere;
`make_job_tree` (helpers.py:66-116) writes a schema-**v1** job with `runs_dir: "./.bots5/runs"`.

### 1.6 What breaks if attempt semantics are added

**[I]** Impact ordered by certainty, all grounded in the call sites above:

1. `storage.persist_stage` filename derivation (storage.py:122-125) — must gain an attempt discriminator
   without changing behavior for legacy single-attempt runs.
2. `storage.persist_run` `stages` dict (storage.py:156) — collapses duplicates; needs either a nested
   attempts map or a separate current-selection document.
3. `storage.load_run_view` `stage_order` walk (storage.py:180-184) — assumes one file per id.
4. `storage.load_stage_view` containment check (storage.py:196-198) — rejects nested output paths.
5. `runner.record_by_id` and every `record_by_id[...]` lookup (runner.py:275, 313, 338, 345, 364, 382,
   419, 422, 436, 446) — the in-memory graph is id-keyed.
6. `usage.usage_document` `per_stage[stage.id]` (usage.py:43) and `aggregate_cost` unknown-id list
   (usage.py:11) — cost/token aggregation keys on id; two attempts of `w1` would double-count or
   overwrite depending on choice.
7. `cli._cmd_status` one-line-per-stage loop (cli.py:120-132) and `cli._cmd_inspect(run_id, stage_id)`
   argument shape (cli.py:35-38, 143-165) — no way to address an attempt today.
8. `RunResult.stages` tuple contract consumed by `_print_result` (cli.py:55-70) and tests.
9. `synthesis.depends_on` gating resolves purely by id (`runner.py:344-346`, `:363-365`, `:404`) — see §10.

---

## 2. Run-directory schema, identity, discovery, versioning

### 2.1 Identity and creation

**[F]** `storage.new_run_id(job_name)` (storage.py:36-38): `<slug(name)>-<UTC %Y%m%dT%H%M%SZ>-<uuid4hex8>`;
`_slug` (storage.py:31-33) restricts to `[A-Za-z0-9._-]`, truncates to 48 chars, falls back to `"run"`.
Uniqueness is tested only by `test_unique_run_ids` (tests/test_runner… actually
`tests/test_storage_events.py:9-10`).

**[F]** `storage.create_run_tree(runs_dir, run_id)` (storage.py:94-112): validates the id
(`paths.validate_run_id`, regex `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, paths.py:11,21-23), rejects a
symlinked `runs_dir` (98-99), uses bare `os.mkdir` for `run_dir` and `stages/`, `events.touch(exist_ok=False)`,
fsyncs both directories, and converts `FileExistsError` into `StorageError("run directory already exists")`
(109-110). **[I]** So a run id is a hard one-shot namespace claim: re-running the same id fails rather
than resuming — which is precisely why regeneration must create a *sibling* artifact, not reuse the id.

### 2.2 The landed layout

**[F]** Documented in `docs/OBSERVABILITY.md:5-15` and produced by `create_run_tree` + the persist
functions:

```text
<RUNS_DIR>/<run-id>/
  run.json              storage.persist_run       (storage.py:134-159)
  job.resolved.json     storage.persist_resolved_job (storage.py:115-116) -> manifest.job_to_dict
  events.jsonl          created empty by create_run_tree; appended by EventWriter
  usage.json            storage.persist_usage      (storage.py:128-131)
  stages/<id>.json      storage.persist_stage
  stages/<id>.md        storage.persist_stage (only when text is not None)
  result.md             storage.persist_result      (storage.py:162-163)
```

**[F]** `run.json` keys, verified against real bytes in three retained historical directories
(`evidence/v0.1-completion-telemetry/*/run.json`, `evidence/v0.1-final-worker-boundary/*/run.json`,
`evidence/v0.1-live-conformance/*/run.json`): exactly
`['ended_at','run_id','run_timeout_seconds','stage_order','stages','started_at','state','synthesis_skipped_reason','usage']`.
There is **no** `schema_version`/`evidence_version` field inside `run.json`. **[X]** Versioning of the
*run directory* is implicit: the only explicit version is the **job** `schema_version` (1 or 2) recorded
inside `job.resolved.json` (`manifest.py:270-272`, `:409`).

**[F]** `job.resolved.json` content rules: `manifest.job_to_dict` (manifest.py:391-432) emits absolute
resolved paths for every input/prompt (`str(spec.system_prompt_path)`, `str(i.path)`, `:397`, `:411`)
and adds a `providers` block **only for schema v2** (manifest.py:425-431), containing only
`base_url` + `api_key_env` name — never the resolved secret. Proven by
`tests/test_runner.py:373-400` (secret absent from every file in the tree) and
`tests/test_runner.py:457-507` (v2 non-secret config persisted; explicit null `api_key_env` preserved).

### 2.3 Discovery and reading of historical runs

**[F]** `paths.locate_run_dir(run_id, runs_dir=None)` (paths.py:52-62): base defaults to
`Path.cwd()/".bots5"/"runs"`; resolves both, then requires `os.path.commonpath([base, run_dir]) == base`
— otherwise `ValidationError("run directory resolves outside runs directory")`. **[F]** There is **no**
listing/enumeration API: nothing in `src/` scans a runs directory to find recent runs. Discovery is
purely operator-supplied (`--runs-dir`, run id printed by `run`). **[I]** A desktop "load an existing
campaign job / inspect durable results" surface therefore has no existing programmatic run-discovery
seam to reuse; it must introduce one (or require the operator to point at a directory).

**[F]** Path-semantics asymmetry, documented twice and enforced in code: manifest `output.runs_dir`
resolves relative to the **job file** (`manifest.resolve_runs_dir` via
`paths.resolve_job_relative`-style base, manifest.py:326; paths.py:33-39), while CLI
`status/inspect --runs-dir` resolves relative to the **current working directory** (paths.py:54). Recorded
at `docs/OPERATING_PROCEDURE_V1.md:121-127`, `docs/OBSERVABILITY.md:66-70`, `docs/JOB_SPEC.md:106-112`.
**[I]** A desktop process has a different cwd than the operator's shell; this asymmetry is a live
integration trap for Phase 10.

**[F]** Runs-directory safety beyond that: symlink rejection before resolution (paths.py:37-38, tested at
`tests/test_manifest.py:172-183`), filesystem-root rejection (paths.py:45-46), "must be a directory if it
exists" (paths.py:47-48), `_atomic_write` refuses to replace a symlink target (storage.py:53-54), and
JSON serialization forbids NaN (`allow_nan=False`, storage.py:84).

### 2.4 Old-run compatibility: what actually proves it today

See §11. **[F]** The strongest existing proof is `tests/test_runner.py:127-137` (disk reconstruction) plus
`tests/test_cli_views.py:86-103` (status/inspect purely from disk), and the three retained real OpenRouter
run trees under `evidence/` (76 tracked files, `git ls-files evidence | wc -l` → 76) whose `run.json`
shape matches the current writer exactly. **[I]** Because the historical shape equals the current writer's
shape, no reader-side compatibility shim exists yet — meaning Phase 10 will be the **first** change to this
schema and there is no existing versioned-reader machinery to imitate inside the campaign package.

---

## 3. Job input/prompt handling, rereads, and the TOCTOU window

### 3.1 Which fields are filesystem paths

**[F]** `InputSpec.path: Path` (models.py:26-28); `WorkerSpec.system_prompt_path: Path` (models.py:36);
`SynthesisSpec.system_prompt_path: Path` (models.py:47); `OutputConfig.runs_dir: Path` (models.py:62-63).
All are resolved to absolute paths at parse time (`manifest.py:226`, `:259`, `:291`, `:326`) with
`resolve(strict=False)`, so **non-existence is allowed at parse time**.

### 3.2 When referenced bytes are re-read

**[F]** Two independent read passes:

1. Validation pass — `manifest.validate_referenced_files(job)` (manifest.py:378-388) calls
   `_validate_text_file` (manifest.py:355-367: existence, regular-file, `os.access(R_OK)`, UTF-8 decode)
   for each input, and `_validate_worker_contract_file` (manifest.py:370-375) which additionally parses
   the six-section contract (`prompts.parse_worker_contract`, prompts.py:29-64).
   Called by `cli._cmd_validate` (cli.py:74-77) and again by `cli._cmd_run` (cli.py:107-108) and by
   `runner.run_job` line 259.
2. Execution pass — `runner._read_utf8(path)` = `path.read_bytes().decode("utf-8")` (runner.py:29-30),
   invoked at runner.py:265 for every system prompt and runner.py:267 for every input, **after**
   `validate_referenced_files` and **before** `create_run_tree` (runner.py:271).

**[F]** Ordering inside `run_job`: `validate_referenced_files` (259) → provider-mapping check (260) →
compile system messages + read inputs (264-268) → `create_run_tree` (271) → persist resolved job/usage/run
(279-289) → `run_started` event (290) → per-stage queue persists (291-293) → semaphore pipeline (306+).

Proved by tests: `tests/test_runner.py:205-217` (invalid contract ⇒ no provider call, `runs_dir` never
created) and `tests/test_audit_blockers.py:35-54` (reread failure ⇒ run tree does not exist).

### 3.3 The TOCTOU window

**[F]** The window is exactly between the byte reads at runner.py:265-267 and the moment the request body
is handed to the transport at runner.py:156 (`await asyncio.wait_for(provider.complete(request), …)`), plus
the earlier window between any UI/CLI `validate` and `run_job`'s own reread.

Concretely, the bytes that reach the provider are the values captured at 265-267, but:

- the *pathname* approved earlier may have been swapped (symlink/file replacement) after approval;
- `runs_dir` is validated at parse time (manifest.py:326) and re-checked only for symlink/root/type;
  `create_run_tree` re-checks the symlink at storage.py:98-99 — the directory could change in between;
- `CompletionRequest` (providers/base.py:12-20) is built fresh per stage from `spec.model`,
  `system_message`, `user_message`, `temperature`, `max_output_tokens`, `timeout_seconds`
  (runner.py:146-153) — none of these are digest-pinned anywhere;
- the worker user message is shared across all workers (`worker_user` computed once at runner.py:268)
  and the synthesis user message is built from in-memory `outputs` (runner.py:404-405), so a mid-run file
  change affects only stages started after the change. **[I]** This makes a "validate once, approve once"
  UI unsound without an explicit content digest.

**[F]** There is **no** hashing/digest/snippet-pin of referenced bytes anywhere in the campaign package:
`grep` for `sha256|digest` in `src/bots5/{manifest,runner,storage,models,usage,events,paths}.py` → no
matches. The digest machinery that exists (`core/interchange.py:111 sha256_hex`, attachment content
addressing) belongs to the desktop side only. **[I]** F-03 in `parcel/FINDINGS.md` is confirmed by bytes:
approval binding needs a mechanism the campaign harness does not currently possess.

### 3.4 Provider-route/model material at execution

**[F]** Provider selection is `providers[spec.provider]` at runner.py:320 (workers) and `:411`
(synthesis), chosen by the job's declared string; `_validate_provider_mapping` (runner.py:231-250) checks
presence and `callable(getattr(p,"complete",None))` before any run directory exists
(tests/test_runner.py:439-445, `:448-455` proves the old single-provider form is removed). Credentials are
resolved when the CLI **constructs** providers, before `run_job`: `cli._build_providers` (cli.py:87-103)
reads `OPENROUTER_API_KEY` (cli.py:91-94) and instantiates `OpenAICompatibleProvider` whose constructor
resolves `api_key_env` immediately (openai_compatible.py:70-74). Tests:
`tests/test_cli_views.py:25-29` and `:77-84` (missing credential ⇒ exit 1, no run dir).

---

## 4. Approval / spend surfaces: what gates a provider request today

**[F]** There is **no preflight, no approval, no spend gate, and no confirmation** in the campaign engine.
Evidence:

- `cli._cmd_run` (cli.py:106-112) goes straight from load → validate → build providers →
  `asyncio.run(run_job(...))`. No human step, no flag, no env gate.
- Inside `run_job`, the first thing that touches the network is runner.py:156
  `await provider.complete(request)`, preceded only by `events.write("request_sent", record.id, model=...)`
  (runner.py:154) and the semaphore acquisition (runner.py:140). Nothing between validation and that line
  can refuse on cost or authority.
- The only cost-bearing control is post-hoc and pre-synthesis:
  `stop_before_synthesis_if_known_cost_exceeds_usd` compared against `aggregate_cost(worker_records)`
  at runner.py:383-402, skipping synthesis with reason `known_cost_threshold_exceeded` and failing the run.
  `docs/JOB_SPEC.md:44-49`, `docs/EXECUTION.md:23-26`, `docs/SECURITY.md:20-21` all state explicitly this is
  a synthesis gate, **not** a whole-run budget, and unknown/partial cost does not block synthesis.

**[F]** In the desktop half of the repo there *are* consequential-action precedents worth preserving as
patterns (not as campaign code):

- explicit consequence modal before an irreversible action:
  `RestoreHandoffDialog.confirm_consequence()` (phase9_dialogs.py:1380-1405) and
  `BackupCreationDialog` overwrite confirmation (phase9_dialogs.py:870-875, :919-948);
- register-then-close handoff where registration alone does nothing
  (`RestoreHandoffRegistry.register` one-shot, bootstrap/desktop.py:68-100; `RestoreHandoffCoordinator.run`
  S2→S3→S5→S6, phase9.py:900-984);
- `MainWindow.closeEvent` active-generation confirm (window.py:1874-1895).

**[I]** For Phase 10 the honest statement is: the spend decision today lives entirely in the operator's
choice to run `bots5 run`, and OPv1 §4 (`docs/OPERATING_PROCEDURE_V1.md:62-75`) is a *human* procedure,
not code. Any desktop preflight/approval must therefore be additive and must bind to something the engine
does not currently produce (see §3.3).

### 4.1 Preflight pricing authority — confirmed absent

**[F]** No campaign pricing registry, lookup, cache, or operator-rate entry point exists in `src/bots5/*`:
`grep -rn "pricing|price" docs/*.md` yields only procedure prose (`OPERATING_PROCEDURE_V1.md:36,66`,
`EXECUTION.md:50`) and historical reports. The historical `preflight.json` files under
`evidence/v0.1-final-worker-boundary/*/` contain a `pricing` block with rates scraped by the **human
campaign** from `https://openrouter.ai/api/v1/models` plus a `cost_bounds` block — verified by direct read.
No Python code reads or writes `preflight.json` (`grep -rn "preflight.json" --include=*.py src tests` →
no matches). **[X]** This confirms FINDINGS F-06: preflight pricing authority is unresolved and is a genuine
HUMAN_SEMANTIC_FORK candidate for the supervisor; the historical evidence shows the operator already
performs manual rate capture out-of-band.

---

## 5. Provider / cost behavior

### 5.1 Where cost comes from

**[F]** Cost is **provider-reported only**, taken from `usage.cost` in the HTTP response and normalized by
`_optional_cost` (openrouter.py:27-36, openai_compatible.py:28-37): rejects `None`, `bool`,
non-finite and negative values, converting them to `None` (= unknown). Tokens go through `_optional_int`
(openrouter.py:21-24, openai_compatible.py:22-25) which also bounds to SQLite int64 max. Normalization is
shared and parity-tested (`tests/test_local_provider.py:267` `test_openrouter_and_local_normalization_parity`).

**[F]** Assignment happens once, at terminal completion: `_apply_result(record, result)`
(runner.py:39-49, called at `:192`) copies `returned_model`, `request_id`, `finish_reason`,
`completion_complete = (finish_reason == "stop")` (line 43), the four token counts, `known_cost_usd`, and
`duration_seconds`. On every failure branch (timeout `:157-168`, `ProviderError` `:169-179`, other
exception `:180-190`) none of these are set, so cost stays `None` = unknown.

**[F]** Campaign providers are **non-streaming for the harness**: `Provider` protocol exposes only
`async complete()` (providers/base.py:51-53). `StreamingProvider` exists (base.py:56-58) and both concrete
providers implement `stream()` (openrouter.py:246-284, openai_compatible.py:267-306), but **the campaign
runner never calls `stream()`** — `runner.py` imports only `CompletionRequest/CompletionResult/Provider`
(lines 14). The desktop generation backend is the sole `stream()` consumer
(`infrastructure/generation/openai_compatible.py:120` `async for chunk in self._provider.stream(...)`,
yielding `GenerationMetadata` with `known_cost_usd` incrementally, lines 141-158). **[F]** This confirms
F-07: campaign "live cost progress" cannot be obtained without adopting streaming, which CONTRACT.md
excludes ("provider streaming redesign merely for UI progress" is an explicit non-goal).

### 5.2 When durable cost evidence appears

**[F]** `usage.json` is written three times: initial all-unknown snapshot at runner.py:280 (before any
request), after internal-failure best-effort (`_best_effort_internal_failure`, runner.py:83), and finally at
runner.py:484. `run.json` is written at start (runner.py:281-289, state `running`) and at terminal
(runner.py:489-498). Per-stage JSON is rewritten at each transition: queued (`:292`), running (`:144`),
terminal success-with-text (`:197`), failure (`:166/:177/:188`), cancel (`:211`), timeout sweep (`:442`,
`:461`), synthesis skip (`:353`, `:372`, `:393`).

**[F]** Unknown cost in durable evidence looks like: stage JSON `"cost_usd": null` + `"cost_known": false`
(models.py:128-129); `usage.json` per-stage same pair (usage.py:48-49) plus aggregate
`cost_usd_known_sum`, `cost_status ∈ {zero, known, partial, unknown}`, `cost_complete`, and
`unknown_cost_stage_ids` (usage.py:59-68). Status vocabulary defined at `docs/OBSERVABILITY.md:53-62`.
Skipped-before-request stages get **harness-known zero** `Decimal("0")` (runner.py:350, `:369`, `:388`,
`:450`) and OBSERVABILITY.md:36-38 states explicitly that this is *not* a provider-reported zero.

**[F]** Tests pinning this: `tests/test_runner.py:303-319` (`cost_status == "partial"`, `"w2"` in
`unknown_cost_stage_ids`), `tests/test_local_provider.py:128` (`parses_usage_and_leaves_unknown_cost_unknown`),
`tests/test_runner.py:140-177` (cost `"0.0042"` as string alongside incomplete completion).

**[I]** Truthful "live cost progress" for Phase 10 therefore means: known subtotal + explicit unknown set,
recomputed from `usage.json`/stage documents as they land — exactly the projection `cli._cmd_status` already
performs (cli.py:133-139). Fabricating per-token dollar accrual would contradict both the code and
OBSERVABILITY.md.

### 5.3 Cross-contamination caution

**[F]** The desktop's own `GenerationAttempt.known_cost_usd` / `remote_outcome_unknown`
(domain/models.py:97-98) is a **different** persistence universe (SQLite store), with its own uncertainty
discipline: `remote_outcome_unknown=True` is stamped on dispatch (application.py:2363-2366) and preserved
through abort/reconcile (application.py:2694-2696; sqlite.py:7984-8016
`reconcile_interrupted_generations`). **[I]** Phase 10 must not let desktop attempt bookkeeping become a
second cost truth for campaign runs (CONTRACT.md "filesystem evidence remains authoritative").

---

## 6. Event durability

**[F]** `src/bots5/events.py`:

- closed vocabulary `EVENT_TYPES` (lines 13-25): `run_started`, `stage_queued`, `stage_started`,
  `request_sent`, `stage_succeeded`, `stage_failed`, `stage_skipped`, `synthesis_blocked`,
  `run_timed_out`, `run_succeeded`, `run_failed`. `EventWriter.write` raises `StorageError` on an unknown
  type (lines 41-42) — the vocabulary is fail-closed, not extensible-by-default.
- Each line is `{ts, run_id, event, stage_id?, meta?}` compact JSON (`separators=(",",":")`,
  `allow_nan=False`), appended under a `threading.Lock`, `flush()` + `os.fsync()` per append (lines 52-58).
- Events carry small metadata only; full prompts/outputs are never written here (verified by the call sites:
  e.g. `events.write("request_sent", record.id, model=spec.model)` runner.py:154;
  `synthesis_blocked` carries reason + dependency lists, runner.py:355-360, `:374-380`, `:395-401`).

**[F]** Reading: **there is no reader.** `grep -rn "events.jsonl" --include=*.py src tests` matches only
`storage.create_run_tree` (touch) — nothing parses the log back. The only test is
`tests/test_storage_events.py:13-22` (two appends produce two valid JSONL lines).

**[F]** Historical events verified live: `head -3 evidence/v0.1-final-worker-boundary/*/events.jsonl`
shows `run_started` then per-stage `stage_queued` lines matching the current writer exactly.
`docs/FIRST_LIVE_SMOKE_REPORT.md:52` and `docs/V0_1_FINAL_CONFORMANCE_REPORT.md:78-80` record event-order
review by humans.

**[I]** Durability classification: events are an **append-only narrative** and are *not* authoritative for
final state — final state is `run.json` + stage JSON + `usage.json`, which are rewritten atomically. But
they are the only artifact that preserves *transition ordering and timing*, which the aggregate documents
cannot reconstruct. So: projections for UI may read them; correctness decisions must read the JSON
documents. **[I]** For Phase 10 regeneration, `events.jsonl` is the natural append-only precedent to extend
(new event types would need `EVENT_TYPES` additions — a closed-set change, hence a versioned-schema
decision, not a free-form one).

---

## 7. Desktop/core lifecycle and shutdown

### 7.1 Startup composition

**[F]** `bootstrap/desktop.py:main` (lines 955-1006): argparse (`--data-root/--backend
fake|local_openai/--base-url/--model/--api-key-env/--reasoning-effort none/--restore-from/
--expected-backup-id`, lines 885-952); `--restore-from` short-circuits to `_initiate_restore` **before any
Qt import** (lines 958-963, docstring 640-671 with exit codes 0/1/2/3). Otherwise:
`QApplication` + `QEventLoop(qt_application)` (965-970), `setQuitOnLastWindowClosed(False)` (972-974),
`build_runtime(...)` (979-986), then `with event_loop: event_loop.run_until_complete(serve(runtime))`
(988-990). Exit code precedence: `runtime.restore_exit_code` if set, else 0 (995-998).

**[F]** `build_runtime` (lines 516-617): `resolve_app_paths` → `_prepare_data_root_topology` (494-513,
creates data root mode 0o700 then non-authoritative dirs) → `AuthorityLock(paths.data_root).acquire()`
(520; note `AuthorityLock` is just an alias of `DataRootAuthority`, infrastructure/authority_lock.py:1-8)
→ `RestoreStartupCoordinator.before_store_open()` (528-532) → `authority.open_store()` (534) →
`SystemClock`, `Uuid7Factory`, `EventBus` (535-537) → backend selection: `fake` ⇒
`BuiltinProviderRouter(fake_backend=FakeStreamingBackend())` (540-546); `local_openai` ⇒
`OpenAICompatibleProvider` wrapped in `OpenAICompatibleStreamingBackend` (547-565) →
`ProviderConfiguration` only in fake mode (566-570) → `GenerationMode.CONFIGURED` vs
`LEGACY_PHASE3_LOCAL_OPENAI` (571-575) → `BackupService` (576-586) → `BotsApplication(...)` (587-604) →
`DesktopSessionInfo` (605-611) → `DesktopRuntime(paths, authority, application, session,
DesktopSessionController(...))` (612-617); any exception releases authority (615-617).

**[F]** `serve(runtime)` (lines 823-866): `runtime.application._ensure_import_scheduler()` (833) resumes
durable queue work before UI; loads workspace windows with `restore_open` (834-838, default one window);
opens each via `runtime.open_window(state)` (842); `await workspace.wait_closed()` (843); `finally:`
`await runtime.close()`, with a CancelledError-safe double-close (845-857), then
`await _run_restore_handoff_post_close(runtime)` (866) only on the non-cancelled path.

### 7.2 asyncio structure and task ownership

**[F]** Three distinct ownership sets:

1. `ExecutionManager` (core/execution.py:10-42) — owns generation coroutines;
   `start(coroutine, name)` refuses after close (17-24); `shutdown()` marks closed, cancels all unfinished
   tasks, gathers with `return_exceptions=True`, and re-raises the first non-CancelledError failure (26-42).
   Generations start through `self._execution.start(self._run_generation(...), name=f"bots5-generation-{attempt.id}")`
   (application.py:1630-1641).
2. `OwnedImportWorkers` (core/import_queue.py:181-268) — the cutoff pattern: preflight is cancellable;
   after `cutoff` the settlement task is `asyncio.shield`-ed and caller cancellation is absorbed with
   `current.uncancel()` and reported only after durable settlement (221-236); `shutdown()` cancels only
   pre-cutoff runners and drains everything else (258-268). Application wires preflight/cutoff/settle at
   application.py:1879-1951 using `Context().run(asyncio.to_thread(...))` fresh-context offloads.
3. Per-window `_refresh_tasks` (window.py:130; `_schedule` at 1801-1807) and controller `_tasks`
   (phase9.py:118-140 `schedule`, `wait_idle`, `close`).

**[F]** `MainWindow._schedule` drops the coroutine if `self._workspace_attached` is False (1801-1804);
`stop_bridge()` (1809-1819) detaches signals, stops the bridge when owned, cancels+clears `_refresh_tasks`,
and calls `phase9.close()`. `closeEvent` (1874-1895) asks the operator only for the **last** registered
window when `application.has_active_generations()` (1878-1892; `has_active_generations` =
`_pending_generations` or `_import_scheduler` alive, application.py:404-407), then accepts and schedules
`_finish_close()` (1897-1938) which stops the bridge, awaits refresh tasks, and unregisters the window
persisting geometry/selection/inspector state (session.py:144-199).

**[F]** Close drivers are single-flight, loop-pinned and error-aggregating at all three levels:

- `BotsApplication.close()` (application.py:2753-2775) → `_close_driver` (2634-2736): cancel import
  scheduler → `_import_workers.shutdown()` → `_execution.shutdown()` → `_commands_idle.wait()` →
  reconcile still-`RUNNING`/`STREAMING` attempts to `ABORTED` with `error_type="aborted"` and preserved
  `remote_outcome_unknown` (2660-2706) → `_store.close()`; errors sorted by `_CLOSE_PRECEDENCE`
  `{store:0, imports:1, execution:2, reconciliation:3, events:4}` (110-116); terminal result stored, state
  CLOSED/FAILED (2728-2736). Reconciliation failure leaves durable RUNNING state deliberately
  ("durable RUNNING state is deliberately left for a fresh authority at restart", comment 2656-2659).
- `DesktopSessionController.close()` (session.py:222-239) drives `bridge.stop_async()`
  (bridge.py:45-51) through one shielded task, raising `StateError` on failure.
- `DesktopRuntime.close()` (bootstrap/desktop.py:340-361) → `_close_driver` (275-331): cancels
  `_opening_windows`, closes workspace, closes application (merging its errors), releases authority **last**
  (299-304), with its own precedence table (306-316).

**[F]** Cancellation of one generation: `cancel_generation(attempt_id)` (application.py:1657-1714) sets
`_cancel_requested`, cancels the task, waits for the terminal-persisted event, and returns the **stored**
attempt — i.e. it awaits until the aborted state is durable. `_run_generation` checks
`if attempt.id in self._cancel_requested: raise asyncio.CancelledError` per event (2363-2364) and stamps
`remote_outcome_unknown` from `dispatch_may_have_occurred` (2363-2366, 2600-2617). `docs/EXECUTION.md:56-65`
states the same contract, including "late backend deltas are ignored after cancellation, and aborted or
uncertain work is never retried automatically".

### 7.3 What happens to in-flight provider work on close

**[F]** Desktop path: the task is cancelled; the HTTP stream is closed where the transport permits; partial
text already persisted stays authoritative; attempt becomes `ABORTED` with truthful
`remote_outcome_unknown`. Crash-restart path reconciles leftover RUNNING attempts at store open
(application.py:316 `self._store.reconcile_interrupted_generations(...)`, implementation
sqlite.py:7984-8016).

**[F]** Campaign path: **there is no campaign integration with this machinery at all.** `run_job` is driven
by `asyncio.run(...)` in `cli._cmd_run` (cli.py:110) — a separate process/loop from the desktop. Inside the
campaign runner, cancellation is handled only by the run-level timeout: `asyncio.wait_for(pipeline(),
run_timeout_seconds)` (runner.py:427) whose `TimeoutError` branch cancels worker tasks, gathers them,
marks unfinished workers `failed/run_timed_out` (435-443) and synthesis `skipped` or
`failed + provider_side_outcome_unknown=True` (445-461), then persists `timed_out` (463-464, 489-498).
`_execute_stage`'s `except asyncio.CancelledError` (runner.py:201-215) marks the record failed with
`provider_side_outcome_unknown = started_monotonic is not None` and **re-raises**.

**[X]** Critical gap for Phase 10: if a desktop-hosted campaign task is cancelled by *window/runtime close*
rather than by the run timeout, `run_job`'s outer `except Exception` (runner.py:465-480) does **not** catch
`asyncio.CancelledError` (it derives from `BaseException` in Python ≥3.8). So a desktop-driven cancellation
would leave `run.json` durably stating `state: "running"` with possibly-`running` stage records — exactly
the F-11/CONTRACT "crash may leave a durable run that says running" case, but now reachable through normal
UI shutdown, not only a crash. **[I]** This is a concrete, code-grounded finding the lifecycle specialist
must address: either the desktop must drive `run_job` to a terminal persist before allowing close, or the
engine must treat cancellation as a terminal-writing path (today only timeouts and internal errors do that).

---

## 8. UI integration seams (what exists to reuse)

**[F]** Shell composition available for reuse, all in `desktop/window.py`:

- `MainWindow.__init__(application, session, workspace=..., window_state=..., *, handoff=None)`
  (window.py:100-166) — keyword-injected capability, no module singleton (documented rationale at
  113-117 and bootstrap/desktop.py:102-115).
- `_build_ui` docks: `inspector_dock` right (337-345), `search_dock` right (347-361),
  `import_queue_dock` bottom/left/right (365-374) attached through
  `self._phase9.attach_queue_dock(...)` (374, phase9.py:344-368).
- `_build_phase9_menus` (379-457) adds File / View / Tools menu entries purely by delegating to the
  controller; chat-rail context actions wired from `LeftRail` signals (widgets.py:1475-1482).
- `TopBar` (widgets.py:123-236) with named, accessible buttons and `_disabled_button` inert-affordance
  helper (199-207) — the Draft-1 "visible but honestly inert" pattern
  (`docs/LINUX_V0_1_UI_DRAFT_1_IMPLEMENTATION_ACCEPTANCE_REPORT.md:33-38`).

**[F]** Controller/ownership law, stated verbatim in `phase9.py:1-20`: widgets are selection+presentation;
`Phase9DesktopController` is task orchestration + thread-safe publication; `BotsApplication` is the semantic
command surface; core/infrastructure own product semantics and filesystem effects. Reusable pieces:

- `Phase9DesktopController.schedule/wait_idle/close` (phase9.py:118-157) — the canonical GUI-thread task
  owner with a no-running-loop guard;
- `Phase9ProgressBridge` (phase9.py:54-89) — the only sanctioned worker-thread→GUI-thread channel
  (`progress_updated/completed/failed` signals; `publish_progress` may be called from any thread);
- dialog base `_Phase9Dialog` (phase9_dialogs.py:70-98) — typed refusal shown verbatim, never collapsed;
- view-model precedent `ImportQueueViewModel` + `QueueRowView` (phase9_imports.py:62-176) — pure
  presentation state derived from a sealed core projection, with read-only CAS tokens and derived control
  flags (`is_cancellable/is_removable/is_retryable`), no SQL, no Qt;
- bounded-poll refresh discipline (phase9_queue_dock.py:136-201: `POLL_INTERVAL_MS=250`,
  `POLL_MAX_INTERVAL_MS=1000`, poll only while visible and `has_active`; immediate reload on visibility and
  after locally issued commands) plus stale-token ⇒ "Queue changed — refresh." and never auto-retry
  (`_STALE_TOKEN_MARKERS`, phase9.py:41-51; dock.show_refresh_prompt, phase9_queue_dock.py:244-252);
- truthful typed-outcome presentation `_restore_result_presentation` (phase9_dialogs.py:1230-1278) mapping
  exact exit statuses without collapsing — the closest analogue for "truthful successful/failed/timed-out/
  partial" outcomes;
- coordinator-owned-task pattern for flows that must survive window teardown
  (`RestoreHandoffCoordinator`, phase9.py:844-985, esp. the R-1 task-ownership docstring 854-863);
- workspace-state persistence seam: `DesktopSessionController.save_window/unregister_window`
  (session.py:144-199) → `BotsApplication.save_workspace_window` (application.py:447-474) →
  `WorkspaceWindowState` (domain/models.py:138-151) → store delete+insert (sqlite.py:9251-9273). Migration
  head is `0012_phase9_archive_import` (migrations/versions/0012_phase9_archive_import.py:8-9). **[I]** If
  Phase 10 wants restart-persistent "currently loaded campaign/result", this is the existing seam — and
  VALIDATION.md:62-65 already anticipates that adding columns makes migration affected.
- theme/objectName convention (`theme.py:253 apply_draft1_theme`, selectors keyed on `objectName`, e.g.
  `theme.py:14,197-201`) — new surfaces must set stable object names; tests select by `objectName`
  throughout `tests/test_phase9_desktop_slice_e.py`.

**[F]** Headless-testability precedent: campaign engine tests run with plain `asyncio.run` and tmp dirs
(`tests/test_runner.py:17-22`), while desktop tests use `_run_qasync` + `QT_QPA_PLATFORM=offscreen`
(`tests/test_desktop_draft1.py:11,35-39`; `tests/test_phase1_desktop.py:7`). **[I]** Phase 10 can therefore
keep engine-facing tests Qt-free and put only the thin surface under pytest-qt.

**[X]** Gap: no existing seam enumerates or watches run directories, and no campaign event source feeds
`CoreEventBridge`/`EventBus` (which is desktop-only, core/events.py:89-178). Any "live progress" projection
must choose between polling filesystem evidence (consistent with F-08) and inventing a new bus — the former
matches the landed bounded-poll precedent; the latter risks becoming "a second campaign engine"
(CONTRACT.md:78-80).

---

## 9. CLI semantics — exact behavior and exit codes

**[F]** `cli.main(argv)` (cli.py:168-182): argparse subcommands required (`dest="command", required=True`,
line 23); dispatch; every `Bots5Error` is caught, printed as `error: <msg>` on stderr and returns 1
(179-181); anything else propagates. Argparse usage errors exit 2 (argparse default; documented at
`docs/EXECUTION.md:76-80`). Scripts: `bots5 = "bots5.cli:main"`,
`bots5-desktop = "bots5.bootstrap.desktop:main"` (pyproject.toml:31-34).

| Command | Code path | Behavior | Exit |
|---|---|---|---|
| `validate JOB` | `_cmd_validate` cli.py:73-77 | `load_job` (strict JSON, closed objects, dup-key/non-finite rejected — manifest.py:60-96) + `validate_referenced_files`; prints `OK: <name>` | 0; 1 on any `Bots5Error` |
| `run JOB` | `_cmd_run` cli.py:106-112 | load → validate → `_build_providers` (only declared providers, cli.py:87-103) → `asyncio.run(run_job(...))` → `_print_result` | `result.exit_code` = 0 iff `SUCCEEDED` else 1 (runner.py:516) |
| `status RUN_ID [--runs-dir]` | `_cmd_status` cli.py:115-140 | disk-only `load_run_view`; per-stage line with model/state/duration/tokens/cost/completion/output/failure; aggregate cost line | 0 iff persisted `state == "succeeded"` (line 140) |
| `inspect RUN_ID STAGE_ID [--runs-dir]` | `_cmd_inspect` cli.py:143-165 | disk-only `load_stage_view`; prints identity, completion (incl. `finish_reason` repr), full `metadata` dict, `--- output ---` body, `--- failure ---` block | 0 whenever the artifact was read (165); missing artifacts raise `ValidationError` ⇒ 1 |

**[F]** Zero-spend guarantees and their tests: `validate` constructs no provider and creates no run dir
(`tests/test_cli_views.py:14-23`, monkeypatching `bots5.cli.OpenRouterProvider` to raise); missing
credential ⇒ exit 1 and no run dir (`:25-29`, `:77-84`); local-only jobs don't need
`OPENROUTER_API_KEY` (`:59-76`); `conftest.py:9-22` blocks sockets and deletes `OPENROUTER_API_KEY` for
every ordinary test.

**[I]** Reusable for Phase 10 zero-spend validation: `manifest.load_job` +
`manifest.validate_referenced_files` are already a complete, side-effect-free validator pair with no
network and no filesystem writes — the desktop can call exactly what `_cmd_validate` calls. The two things
it cannot give you are (a) any content digest (§3.3) and (b) any cost estimate (§4.1).

**[F]** Output-shape fragility worth noting: `inspect` prints `f"metadata: {meta}"` (cli.py:157) — the raw
Python dict repr. **[I]** Adding attempt keys to the stage document changes this line's literal output;
`tests/test_cli_views.py:96-103` asserts only substrings, so it survives, but any operator tooling parsing
that line would not.

---

## 10. Synthesis representation today, gates, and dependencies

**[F]** Synthesis is **just another stage record** with a different spec type. Construction:
`SynthesisSpec` (models.py:42-51) adds `depends_on: tuple[str,...]`; parsed at manifest.py:233-264 with
closed key set `SYNTH_KEYS = WORKER_KEYS | {"depends_on"}` (line 51), non-empty unique deps (245-253), each
dep must name an existing worker (319-323), and its id may not collide with a worker id (317-318). It joins
the same `_all_stage_records` list (runner.py:220-227) and the same `_execute_stage` path (runner.py:406-415).

**[F]** Gates, evaluated strictly after the worker phase joins (`asyncio.gather(*worker_tasks)`
runner.py:330), in this order:

1. `dependency_failed` — any dep whose `state != SUCCEEDED` (runner.py:344-361): synthesis
   `SKIPPED`, `known_cost_usd = Decimal("0")`, `error_type/error_message` set, persisted, `stage_skipped` +
   `synthesis_blocked(reason, failed_dependencies)` events, run `FAILED`.
2. `dependency_incomplete` — any dep `completion_complete is not True` (runner.py:363-380): same shape with
   `incomplete_dependencies` in the event.
3. `known_cost_threshold_exceeded` — `aggregate_cost(worker_records).known_sum_usd > threshold`
   (runner.py:382-402): same shape, event carries `known_cost_usd` and `threshold_usd`.
4. Otherwise it runs, receiving `render_synthesis_user_message([(dep, outputs[dep]) for dep in
   synth.depends_on])` (runner.py:404-405; rendering.py:11-15) — **only declared dependencies' in-memory
   outputs, in `depends_on` order**, never original inputs (JOB_SPEC.md:113-121, WORKER_CONTRACTS.md:18-20).

**[F]** Whole-run success is broader than synthesis gating: `all(_stage_completed_successfully(...))` over
**every** declared worker (runner.py:336-340 without synthesis; :419-424 with).
`_stage_completed_successfully` = `state == SUCCEEDED and completion_complete is True` (runner.py:52-53).
`result.md` is written whenever synthesis returned usable text and its record is `SUCCEEDED`, **even if
incomplete** (runner.py:416-417; documented OBSERVABILITY.md:17-20, OPERATING_PROCEDURE_V1.md:115-116).

**[F]** What depends on which worker outputs, mechanically:

- synthesis input: `outputs[spec.id]` populated only for workers returning non-`None` text
  (runner.py:331-333), gated by `depends_on` (runner.py:404);
- synthesis gating: `record_by_id[dep].state/.completion_complete` (runner.py:345, :364);
- run success: all workers (runner.py:338, :419);
- cost gate: all worker records, regardless of `depends_on` (runner.py:382-384);
- durable evidence: `run.json.stage_order` (declaration order, runner.py:274) and per-id stage files.

**[I]** For stale-synthesis semantics (CONTRACT items 10-11, F-14): the dependency relation is *already*
mechanically reconstructable from `job.resolved.json`'s `synthesis.depends_on` plus `run.json.stage_order`
and the per-stage records — but only because there is exactly one record per id. Introducing sibling
attempts makes "which output did this synthesis consume" **unrepresentable**: today the answer is implied by
id equality and by the in-memory `outputs` dict (runner.py:307, 331-333, 404), neither of which is persisted
as provenance. **[F]** Confirmed absence: no document records the synthesis input digests or the specific
worker-artifact identities it consumed; `WORKER OUTPUT` blocks are ephemeral. That is the central new
evidence obligation Phase 10 inherits.

---

## 11. Tests: relevant files and what currently proves old-run compatibility

**[F]** Campaign-engine suites (all zero-provider, all passing at baseline; combined run = 115 passed):

| File | Lines | Covers |
|---|---|---|
| `tests/test_manifest.py` | 214 | v1/v2 acceptance, closed objects, dup keys, non-finite, unsupported schema/provider, unsafe stage id, runs-dir symlink, missing/non-UTF-8 referenced files, invalid contract |
| `tests/test_runner.py` | 507 | success persistence, conservative completion matrix (`stop/length/None/content_filter/refusal/future_reason`), disk reconstruction, empty-but-incomplete output, system/user separation, pre-run contract failure, dependency gating, cost threshold, unknown/partial cost, per-stage timeout, overall timeout, real worker overlap, api-key non-persistence, v2 routing (local/mixed/missing mapping/single-provider removed), resolved-job secrets |
| `tests/test_storage_events.py` | 22 | run-id uniqueness, JSONL validity |
| `tests/test_cli_views.py` | 103 | validate zero-spend/no-run-dir, missing key no-run-dir, run summary exposes incomplete, local-only CLI auth, status/inspect disk-only |
| `tests/test_provider.py` | 207 | OpenRouter normalization, finish-reason mapping, sanitized non-2xx, malformed/empty/null-content rules |
| `tests/test_local_provider.py` | 286 | local non-streaming request shape, reasoning_effort, auth env only, unknown cost stays unknown, redaction, transport errors, **normalization parity across both providers** |
| `tests/test_prompts.py` | 76 | contract grammar, exact boundary prepend, hostile source confined to user payload |
| `tests/test_rendering.py` | 15 | exact INPUT / WORKER OUTPUT block spelling |
| `tests/test_audit_blockers.py` | ~55 | internal-error terminal persistence; reread failure creates no run tree |
| `tests/conftest.py` | 22 | autouse socket block + `OPENROUTER_API_KEY` deletion (marker `local_qwen_acceptance` exempt) |
| `tests/helpers.py` | 116 | `FakeProvider`, `worker_contract`, `make_job_tree` (v1) |

**[F]** Desktop/lifecycle suites that matter as T2/T3 neighbors: `test_desktop_draft1.py` (shell,
close/reopen durability at `:203-243`), `test_phase1_desktop.py`, `test_phase4.py` (workspace/shutdown),
`test_phase7_desktop.py`, `test_phase9_desktop_slice_e.py` (31 tests incl.
`test_window_close_during_import_drains_cutoff_past_work_on_gui_thread:1030`,
`test_declined_active_generation_close_prompt_aborts_handoff_without_child_thread:2676`,
`test_failed_runtime_close_drops_restore_request_and_launches_no_child:3009`,
`test_cancelled_restore_handoff_awaiter_keeps_request_pending_thread:3266`), plus
`test_phase8_inspection*.py`, `test_authority_effect_grants.py`, `test_phase7_migration_authority_faults.py`.

**[F]** What proves old-run compatibility today: **nothing directly.** There is no test that reads a
historical run directory produced by an earlier revision. The nearest proxies are
`tests/test_runner.py:127-137` and `tests/test_cli_views.py:86-103` (round-trip within one process/run),
and the checked-in `evidence/**` trees (76 tracked files) which are human-reviewed artifacts, not test
inputs. **[I]** Consequence: Phase 10's "V0/V0.2 run directories remain inspectable" obligation (CONTRACT
§Backward compatibility, F-10) has **no existing regression harness**; the accepted design should add a
golden-run-directory test (fixture-based reader test) rather than rely on same-process round trips.

---

## 12. Producer/consumer impact inventory

### 12.1 Complete live reference graph (grep-verified)

Producers of campaign evidence: `runner.run_job` → `storage.{create_run_tree, persist_resolved_job,
persist_stage, persist_usage, persist_run, persist_result}` + `events.EventWriter.write`.
Consumers: `cli.{_cmd_status, _cmd_inspect}` via `storage.{load_run_view, load_stage_view}` and
`paths.locate_run_dir`; `runner` itself via `record_by_id`; `usage.usage_document`/`aggregate_cost` via
`StageRecord` lists; tests listed in §11; humans/docs via `evidence/**`.

**[F]** No other module in `src/` references these symbols (verified grep:
`run_job(|load_run_view|load_stage_view|persist_stage|create_run_tree|EventWriter|stage_order|output_path`).

### 12.2 If storage gains append-only attempt semantics — breakage table

| Component | Site | Why it breaks |
|---|---|---|
| Stage filename derivation | storage.py:122-125 | `stages/<id>.md/.json` collides; sibling attempt overwrites history |
| Symlink/overwrite guard | storage.py:52-54, 71 | `os.replace` is an overwrite primitive; append-only needs a create-new-name discipline |
| Aggregate stage map | storage.py:156 | dict keyed by id silently drops duplicates |
| Stage order list | storage.py:155 | encodes declaration order only; cannot express "current attempt" |
| Run view reader | storage.py:180-184 | one file per id, hard `ValidationError` on missing |
| Stage output containment | storage.py:196-198 | parent must be exactly `stages/`; nested attempt dirs rejected |
| Usage per-stage map + aggregates | usage.py:43-54, 9-33 | id-keyed; double count or clobber; unknown-cost id list ambiguous |
| In-memory record graph | runner.py:275 and all `record_by_id[...]` | uniqueness assumed |
| Synthesis dependency resolution | runner.py:344-346, 363-365, 404 | selects by id equality only; no attempt selection, no consumed-artifact provenance |
| Result leaf | runner.py:416-417 / storage.py:162-163 | single fixed `result.md`; rerun would overwrite prior synthesis output |
| Event vocabulary | events.py:13-25, 41-42 | closed set; attempt/regeneration events are a schema change |
| CLI status loop | cli.py:120-132 | one line per stage; ambiguous with siblings |
| CLI inspect addressing | cli.py:35-38, 143-165 | `(run_id, stage_id)` cannot address an attempt; `metadata:` repr changes |
| `RunResult.stages` | models.py:164-170, cli.py:55-70 | tuple-of-one-per-stage contract |
| Stage identity grammar | paths.py:10,14-18 (`{0,63}`) | limits how much discriminator can ride inside the id |
| Existing tests | §11 rows | assert exact single-record shapes/counts |

### 12.3 Why in-place overwrite is forbidden (grounded, not rhetorical)

**[F]** 1. `docs/OPERATING_PROCEDURE_V1.md:128-137` §9 requires preserving "the authoritative B.O.T.S. run
directory unchanged"; `docs/V0_2_DESIGN_CAMPAIGN_REPORT.md:477` records "Historical failed and incomplete
runs remain immutable evidence and were not rewritten after diagnosis."
**[F]** 2. Evidence integrity machinery elsewhere in the repo is overwrite-hostile by construction:
`_atomic_write` refuses symlink targets (storage.py:53-54), fsyncs file and parent dir (68-72), and
`run.json`/stage docs are treated as accounting truth after the fact
(`OPERATING_PROCEDURE_V1.md:74-75`). Overwriting a stage document destroys the only durable record of a
possibly-paid provider request whose outcome may be uncertain (`provider_side_outcome_unknown`,
models.py:110; runner.py:161, 206, 458).
**[F]** 3. Retained historical evidence is checked into the repository (`evidence/**`, 76 tracked files,
with SHA-256 pins recorded in `docs/V0_1_FINAL_CONFORMANCE_REPORT.md:100-103` and
`docs/V0_1_LIVE_CONFORMANCE_REPORT.md:231-236`). **[I]** Rewriting a live run directory would make those
documented hashes unreproducible for the newest runs and destroy the audit chain OPv1 §9 depends on.
**[F]** 4. CONTRACT.md:47 states it normatively: "Do not rewrite historical stage output in place."

### 12.4 Consumers Phase 10 must preserve (headline list)

`bots5` CLI (all four verbs), `bots5-desktop` entry point and its `--restore-from` pre-Qt short circuit,
`Provider`/`CompletionRequest`/`CompletionResult` seam reused by the desktop backend, `providers/discovery`
used by `window.py:47` and `application.py:73`, the `evidence/**` historical trees, `examples/**` job assets
(`example-job.json`, `example-job-v2-local-openai.json`, `opv1-controlled-failure/job.json`,
`v0.1-live-conformance/canary-job.json`), the docs contracts (`EXECUTION.md`, `OBSERVABILITY.md`,
`JOB_SPEC.md`, `WORKER_CONTRACTS.md`, `SECURITY.md`, `DEVELOPMENT.md:110-113` "externally visible campaign
manifest schema changes require matching validation tests and `JOB_SPEC.md` updates"), the
`AppStateStore`/migration head `0012_phase9_archive_import`, and the Phase 9 controller/dialog/dock/view-model
reuse seams in §8.

---

## 13. Conflicts and gaps the supervisor must resolve ([X] register)

1. **[X] Durable `running` on desktop-initiated cancellation.** `run_job` does not treat `CancelledError`
   as a terminal-writing path (runner.py:465 catches `Exception`, not `BaseException`; only the timeout
   branch at :428 and `_execute_stage`'s :201 handle it, and :215 re-raises). Closing a desktop window with
   an active campaign would therefore leave `run.json` saying `running`. CONTRACT.md:81-82 tolerates this
   after a *crash*, but Phase 10 makes it routine. Fork: engine-side terminal-on-cancel vs UI-side
   "must-not-close-until-terminal". Not settled by any current authority text I found.
2. **[X] Preflight pricing authority absent.** No code, no OrgMem-implied registry, no operator-entry
   surface. Historical practice was manual out-of-band capture (`evidence/v0.1-final-worker-boundary/*/preflight.json`,
   unread by any code; `docs/V0_1_FINAL_CONFORMANCE_REPORT.md:112-121` documents advertised-rate
   disagreement making a conservative bound genuinely ambiguous). F-06 stands; STOP FOR MICK territory.
3. **[X] Approval binding has no substrate.** Bytes are read at runner.py:265-267 with no digest anywhere
   (§3.3). Either the engine produces an immutable execution snapshot, or approval is definitionally
   unsound. CONTRACT.md:51-60 anticipates exactly this fork.
4. **[X] Live cost vs non-streaming.** Harness cost arrives only at terminal completion
   (runner.py:192 + `_apply_result`), while `stream()` exists but is unused by the runner
   (§5.1), and streaming redesign is excluded (CONTRACT.md:100-102). "Live cost progress" must be defined as
   known-subtotal + unknown-set projection.
5. **[X] No run-discovery seam.** `locate_run_dir` requires an operator-supplied id/base
   (paths.py:52-62); nothing enumerates runs (§2.3, §8 gap). Product question: does the desktop browse past
   campaigns, or only accept an explicit path/id?
6. **[X] `load_stage_view` containment rule** (storage.py:196-198) silently forbids the obvious nested
   attempt layout. Any attempt-path design must consciously revisit this security predicate; it is not a
   free change.
7. **[X] Event vocabulary is closed** (events.py:13-25, 41-42) and nothing reads `events.jsonl`
   programmatically (§6). Regeneration/staleness transitions imply new event kinds ⇒ a versioned evidence
   decision, and no existing reader-side compatibility shim exists to copy (§11).
8. **[X] Old-run compatibility has no automated proof** (§11). If Phase 10 extends the schema, the
   backward-compatibility obligation in CONTRACT.md:88-92 will be enforced by tests that do not yet exist.
9. **[X] Documentation status lag (F-16).** Tracked docs embedded in the Slice E candidate describe a
   pre-commit state (`docs/LINUX_V0_1_DESIGN.md:1-16`, `docs/ROADMAP.md:95-96`, `README.md:11-13`,
   `docs/ARCHITECTURE.md:7-10`), while `parcel/BASELINE.json:9-18` and OrgMem Decision 0012 record Phase 9
   closed and landed. Also `docs/DEVELOPMENT.md:8-9` still says "landed through Phase 8 … Phase 9 is
   current", and `docs/SECURITY.md:27` says "landed through Phase 6". These are stale prose, not conflicts
   about behavior; Phase 10 must not treat them as current-state authority.
10. **[X] cwd/path asymmetry** (§2.3) is a real operational trap for a GUI process; settled for the CLI by
    documentation only (`OPERATING_PROCEDURE_V1.md:121-127`).

---

## 14. Call/evidence graph (condensed, verified)

```text
bots5 (console script, pyproject.toml:32)
 └─ cli.main (cli.py:168)
     ├─ validate → manifest.load_job (:349 ← _load_json :73 ← validate_job :267 ← paths.resolve_* )
     │            → manifest.validate_referenced_files (:378 → prompts.parse_worker_contract)
     ├─ run      → cli._build_providers (:87 → OpenRouterProvider / OpenAICompatibleProvider)
     │            → asyncio.run(runner.run_job (:253))
     │               ├─ manifest.validate_referenced_files (:259)
     │               ├─ _validate_provider_mapping (:231)
     │               ├─ _read_utf8 (:29) × prompts (:265) + inputs (:267)   ← TOCTOU edge
     │               ├─ compile_worker_system_message (prompts.py:67)
     │               ├─ render_worker_user_message (rendering.py:4)
     │               ├─ storage.create_run_tree (:271) → paths.validate_run_id
     │               ├─ persist_resolved_job / persist_usage / persist_run(RUNNING)
     │               ├─ EventWriter(events.jsonl).write(run_started, stage_queued…)
     │               ├─ asyncio.Semaphore(max_parallelism) → _execute_stage (:127)
     │               │    └─ asyncio.wait_for(provider.complete(req), timeout) (:156)  ← FIRST SPEND
     │               │         ├─ ok → _apply_result (:39) → persist_stage(+stages/<id>.md) → stage_succeeded
     │               │         └─ TimeoutError/ProviderError/Exception → persist_stage(failed) → stage_failed
     │               ├─ synthesis gates: dependency_failed (:344) / dependency_incomplete (:363)
     │               │                  / known_cost_threshold_exceeded (:382, usage.aggregate_cost)
     │               ├─ render_synthesis_user_message (rendering.py:11) → _execute_stage → persist_result(result.md)
     │               ├─ run timeout sweep (:428-464)
     │               └─ persist_usage + persist_run(final) → RunResult(exit_code = 0 iff succeeded)
     ├─ status   → paths.locate_run_dir → storage.load_run_view → stdout (+exit 0 iff succeeded)
     └─ inspect  → paths.locate_run_dir → storage.load_stage_view → stdout

bots5-desktop (pyproject.toml:33)  [NO EDGE INTO THE CAMPAIGN GRAPH ABOVE]
 main → (–restore-from → _initiate_restore, Qt-free)
      → QApplication + QEventLoop → build_runtime → serve(runtime) → wait_closed
          → DesktopRuntime.close → workspace.close → application.close
              → import scheduler cancel → OwnedImportWorkers.shutdown → ExecutionManager.shutdown
              → reconcile RUNNING→ABORTED → store.close → authority.release (LAST)
          → _run_restore_handoff_post_close → subprocess child → RestoreHandoffResultDialog
```

Shared edges between the two graphs (the only ones): `bots5.providers.base` types,
`bots5.providers.openai_compatible.OpenAICompatibleProvider`, `bots5.providers.discovery`,
`bots5.core.urls.canonical_http_base_url`, `bots5.errors.ProviderError*`.

---

## 15. Method notes and limits

- All line numbers refer to bytes at HEAD `0756904481ae884bb9e864e8e1e11fc4a27a72ff`; blob pins in
  `parcel/BASELINE.json:25-43` were not individually re-hashed by me (no `git hash-object` run) — the
  tracked tree being clean (`git diff --quiet`, exit 0) plus HEAD match makes the working bytes equivalent
  to the pinned ones. **[I]**
- OrgMem sources listed in `parcel/SOURCES.md:41-53` were **not** inspected: no OrgMem checkout is present
  in this workspace, and my scope is the B.O.T.S. repository. Authority questions flagged as [X] are stated
  as gaps in *implementation*, not as adjudications against OrgMem text.
- No provider/API/network call was made; no worker spawned; no dependency installed; the only write is this
  report. Test runs used the repository venv (Python 3.14.7) with the autouse socket-blocking fixture.
- `docs/LINUX_V0_1_UI_UX_DRAFT_1.md` contains **no** campaign/New Job/regeneration language at all
  (`grep -n "campaign|New Job|regenerat|stale"` → no matches). **[F]** The Phase 10 product wording comes
  from `docs/LINUX_V0_1_DESIGN.md:48` ("a thin operational desktop surface over the existing campaign
  engine") and `docs/ROADMAP.md:110` / `docs/LINUX_V0_1_DESIGN.md:413`, not from the UI draft — which is
  itself explicitly provisional (`LINUX_V0_1_UI_UX_DRAFT_1.md:3`, `docs/ROADMAP.md:126-129`). **[X]** The
  campaign desktop has no visual-direction authority yet; specialists should not assume the Draft-1 mockups
  cover it.
