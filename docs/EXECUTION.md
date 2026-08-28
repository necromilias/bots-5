# Execution

`bots5 validate JOB.json` parses the closed schema and validates every referenced text file. It makes
no API request and creates no run directory.

`bots5 run JOB.json` repeats validation. The CLI then requires `OPENROUTER_API_KEY`; a missing key
fails before run-directory creation.

After validation and key acquisition:

1. Create a unique run directory and `stages/`.
2. Persist `job.resolved.json`, queued stage records, initial usage/run state, and events.
3. Read declared inputs and render one deterministic worker user message.
4. Launch all worker coroutines. A semaphore limits in-flight worker requests.
5. Persist each success or failure independently.
6. After the worker phase joins, evaluate synthesis dependencies.
7. If a dependency failed, persist synthesis as skipped and fail the run.
8. Otherwise compare the exact known worker-cost subtotal with the configured synthesis gate. If the
   known subtotal is greater than the threshold, skip synthesis and fail the run. Unknown cost is not
   treated as zero.
9. Otherwise run synthesis with its own request timeout.
10. Persist final usage and run state. `result.md` exists only when synthesis succeeds.

A run without synthesis succeeds only if every worker succeeded. With synthesis, the run succeeds
only if every worker and synthesis succeeded.

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
