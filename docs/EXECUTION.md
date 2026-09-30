# Execution

This repository has two campaign execution surfaces, and they must not be confused:

1. the **legacy headless path** — `bots5 validate` and `bots5 run`, which produce **evidence-v1**
   runs and remain byte-for-byte the pre-Phase-10 behaviour;
2. the **Phase 10 approved path** — the desktop campaign surface and the explicit worker-regeneration
   and synthesis-rerun operations, which produce and operate on **evidence-v2** runs.

The approved path is additive. It does not change the legacy path, and ordinary `bots5 run` is never
retroactively described as approval-bound or evidence-v2.

## Legacy headless path (`bots5 validate` / `bots5 run`) — evidence v1

`bots5 validate JOB.json` parses the closed schema, validates every referenced UTF-8 text file, and
validates every worker and synthesis contract. It makes no API request and creates no run directory.

`bots5 run JOB.json` repeats validation. It constructs only the providers declared by the stages in
the job. OpenRouter stages require `OPENROUTER_API_KEY`; local stages use their validated
`providers.local_openai` configuration and require only its optional configured `api_key_env`, if
one is present. Missing credentials fail before run-directory creation.

`bots5 run` invokes the runner with **no snapshot and no approval**, so it always produces an
**evidence-v1** run: no `evidence_version` marker in `run.json`, no `preflight.json`, no
`selection.json`, and single legacy stage files `stages/<stage-id>.json`/`.md` rather than
attempt-addressed files. It is a legacy headless execution path and is neither approval-bound nor
evidence-v2.

After validation and key acquisition:

1. Compile each system message from the harness-owned execution boundary and validated contract.
2. Read declared inputs and render one deterministic worker user message containing INPUT data.
3. Create a unique run directory and `stages/`.
4. Persist `job.resolved.json`, queued stage records, initial usage/run state, and events.
5. Launch all worker coroutines. A semaphore limits in-flight worker requests.
6. Persist each success or failure independently.
7. After the worker phase joins, evaluate synthesis dependencies.
8. If a dependency failed, persist synthesis as skipped with `dependency_failed` and fail the run.
9. If a dependency succeeded without a normal completion, persist synthesis as skipped with
   `dependency_incomplete` and fail the run.
10. Otherwise compare the exact known worker-cost subtotal with the configured synthesis gate. If the
    known subtotal is greater than the threshold, skip synthesis and fail the run. Unknown or partial
    worker cost does not itself block synthesis; the gate evaluates only the known subtotal and must
    not be treated as a hard budget or a fail-closed unknown-cost control.
11. Otherwise run synthesis with its own compiled system message and WORKER OUTPUT data blocks,
    through the synthesis stage's declared provider.
12. Persist final usage and run state. `result.md` is written when synthesis returns usable output
    and its stage is persisted as succeeded, including an incomplete synthesis; run success still
    requires every worker and synthesis stage to have completed normally.

A run without synthesis succeeds only if every worker is succeeded and completed normally. With
synthesis, the run succeeds only if every declared worker and synthesis is succeeded and completed
normally. `synthesis.depends_on` controls synthesis input and dependency gating; it does not remove
other declared workers from the final whole-run success condition.

## Programmatic runner API — dual mode

The programmatic runner API is `run_job(job, providers, ...)`, where `providers` is a mapping from
provider ID to a `Provider`. There is no generic single-provider compatibility form. A missing mapping
entry is rejected before a run directory is created.

`run_job` has two modes:

- **Legacy v1 mode** — `run_job(job, providers, ...)` with neither `snapshot` nor `approval`. This is
  the exact pre-Phase-10 behaviour: system messages are compiled and referenced inputs are re-read at
  dispatch time, and the run is written as evidence v1.
- **Evidence-v2 mode** — `snapshot` and `approval` must be supplied **together**. Supplying exactly
  one of them is a hard error (`run_job requires snapshot and approval together (or neither)`), so
  there is no half-bound state. In v2 mode the engine verifies the approval/snapshot binding
  *before* the run directory is created and before any provider request; an omitted `run_id` is
  resolved from the approval target; the one-shot approval is consumed durably; `preflight.json` is
  written; and dispatch uses only the frozen approved messages rather than silently re-rendering
  changed input.

There is **no automatic conversion** of an existing evidence-v1 run into evidence v2. A v1 run stays
v1 and remains readable; it is never rewritten in place.

## Phase 10 approved execution path (evidence v2)

### Desktop full-run flow

The desktop never writes campaign evidence itself. It observes and commands the engine through the
Qt-free `CampaignBridge` (`core/campaign.py`), in three zero-spend preparation steps followed by an
explicit approval:

- **load job** — parses and validates the job file. No provider is constructed, nothing is written,
  and nothing is spent.
- **validate** — validates every referenced input and contract. No provider is constructed, no run
  directory is created, and nothing is spent.
- **prepare full run** — builds a frozen `PreflightSnapshot`: the generated run id bound to the
  approval, the exact referenced input/contract bytes, the declared provider routes, execution limits,
  the compiled system messages, and the preflight digest. It also computes the approval's pricing
  evidence from any operator-supplied rates. This step is still zero-spend: no provider is
  constructed and nothing is written, so an abandoned approval leaves zero run-directory bytes.
  Preparing does **not** write `preflight.json`. A paid route requires complete operator pricing
  evidence; that requirement is enforced at approval, not by preparation.
- **approve/start** — the provider is constructed only after approval, through an injectable factory
  that defaults to the real route rules. The engine re-verifies the approval/snapshot binding
  (digest identity, scope and target, configuration consistency, on-disk bytes of every referenced
  input and contract, system-message/contract correspondence, provider-object route identity) and the
  pricing evidence before any provider request. The one-shot approval is then consumed durably, the
  evidence-v2 run tree and `preflight.json` are created, and dispatch uses the frozen approved
  messages.

