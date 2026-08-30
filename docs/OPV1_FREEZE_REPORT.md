# Operating Procedure v1 Freeze Report

## Decision

Operating Procedure v1 is frozen.

The candidate freeze gate required both a revised genuinely useful campaign that completed normally
and a controlled failure-path campaign that exercised important failure semantics without exposing a
blocking harness or procedure defect. Both conditions are satisfied.

## Useful-campaign gate

The revised repository-audit campaign completed normally and produced substantively useful,
traceable findings. Its findings were human-adjudicated rather than accepted automatically.

Accepted implementation findings were corrected before freeze:

- CLI run output now exposes completion and finish reason, and `bots5 status` returns nonzero for a
  persisted failed or timed-out run;
- unexpected execution, orchestration, and persistence failures make a best-effort transition to a
  terminal failed run rather than leaving durable state stuck at `running`;
- configured `runs_dir` symlinks are rejected before path resolution;
- the generic example was clarified as illustrative rather than a model/ceiling recommendation.

The correction branch was locally validated with 65 passing tests, successful `compileall`, and a
clean `git diff --check` before merge to `main` at
`de70e4460506623ee119efd5c5d76a57a99ff6bb`.

## Controlled failure-path gate

Campaign assets are preserved under `examples/opv1-controlled-failure/`.

Authoritative run ID:
`bots5-opv1-controlled-failure-20260830T001907Z-076fc9d3`.

The campaign attempted to force a normal provider `length` completion with a one-token worker ceiling.
OpenRouter instead returned a response that B.O.T.S. could not reconstruct as a valid completion, so
the worker persisted `ProviderResponseError: malformed_provider_response`.

Observed harness behavior was controlled and correct:

- the worker persisted terminal `failed` rather than remaining `running`;
- synthesis was skipped with `dependency_failed` and no synthesis provider request was made;
- synthesis cost persisted as harness-known zero;
- worker cost remained unknown rather than being coerced to zero;
- aggregate cost persisted as `partial` and incomplete;
- the overall run persisted terminal `failed` with `ended_at`;
- `status` and `inspect` exposed the durable failure state from disk.

The unexpected provider response was adjudicated instead of automatically rerunning the paid
campaign. It exposed no blocking harness or procedure defect. The specific `dependency_incomplete`
path originally targeted by the campaign is already covered by deterministic tests and prior real
truncation evidence.

## Freeze outcome

No further paid provider run is required for OPv1 freeze. The candidate document is retired and
`docs/OPERATING_PROCEDURE_V1.md` is the normal operating baseline.
