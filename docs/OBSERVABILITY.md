# Observability

Each run is self-contained. There are two evidence layouts: **evidence v1** (legacy headless runs)
and **evidence v2** (Phase 10 approved runs). `run.json` declares which: `evidence_version: 2` means
v2, and an absent marker means v1. Version-1 evidence remains readable and is never rewritten.

## Evidence-v1 run layout

```text
<RUNS_DIR>/<run-id>/
  run.json
  job.resolved.json
  events.jsonl
  usage.json
  stages/
    <stage-id>.json
    <stage-id>.md
  result.md
```

`result.md` is present when synthesis returns usable output and its stage is persisted as succeeded.
It may therefore exist for an incomplete synthesis whose overall run is failed. File presence alone
is not evidence of normal completion or human acceptance; check `run.json` and synthesis completion
metadata.

For schema-v2 runs, `job.resolved.json` contains the validated non-secret provider configuration,
including the local endpoint and configured environment-variable name when present. It never contains
the resolved environment-variable value or an authorization header.

## Evidence-v2 run layout

```text
<RUNS_DIR>/<run-id>/
  run.json                  # evidence_version: 2, plus stage_order
  job.resolved.json
  events.jsonl
  usage.json
  preflight.json            # written by the engine after approval, when the v2 run begins
  approvals/
    <approval-id>.json      # one-shot approval consumption marker
  selection.json            # authoritative stage -> attempt map (may be absent; see below)
  stages/
    <stage-id>.att<N>.json
    <stage-id>.att<N>.md
  result.md
```

Not every file always exists. Precisely:

- `preflight.json` exists only for a run that began through the approved path; preparation itself
  writes nothing.
- `approvals/<approval-id>.json` exists only after that approval has been consumed; it is the durable
  one-shot marker.
- `selection.json` **may be absent**. Absence means implicit attempt 1 for every stage while no
  sibling ambiguity exists. Before a sibling attempt is claimed, the engine materializes that
  implicit default selection, after which `selection.json` is the authoritative explicit
  stage→attempt mapping. A v2 run directory that holds `.att2+` files without an authoritative
  `selection.json` is malformed and **fails closed** rather than guessing a selection from filename
  shapes.
- `stages/<stage-id>.att<N>.md` is present only when that attempt produced output text.
- `result.md` follows the same rule as v1: it is present when synthesis returns usable output and its
  stage is persisted as succeeded, and may exist for an incomplete synthesis whose overall run is
  failed.

## Attempt evidence

- Attempt numbers are positive per-stage identities. They are never renumbered and never reused: the
  next attempt is `max(existing) + 1`, and a stage with no attempts yet derives 1.
- A new attempt claims its namespace with exclusive-create semantics; a second writer is refused and
  writes nothing.
- Terminal and metadata updates mutate only the same claimed attempt record. Sibling regeneration
  never rewrites attempt 1.
- On top of the v1 stage fields below, attempt metadata adds `attempt_number` (always emitted) and,
  only when applicable, `consumed_dependencies` and `dependency_digests` (a synthesis attempt bound to
  its selected dependency attempts and their output digests) and `preflight_digest` (a v2 attempt
  bound to the approved preflight).

## Stage JSON

Contains stage/provider/model identity, state, timestamps, duration, token usage when known, exact
cost when known, request ID, relative output path, completion metadata, and sanitized failure
metadata. Completion preserves the provider finish reason; only exact `stop` is complete, while
missing, malformed, and all other reasons are conservatively incomplete.
`cost_usd: null` plus `cost_known: false` means unknown. Known costs are stored as decimal strings
to avoid binary floating-point money arithmetic.

A known cost may come from exact provider-reported usage or from a harness-known zero when a stage is
skipped before any provider request is sent. A skipped stage recorded as zero is therefore not a
claim that the provider reported a zero-dollar request; it records that no request for that stage was
made and no provider cost was incurred by it.

## Events

`events.jsonl` is append-only, one compact JSON object per line, fsynced per append. Event writes are
serialized with a process-local lock. Events contain small metadata only, never complete prompts,
outputs, request headers, or API keys.

Legacy vocabulary:

`run_started`, `stage_queued`, `stage_started`, `request_sent`, `stage_succeeded`, `stage_failed`,
`stage_skipped`, `synthesis_blocked`, `run_timed_out`, `run_succeeded`, `run_failed`.

Phase 10 appends, without removing or renaming any existing kind:

`attempt_selected`, `synthesis_stale`, `run_cancelled`, `worker_regeneration_started`,
`worker_regeneration_finished`, `synthesis_rerun_started`, `synthesis_rerun_finished`.

