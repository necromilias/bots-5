# Roadmap

## Implemented in this V0 candidate

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
- that campaign is classified `FAIL - HIGH SUBSTANTIVE VALUE`: all root workers completed normally,
  but required synthesis terminated `length`/incomplete at its 5,000-token ceiling;
- its findings have now been human-adjudicated: accepted pre-OPv1 work is documentation/procedure
  clarification, with the extreme-number validator edge case deferred;
- `docs/OPERATING_PROCEDURE_V1_CANDIDATE.md` is the candidate normal operating baseline, not yet
  frozen OPv1;
- the next execution step is to tighten the external first-useful campaign synthesis contract and run
  one revised useful campaign once;
- after a useful normal-completion run, execute the planned controlled failure-path campaign before
  deciding whether to freeze Operating Procedure v1.

## Likely next steps

Only after V0 is reviewed:

- local OpenAI-compatible provider;
- planner-generated manifests;
- richer but still explicit DAG semantics;
- more deliberate retry policy where idempotence is understood;
- stronger filesystem/tool capability model.

## Deferred defect

- extremely large JSON integers can escape `_number()` through `float()` overflow instead of a clean
  validation error; low operational consequence, deferred until after the OPv1 campaign sequence.

## Speculative

- OMC authority packaging;
- gated patch application;
- live terminal/graphical status UI;
- distributed execution.

None of the speculative items exist in V0.
