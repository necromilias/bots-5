# V0 Closure Report

## Decision

B.O.T.S. 5 V0 is closed.

The final closure validation was performed against exact `origin/main` at
`13e3ac463c44d66e57d4443027f0cc9dfe9b93a5` after fast-forwarding the local checkout to the remote
branch tip. No code or test changes were made as part of this closure decision.

## Final zero-spend validation

Environment:

- branch: `main`;
- `HEAD`: `13e3ac463c44d66e57d4443027f0cc9dfe9b93a5`;
- `origin/main`: identical;
- `OPENROUTER_API_KEY`: unset;
- provider/API calls: none.

Commands:

```bash
git fetch origin main
git merge --ff-only origin/main
env -u OPENROUTER_API_KEY PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider
env -u OPENROUTER_API_KEY PYTHONPYCACHEPREFIX=/tmp/bots5-v0-final-compileall-20260831 .venv/bin/python -m compileall -q src tests
env -u OPENROUTER_API_KEY .venv/bin/bots5 validate examples/example-job.json
env -u OPENROUTER_API_KEY .venv/bin/bots5 validate examples/opv1-controlled-failure/job.json
env -u OPENROUTER_API_KEY .venv/bin/bots5 validate examples/v0.1-live-conformance/canary-job.json
git diff --check
```

Results:

- pytest: **65 passed**;
- compileall: passed;
- manifest validation: all three checked-in example manifests passed;
- `git diff --check`: passed;
- tracked/staged working-tree changes: none;
- pre-existing untracked `examples/first-live-smoke-job.json`: preserved unchanged;
- blockers: none.

## Closure scope

The closure decision includes the implemented V0/V0.1 runtime behavior, deterministic worker-boundary
hardening, completion telemetry, durable failure semantics, frozen OPv1 operating procedure, useful
campaign evidence, controlled failure-path evidence, and the final documentation corrections present
at the validated baseline.

Historical campaign and validation reports remain historical records and are not rewritten by this
closure update.

## Deferred item

The known extreme-integer validation edge case remains explicitly deferred: extremely large JSON
integers can reach `_number()` and cause `float()` overflow instead of a clean validation error. It is
low operational consequence and is not a V0 closure blocker.

## Baseline and closure-document commit

`13e3ac463c44d66e57d4443027f0cc9dfe9b93a5` is the exact executable/test/documentation baseline that
received the final zero-spend closure validation. The commit adding this report and updating live
closure-facing documentation is a post-validation documentation-only commit; it does not modify
runtime or test files.
