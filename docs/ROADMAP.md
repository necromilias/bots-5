# Roadmap

## Implemented V0

- strict JSON manifests;
- OpenRouter provider boundary;
- bounded parallel independent workers;
- optional synthesis;
- durable per-stage outputs and run telemetry;
- known/unknown cost semantics;
- disk-only status/inspect;
- human-controlled real execution.

## Likely next steps

Only after V0 is reviewed against observed use:

- local OpenAI-compatible provider;
- planner-generated job manifests;
- richer but still explicit DAG dependencies;
- stronger per-stage filesystem/tool permissions where tools become necessary;
- selective, documented retry policy if observed failures justify it.

## Speculative

- gated patch application;
- live terminal or graphical status UI;
- OMC integration for authority/context packaging;
- distributed execution.

Nothing in the latter sections is implemented merely because it is listed here.