The writer stays fail-closed: an unknown event kind raises rather than being written.

## Usage and cost

### Evidence v1

`usage.json` contains per-stage usage and aggregate known token sums. Cost state is:

- `zero`: every stage cost is known and zero.
- `known`: every stage cost is known and the sum is nonzero (or no stages exist).
- `partial`: at least one known and at least one unknown cost.
- `unknown`: all relevant stage costs are unknown.

Unknown provider cost, including the normal local-provider case when the response supplies no valid
cost telemetry, is never silently converted to zero. Harness-known skipped-stage zero is a
separate case: no provider request was sent for that stage.

### Evidence v2 authority hierarchy

An evidence-v2 `usage.json` carries `evidence_version: 2` and the following keys, in this authority
order:

- `cumulative_spend` — every executed attempt on disk; the financial history of the run.
- `per_attempt` — per-attempt usage and cost, keyed `<stage-id>.att<N>`.
- `selected_spend` — the selected-pipeline summary; the figure that answers "what did the current
  selection cost".
- `stages` — the legacy-compatible selected-stage view.
- `aggregate` — mirrors selected-spend semantics for compatibility.

`selected_spend` (and the `aggregate` mirror) is **re-derived at read time** from `per_attempt` plus
the current `selection.json`. A disagreement with a stored selected cache never overrides the
derivation. Unknown cost stays unknown: cost is never converted into a projected accrual, and
`cumulative_spend` is financial history, not a budget or a forecast.

Each spend summary uses the same status rules as v1 (`zero` / `known` / `partial` / `unknown`, with
the known sum, completeness flag and unknown stage list).

## Synthesis freshness

Synthesis freshness is reconstructed mechanically from the selected dependency attempt identities and
the bound output digests where the evidence permits. It is not a stored claim that is trusted as-is.
The classifications are exactly:

- `FRESH` — the selected synthesis attempt's bound dependency attempts and digests match the current
  selected dependency attempts and their output bytes.
- `STALE` — they no longer match: a selected dependency attempt changed after synthesis ran.
- `UNVERIFIABLE` — the evidence is v2 but does not carry enough bound provenance to decide.
- `NOT_APPLICABLE` — synthesis was never dispatched (persisted `skipped`, or a v2 attempt that
  explicitly records `started_at: null`). This is decided before any provenance evaluation and raises
  no integrity warning.
- `LEGACY_UNVERIFIED` — the run is evidence v1, which carries no v2 provenance to check.

Legacy v1 synthesis is `LEGACY_UNVERIFIED`; it is not described as having v2 provenance it does not
carry.

## Durable truth

- Filesystem evidence remains authoritative. The run directory is the record.
- The desktop projection is read from durable run evidence only; the `CampaignBridge` is Qt-free and
  builds views from the run directory.
- UI state is not a second campaign truth store. There is no campaign event bus, no shadow database,
  and no separate in-memory truth.
- An interrupted durable `running` state without a live hosted task is reported as interrupted and
  uncertain — it is not silently called success, failure, or resumable.
- Provider-side outcome uncertainty remains explicit. A durable post-dispatch failure is unknown
  unless the record proves a definitive rejection; a live cost figure is the known subtotal plus the
  explicit unknown set, never a fabricated accrual.

## Status and inspect

`bots5 status RUN_ID` and `bots5 inspect RUN_ID STAGE_ID` use
`./.bots5/runs` relative to the current working directory by default. For manifests configured
elsewhere, pass `--runs-dir PATH`. A relative `--runs-dir` value is interpreted from the current
working directory, unlike manifest `output.runs_dir`, which is resolved relative to the job file.
Use an absolute `--runs-dir` when changing directories between execution and inspection.

These commands read disk only and make no provider calls. Both views display the persisted
completion state and finish reason. `bots5 status` exits zero only when the persisted run state is
`succeeded`; persisted `failed` or `timed_out` runs produce a nonzero status exit. `bots5 inspect`
continues to report whether the requested persisted stage artifact was read successfully.

For evidence-v2 runs, `bots5 status` additionally exposes each stage's selected attempt and the
available attempt numbers, plus the synthesis freshness/integrity markers described above.
`bots5 inspect RUN_ID STAGE_ID --attempt N` reads exactly that v2 attempt; when an explicit attempt
is requested there is no fallback to a "latest" attempt, and a missing or malformed attempt is an
error. Without `--attempt`, inspection reads the selected attempt (attempt 1 when nothing is
explicitly selected).

The immediate `bots5 run` summary also displays each stage's completion state and exact finish reason,
so a provider response persisted as stage state `succeeded` but completion `incomplete` is visible
without requiring a separate status command.
