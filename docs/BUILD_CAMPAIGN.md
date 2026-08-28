# V0 Build Campaign Retrospective

## Purpose

This document records how the first B.O.T.S. 5 V0 candidate was produced, what the temporary
multi-model build method did well, what failed, and which observations should inform later B.O.T.S. 5
execution design.

It is a campaign retrospective, not runtime authority. Runtime behavior is defined by the source,
tests, job specification, execution documentation, and accepted ADRs.

## Campaign shape

The initial build experiment used Langflow as a temporary visual fan-out/fan-in orchestrator over
OpenRouter. A common V0 Build Brief was supplied to bounded specialist workers. Their reports were
intended to feed one final integration model that would emit a self-contained `bootstrap_bots5.py`
transport artifact.

The specialist roles evolved into:

- Luna: requirements and implementation-contract review;
- DeepSeek runtime/core: module boundaries, manifest handling, provider interface, scheduling,
  persistence, CLI, and failure semantics;
- DeepSeek testing/reliability: fake-provider and failure-propagation design;
- DeepSeek security/runtime-safety: secrets, filesystem, persistence, cost, timeout, and
  cancellation semantics;
- Gemini: adversarial pre-implementation review;
- final integrator: reconcile the reports and generate the complete candidate.

Workers were deliberately bounded. They were not given shell access, repository mutation authority,
or permission to create more workers.

## What worked

### Fan-out produced useful specialist material

The bounded worker reports were generally useful when each role had a narrow cognitive surface.
The runtime/core report produced concrete module and state-machine guidance. The security report
identified strict-JSON, secret-redaction, durability, cost-completeness, and cancellation traps.
The adversarial review found several plausible "looks finished but is wrong" failure modes.

### Parallel work was the right shape

Independent analysis tasks could run concurrently. The useful work did not require a model-led
committee or recursive delegation. The campaign reinforced the core B.O.T.S. 5 idea that topology,
permissions, and stopping conditions belong to the harness rather than to the models.

### Persisted intermediate reports were valuable

Once worker outputs were explicitly checkpointed to files, completed analysis survived later
integration failures. This is a direct design lesson for B.O.T.S. 5: completed stages should be
persisted independently and immediately rather than existing only inside a live orchestration graph.

### Direct integration plus executable verification worked

After repeated Langflow integration failures, the Build Brief and surviving specialist reports were
integrated directly into a local project tree. The resulting candidate was then exercised with unit
tests and local validation rather than being accepted on model confidence.

## What failed

### A nominal 300-second execution ceiling dominated the visual flow

Slow reasoning workers consumed almost the entire flow window, leaving the final integrator only a
small remainder. Increasing Langflow's server worker timeout did not remove every observed
300-second failure, indicating that more than one timeout layer could affect the campaign.

The operational lesson is that per-request, per-stage, and whole-run timeouts are different
controls. A harness must represent them separately and report which one actually fired.

### Large reasoning surfaces performed badly

A single DeepSeek runtime-architecture assignment took roughly 282 seconds. A broader reliability
assignment also ran into the timeout envelope. Splitting work by cognitive shape helped more than
merely shortening prompt text.

The scheduling lesson is that "one specialist" is not automatically one sensible stage. Tasks that
combine architecture, security, test design, persistence, cancellation, and cost semantics are
several jobs wearing one coat.

### Giant one-shot integration was fragile

GLM and later Kimi Code were both asked to ingest the Build Brief plus multiple specialist reports
and emit an entire multi-file project inside one giant bootstrap-script completion. That generation
shape repeatedly ran out of wall-clock time.

The problem was not simply model intelligence. The requested completion combined reconciliation,
architecture decisions, code generation, documentation, tests, and transport encoding into one
large serial stage.

Future campaigns should prefer bounded code-generation groups or direct workspace generation when
available, followed by deterministic tests and a repair pass.

### Side-effect branches were not reliable checkpoints by themselves

A Langflow `Write File` component hanging off a worker output was not sufficient to guarantee that
all side branches executed. Checkpoint writes had to be made part of the dependency path when their
execution mattered.

The general lesson is that persistence is part of execution semantics, not decorative telemetry.
The harness should own it directly rather than asking a model or optional side branch to perform it.

### One worker produced the wrong assignment

The supplied `Deepseek2.md` output was a continuity/authority-state report rather than the intended
testing/failure blueprint. This exposed another orchestration requirement: successful transport and
a green component state do not prove semantic task completion.

A future harness should make stage contracts inspectable and allow explicit review or automated
checks before downstream stages consume outputs.

## Final integration path

The final V0 candidate was built by treating the V0 Build Brief as primary implementation input and
using the useful worker reports as specialist review material where consistent with it.

Important deterministic resolutions included:

- closed strict-JSON objects and duplicate-key rejection at every nesting level;
- explicit numeric bounds and no type coercion;
- filename-safe stage identifiers;
- deterministic input and synthesis rendering;
- bounded `asyncio` worker concurrency;
- independent immediate stage persistence;
- atomic artifact replacement and serialized event writing;
- exact provider-reported cost only, represented with decimal semantics;
- explicit known/unknown/partial aggregate cost state;
- per-stage and overall timeout semantics;
- sanitized provider failures and no API-key persistence;
- fixed default run discovery plus an explicit `--runs-dir` override;
- no retries, autonomous delegation, model shell, database, RAG, OMC, or Git mutation in V0.

## Validation performed on the first candidate

The first integrated candidate was validated locally with no real OpenRouter request:

- `pytest`: 32 passed;
- example manifest validation: passed;
- `python -m compileall`: passed;
- bootstrap syntax check: passed;
- bootstrap reconstruction: 44/44 original generated files byte-identical;
- package installation/import: passed under the available Python 3.13.5 environment.

Python 3.12-specific runtime verification was not available in that environment. No real API smoke
run, deployment, or production claim was made.

## Method findings carried forward

1. Plan the campaign once; do not let workers invent topology.
2. Keep worker roles bounded by reasoning surface, not merely prompt length.
3. Persist each completed stage immediately and independently.
4. Treat transport success as different from semantic task success.
5. Distinguish per-request, per-stage, and whole-run timeout semantics.
6. Avoid one giant "integrate everything and emit the whole repository" completion when direct or
   bounded file generation is possible.
7. Let tests locate the sharp edges after the first integrated candidate, then run a bounded repair
   pass.
8. Keep consequential actions behind a human gate.

## Bottom line

Langflow was useful as a topology prototype and as a way to discover failure shapes. It was a poor
fit for the final long-running integration stage. The experiment nevertheless validated the central
B.O.T.S. 5 direction: a deterministic execution shell around bounded nondeterministic workers is
more useful than an autonomous committee of agents.
