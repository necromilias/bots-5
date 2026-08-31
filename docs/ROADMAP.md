# Roadmap

## Closed V0 baseline

B.O.T.S. 5 V0 is closed. Final zero-spend closure validation passed against
`13e3ac463c44d66e57d4443027f0cc9dfe9b93a5`; see `docs/V0_CLOSURE_REPORT.md`.

The closed baseline includes:

- strict JSON manifest;
- explicit UTF-8 inputs/prompts;
- bounded parallel worker phase;
- optional synthesis;
- OpenRouter provider boundary;
- per-stage and overall timeout handling;
- durable run/stage/event/usage artifacts;
- disk-only status and inspect;
- exact-known/unknown cost semantics;
- zero-spend unit tests.

## V0.1 status

- worker-boundary hardening is implemented;
- completion telemetry is implemented;
- final live worker-boundary conformance is satisfied;
- the first genuinely useful low-stakes repository-audit campaign is recorded in
  `docs/FIRST_USEFUL_CAMPAIGN_REPORT.md`;
- the revised useful campaign completed normally and was judged substantively useful;
- its accepted findings were adjudicated and the blocking CLI, terminal-persistence, and runs-dir
  symlink defects were corrected and locally validated;
- the controlled failure-path campaign is preserved under `examples/opv1-controlled-failure/` and
  exercised terminal provider-response failure, dependency blocking, skipped-stage zero-cost
  accounting, partial aggregate cost, and durable inspection without exposing a blocking defect;
- the exact `dependency_incomplete` semantics are additionally covered by deterministic tests and
  prior real truncation evidence;
- `docs/OPERATING_PROCEDURE_V1.md` is the frozen normal operating procedure;
- the OPv1 freeze decision and evidence are recorded in `docs/OPV1_FREEZE_REPORT.md`.

## Post-V0 work

Likely next steps:

- local OpenAI-compatible provider;
- planner-generated manifests;
- richer but still explicit DAG semantics;
- more deliberate retry policy where idempotence is understood;
- stronger filesystem/tool capability model.

## Deferred defect

- extremely large JSON integers can escape `_number()` through `float()` overflow instead of a clean
  validation error; low operational consequence and explicitly carried forward beyond V0 closure.

## Speculative

- OMC authority packaging;
- gated patch application;
- live terminal/graphical status UI;
- distributed execution.

None of the speculative items exist in V0.
