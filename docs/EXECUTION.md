# Execution

`bots5 validate JOB.json` parses the closed schema, validates every referenced UTF-8 text file, and
validates every worker and synthesis contract. It makes no API request and creates no run directory.

`bots5 run JOB.json` repeats validation. The CLI then requires `OPENROUTER_API_KEY`; a missing key
fails before run-directory creation.

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
11. Otherwise run synthesis with its own compiled system message and WORKER OUTPUT data blocks.
12. Persist final usage and run state. `result.md` is written when synthesis returns usable output
    and its stage is persisted as succeeded, including an incomplete synthesis; run success still
    requires every worker and synthesis stage to have completed normally.

A run without synthesis succeeds only if every worker is succeeded and completed normally. With
synthesis, the run succeeds only if every declared worker and synthesis is succeeded and completed
normally. `synthesis.depends_on` controls synthesis input and dependency gating; it does not remove
other declared workers from the final whole-run success condition.

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

## Timeouts and cancellation

Each request is wrapped in `asyncio.wait_for(timeout_seconds)`. A request timeout fails that stage and
marks provider-side outcome as uncertain.

The whole pipeline is also wrapped by `run_timeout_seconds`. On expiry, in-flight work is cancelled
where Python/httpx cancellation permits. Already persisted siblings remain. Unfinished workers are
recorded failed with `run_timed_out`; synthesis not yet reached is skipped. The run state is
`timed_out`. B.O.T.S. 5 does not claim that a provider necessarily stopped processing a request that
was already sent.

## Exit behavior

- `0`: command/run succeeded.
- `1`: validation, execution, lookup, or persisted-state failure.
- `2`: argparse usage error.
