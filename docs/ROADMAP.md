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
- the first genuinely useful low-stakes repository-audit campaign has been executed and is recorded
  in `docs/FIRST_USEFUL_CAMPAIGN_REPORT.md`;
- that campaign is classified `FAIL - HIGH SUBSTANTIVE VALUE`: all root workers completed normally,
  but required synthesis terminated `length`/incomplete at its 5,000-token ceiling;
- the next step is to adjudicate the campaign observations, tighten the synthesis contract, and run
  one revised useful campaign before deciding whether to freeze Operating Procedure v1.

## Likely next steps

Only after V0 is reviewed:

- local OpenAI-compatible provider;
- planner-generated manifests;
- richer but still explicit DAG semantics;
- more deliberate retry policy where idempotence is understood;
- stronger filesystem/tool capability model.

## Speculative

- OMC authority packaging;
- gated patch application;
- live terminal/graphical status UI;
- distributed execution.

None of the speculative items exist in V0.