`preflight.json` is therefore written by the **engine, after approval**, when the v2 run begins — not
by preparation.

### Worker regeneration

```text
bots5 regenerate RUN_ID STAGE_ID --model MODEL --job JOB [--runs-dir DIR]
                   [--approve --actor LABEL | --approval APPROVAL.json] [--pricing PRICING.json]
```

Semantics:

- operates on **evidence-v2 runs only**; an evidence-v1 target is refused before anything is written;
- appends an explicit **sibling attempt** (`stages/<stage-id>.att<N>.json`/`.md`) and never overwrites
  an existing attempt; the original attempt is never modified;
- the provider route stays bound; only the requested `--model` may differ, as the implementation
  permits;
- the attempt number is derived from disk and is binding — an approval whose bound attempt number
  disagrees with the disk-derived next number is refused rather than renumbered;
- regeneration **never auto-selects** the new attempt; the selection is unchanged by the execution;
- it is never an automatic retry or resume;
- a version-1 target refuses before writing.

CLI consent:

- without `--approve` or `--approval` the verb is a **zero-spend preflight only** — nothing is spent;
- `--approve` requires a non-empty `--actor` operator label, which is recorded;
- `--approval` loads a pre-built `ApprovalRecord` JSON instead;
- paid routes require valid operator pricing evidence as implemented;
- the provider is constructed only after consent.

### Synthesis rerun

```text
bots5 rerun-synthesis RUN_ID --job JOB [--runs-dir DIR]
                       [--approve --actor LABEL | --approval APPROVAL.json] [--pricing PRICING.json]
```

Semantics:

- operates on **evidence-v2 runs only**;
- appends a new synthesis attempt; earlier synthesis attempts are never modified;
- binds the exact selected dependency attempt numbers and the SHA-256 of their output bytes approved
  at preflight time, and refuses before dispatch if they no longer match;
- the pre-synthesis cost gate evaluates the **derived selected worker cost**, never cumulative
  historical spend and never a trusted stale cache;
- on normal completion the new synthesis attempt becomes the selected one and `result.md` mirrors its
  output;
- on failure the previous selection is unchanged;
- it is never triggered automatically by regeneration, staleness detection, timeouts or shutdown.

### Status and inspection

- `bots5 status RUN_ID` understands both evidence versions. For a v2 run it exposes each stage's
  selected attempt and available attempts, plus the synthesis freshness/integrity markers.
- `bots5 inspect RUN_ID STAGE_ID --attempt N` reads exactly that v2 attempt. When an explicit attempt
  is requested there is no fallback to a "latest" attempt: a missing or malformed attempt is an error,
  not a silent substitution.
- Without `--attempt`, `bots5 inspect` reads the selected attempt (attempt 1 when nothing is
  explicitly selected).

## Operator preflight and review

The frozen normal operating procedure is in `OPERATING_PROCEDURE_V1.md`. Operators should at minimum:

- validate before a paid run;
- review output-token ceilings against the required contract output rather than treating them as
  harmless targets;
- calculate a conservative paid-run estimate from the highest applicable currently advertised
  pricing surface available at preflight time;
- treat provider-reported persisted cost as final accounting truth after the run;
- require exact `finish_reason == "stop"` and complete status for every stage counted as normally
  completed evidence;
- preserve the authoritative run directory and record a separate human usefulness decision.

## Chat generation timeouts and cancellation

Linux v0.1 Phase 3 exposes an explicit local cancellation command and desktop Stop action for an
active generation. The owned generation task is cancelled and the HTTP stream is closed where the
transport permits. Any already persisted partial assistant text remains authoritative; the assistant
message and generation attempt become `aborted`. Because dispatch may already have happened,
`remote_outcome_unknown` is persisted as true for an explicit cancellation and for restart/crash
reconciliation only when dispatch may have occurred; a durable pre-dispatch false remains false.
Late backend deltas are ignored after cancellation, and aborted or uncertain work is never retried
automatically.

Each request is wrapped in `asyncio.wait_for(timeout_seconds)`. A request timeout fails that stage and
marks provider-side outcome as uncertain.

The whole pipeline is also wrapped by `run_timeout_seconds`. On expiry, in-flight work is cancelled
where Python/httpx cancellation permits. Already persisted siblings remain. Unfinished workers are
recorded failed with `run_timed_out`; synthesis not yet reached is skipped. The run state is
`timed_out`. B.O.T.S. 5 does not claim that a provider necessarily stopped processing a request that
was already sent.

## Campaign cancellation (Phase 10)

Campaign cancellation is a separate concern from chat-generation cancellation above.

- Cancellation terminalizes campaign evidence truthfully. Cancelling a full run persists the
  affected attempts as `failed` with `error_type` `cancelled`, records `stage_failed` per attempt,
  emits `run_cancelled`, and persists run state `cancelled`. Cancelling a regeneration or
  synthesis-rerun operation terminalizes that attempt the same way and emits the operation's
  `worker_regeneration_finished` / `synthesis_rerun_finished` event.
- An already-dispatched provider-side outcome may remain **unknown**; the durable record sets
  provider-side uncertainty when the attempt had started, rather than claiming a definitive result.
- There is no automatic retry: cancelled or uncertain campaign work is never re-dispatched
  automatically.
- The desktop runtime performs a bounded campaign drain during shutdown, so a hosted campaign
  operation is given a bounded opportunity to terminalize rather than being silently abandoned.

## Exit behavior

- `0`: command/run succeeded.
- `1`: validation, execution, lookup, or persisted-state failure.
- `2`: argparse usage error.
