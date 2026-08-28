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
