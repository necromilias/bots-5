# B.O.T.S. 5 V0.2 Design Campaign Report

Date: 2026-08-31

Classification: **COMPLETE WITH DESIGN CORRECTIONS REQUIRED**.

## Purpose and conclusion

This report records the V0.2 local OpenAI-compatible provider design campaign, the provider-response
normalization defect discovered during synthesis recovery, the accepted repair and operating-procedure
lessons, and the final Kimi K3 synthesis result.

The campaign objective was design only. V0.2 implementation has not begun.

The six-worker R3 design swarm completed normally, and the final Kimi K3 synthesis also completed
normally with exact provider `finish_reason: "stop"`. The synthesis is useful and converges on the
intended narrow architecture, but human review found three blocking specification contradictions that
must be corrected before implementation:

1. the synthesis accidentally restates the pre-fix null-content response rule and would reintroduce
   the reasoning-only incomplete-response defect on the local provider path;
2. it proposes validated schema-v2 provider configuration without giving that configuration a
   coherent validated representation in the `Job` model;
3. it proposes a generic single-provider runner compatibility adapter that cannot reliably detect
   provider identity because the current `Provider` protocol has no provider-identity field.

No further paid synthesis run is required. The next action is human adjudication and correction of the
completed synthesis into the accepted V0.2 implementation specification.

## Scope

The V0.2 feature under design is deliberately narrow:

- add one built-in generic local OpenAI-compatible provider;
- preserve the existing `CompletionRequest -> CompletionResult` provider seam;
- allow OpenRouter and local-provider stages to coexist;
- support explicit non-secret local endpoint configuration and optional environment-indirected
  authentication;
- preserve non-streaming execution, completion semantics, persistence, timeout behavior, cost
  semantics, synthesis gating, failure semantics, and OpenRouter behavior.

Explicitly outside V0.2:

- retries;
- streaming;
- model discovery;
- planner-generated manifests;
- tools/function calling;
- RAG or OMC integration;
- richer DAG semantics;
- campaign resume/checkpoint implementation;
- plugin/discovery frameworks;
- distributed execution;
- UI work;
- unrelated refactoring.

## Tested identity

Implementation repository:

`necromilias/bots-5`

Authoritative implementation revision after the provider-response repair:

`2fbb2a591c95f84247888a050a8af4086acaac29`

At that revision:

- exact `finish_reason == "stop"` remains the only normal-completion criterion;
- incomplete required stages remain overall-run failures;
- incomplete synthesis dependencies remain blocked;
- explicit reasoning-only incomplete OpenRouter responses are normalized correctly;
- the full deterministic suite passes 78 tests;
- `docs/OPERATING_PROCEDURE_V1.md` contains the accepted model-selection and reasoning-budget rules
  discovered during this campaign.

## Design-swarm chronology

### Initial execution-environment failure

The first paid campaign attempt was executed from the default Codex sandbox and failed before model
work because DNS resolution was unavailable.

Run:

`bots5-v0.2-local-openai-design-swarm-20260831T081324Z-d90521ca`

Observed result:

- all workers failed with provider transport errors caused by name-resolution failure;
- synthesis was skipped with `dependency_failed`;
- durable failure evidence was preserved correctly.

A zero-spend diagnostic established that the host/network-capable environment resolved and reached
OpenRouter normally.

Accepted operational finding:

**Live provider campaigns require the host/network-capable execution environment.**

The sandbox remains suitable for zero-network validation and deterministic tests.

### R1 host-network rerun

Run:

`bots5-v0.2-local-openai-design-swarm-20260831T083203Z-b323b991`

Observed result:

- architecture: complete / `stop`;
- manifest: incomplete / `length`;
- transport: complete / `stop`;
- runtime: complete / `stop`;
- tests: complete / `stop`;
- redteam: `malformed_provider_response`;
- synthesis: skipped.

Known aggregate cost:

USD `0.2060373106`

The manifest stage exhausted its 5,000-token ceiling. The redteam failure required separate diagnosis.

### R2

Run:

`bots5-v0.2-local-openai-design-swarm-20260831T085402Z-e5d57fc3`

Changes from the preceding campaign were limited to the evidence-driven corrections:

- manifest output ceiling raised from 5,000 to 8,000;
- redteam model changed after its abnormal response.

Observed result:

- architecture: complete;
- manifest: complete;
- transport: complete;
- runtime: complete;
- tests: incomplete / `length`;
- redteam: complete;
- synthesis: skipped with `dependency_incomplete`.

Aggregate cost:

USD `0.17307127944`

This reinforced that completion ceilings are capacity boundaries and must be sized from observed task
behavior rather than inherited mechanically.

### R3

Run:

`bots5-v0.2-local-openai-design-swarm-20260831T090828Z-eadce9d8`

All six root workers completed normally with exact provider `finish_reason: "stop"`.

Worker aggregate cost:

USD `0.09255486384`

The six authoritative worker-output SHA-256 values are:

- architecture: `188b972b5e5e1c4a24d9c8bb34026eed3afb8e3326fade9ad9bdccd437a61900`
- manifest: `ff597f2cda6559f594c997dcf115873445f82ccf425e39b00e997754248d6c3b`
- transport: `7e8186befc9c5d7042491d5766c94cf07baab483b56bbfdffb5b8825cdabf053`
- runtime: `d95dc155d4268c8200f62a7586852f2fd67816a6394e29e04442d435639dbf7e`
- tests: `87fff146a4c330afcdc83051a66b44af2344fbd71ee0fcf647cf65e055aa08c4`
- redteam: `0074d2e22287688c8260a045e913948a926de3c85f483700c1fb91356263d103`

The R3 synthesis stage, using GLM, failed with `malformed_provider_response`.

The six normally completed root-worker outputs were preserved as immutable design evidence and reused
for isolated synthesis recovery rather than rerunning successful workers.

## Synthesis-recovery investigation

### First Qwen recovery

Run:

`bots5-v0.2-local-openai-design-synthesis-recover-20260831T094215Z-0670aa4e`

Model:

`qwen/qwen3.8-2.4t-a95b`

Result:

`ProviderResponseError: malformed_provider_response`

No authoritative synthesis was produced.

At this point abnormal malformed-response outcomes had appeared across multiple model families. That
made continued model substitution weaker than investigation of the shared OpenRouter normalization
path.

## Provider-response defect

A sanitized diagnostic reproduced the response shape with a tiny Qwen request:

- HTTP 200;
- valid JSON object;
- valid first choice;
- valid message object;
- `message.content: null`;
- reasoning text/details present;
- `finish_reason: "length"`;
- usage present;
- provider cost present.

The model had consumed its completion allowance in reasoning before producing visible final content.

The defect was in `src/bots5/providers/openrouter.py`: B.O.T.S. rejected non-string content before
correctly interpreting the trustworthy non-stop finish reason.

The old behavior discarded valid:

- finish reason;
- request ID;
- returned model;
- token usage;
- reasoning-token usage;
- known provider cost.

## Accepted repair

The surgical repair was committed and pushed as:

`2fbb2a591c95f84247888a050a8af4086acaac29`

Commit message:

`Handle reasoning-only incomplete responses`

The repaired semantics are:

- non-empty string content: unchanged;
- whitespace-only string content: `empty_model_response`;
- missing content key: malformed;
- unsupported array/object/number/boolean content: malformed;
- explicit `content: null` plus a trustworthy string `finish_reason != "stop"`:
  normal `CompletionResult(output_text="")`, incomplete completion, telemetry preserved;
- explicit `content: null` plus exact `finish_reason == "stop"`:
  `empty_model_response`;
- null content without a trustworthy string finish reason: malformed.

Reasoning text and reasoning details are never promoted into worker output.

No runner change was required. The existing rule remains:

`completion_complete = (finish_reason == "stop")`

Validation after the repair:

- targeted provider/runner tests: 45 passed;
- full suite: 78 passed;
- `git diff --check`: passed.

## Live repair canary

Run:

`bots5-v0.2-local-openai-reasoning-incomplete-liv-20260831T110206Z-370f236d`

The tiny canary deliberately reproduced reasoning-only exhaustion.

Observed result:

- stage state: succeeded;
- completion complete: false;
- finish reason: `length`;
- output artifact: 0 bytes;
- prompt tokens: 361;
- completion tokens: 24;
- reasoning tokens: 24;
- total tokens: 385;
- provider cost: USD `0.000866`;
- no provider-response error;
- overall run: failed because exact `stop` was not reached.

This live run proved the repaired normalization path end to end.

## Operating Procedure v1 findings

Two procedure findings were accepted during the campaign.

### Evidence-driven model selection

Committed as:

`efcc6cda265dd3ea471d6f508b6f107ea68215ce`

Observed campaign behavior now overrides prior model-selection rationale. Repeated truncation,
malformed responses, route instability, timeouts, or other abnormal completion can remove a
model/provider route from critical-path roles unless evidence shows the failure condition has been
eliminated.

Model diversity remains useful only when it returns reliable evidence. It is not a goal that outranks
normal completion.

### Reasoning-aware completion ceilings

For reasoning-capable models, completion capacity may be consumed by both reasoning and visible
answer tokens. Operators must therefore size the ceiling for both, or explicitly constrain reasoning
where the chosen model/provider and campaign contract support doing so.

## Qwen 24k synthesis recovery

Run:

`bots5-v0.2-local-openai-design-synthesis-recover-20260831T110942Z-7c53499f`

Model:

`qwen/qwen3.8-2.4t-a95b`

Ceiling:

24,000 completion tokens

Observed result:

- stage state: succeeded;
- completion complete: false;
- finish reason: `length`;
- prompt tokens: 23,842;
- completion tokens: 24,000;
- reasoning tokens: 16,310;
- total tokens: 47,842;
- provider cost: USD `0.191684`;
- preserved visible output: 34,454 characters;
- no provider error, timeout, malformed response, or shutdown warning.

This was a normal incomplete completion, not a B.O.T.S. defect.

The incomplete synthesis was retained as evidence only and was not accepted.

## Final Kimi K3 synthesis

Run:

`bots5-v0.2-local-openai-design-synthesis-recover-20260831T112740Z-80751f14`

Model:

`moonshotai/kimi-k3`

Observed result:

- exit code: 0;
- terminal run state: succeeded;
- synthesis state: succeeded;
- completion complete: true;
- finish reason: `stop`;
- prompt tokens: 22,369;
- completion tokens: 8,655;
- reasoning tokens: 0;
- total tokens: 31,024;
- provider cost: USD `0.196932`;
- duration: 132.681 seconds;
- output size: 38,645 bytes / 38,593 characters;
- no provider error, timeout, malformed response, routing anomaly, or shutdown warning.

Synthesis artifact SHA-256:

`a330c15bfe67f001d66b2ba0c13d2ce84fc9c3b2b7925afe2e8ea8d30a34676e`

Run inventory SHA-256:

`6b4518562aa4f562d47af368b735bde313de03fc75349151e79438d5c713d42d`

This is the first normally completed synthesis for the V0.2 design campaign.

Mechanical completion makes it valid synthesis evidence. It does not automatically make every design
choice accepted project policy.

## Human synthesis review

The completed synthesis is substantively strong. It converges on:

- one built-in `local_openai` provider;
- explicit per-stage provider selection;
- the existing completion abstraction;
- optional environment-indirected authentication;
- deterministic no-network tests;
- no plugin framework, retries, streaming, discovery, or unrelated architecture.

Three blocking corrections are required before implementation.

### 1. Preserve current reasoning-only incomplete semantics

The synthesis says local normalization should match OpenRouter, but its detailed normalization/error
tables revert to the older rule that non-string content is malformed.

That contradicts current authoritative OpenRouter behavior at
`2fbb2a591c95f84247888a050a8af4086acaac29`.

The local provider must preserve:

- explicit `content: null`;
- trustworthy string `finish_reason != "stop"`;

as valid empty incomplete output with telemetry preserved.

Implementing the synthesis literally would reintroduce the repaired defect on the local-provider path.

### 2. Give provider configuration a validated representation

The synthesis proposes schema-v2:

- `providers.local_openai.base_url`;
- `providers.local_openai.api_key_env`;

and says the non-secret provider configuration should survive into `job.resolved.json`.

It simultaneously says `models.py` requires no structural change.

Those claims are incompatible with the current validation flow, which returns a validated `Job`.
Provider configuration needs an explicit validated representation carried by `Job` or an equally
explicit validated companion structure. The CLI should not re-read or depend on raw unvalidated JSON
after validation.

The likely narrow design is a small validated provider-config model attached to schema-v2 `Job`.

### 3. Resolve runner API compatibility coherently

The synthesis proposes a provider mapping as the new runner API while also preserving
`run_job(job, provider)` and requiring the old single-provider form to reject provider/manifest
mismatch.

The current `Provider` protocol has no provider-identity field. A generic runner therefore cannot
reliably know whether an arbitrary `Provider` instance represents OpenRouter, local OpenAI, or a test
fake.

The narrow choices are:

- make the provider mapping the authoritative runner API and update internal/test callers; or
- carry provider identity explicitly with the single instance.

Concrete-class `isinstance` routing inside the runner should be avoided.

## Remaining human decisions

The synthesis statement that no unresolved human decisions remain is not accepted.

Before implementation, adjudicate:

1. schema v2 versus a schema-v1 extension;
2. exact validated provider-config representation;
3. whether single-provider runner compatibility is required;
4. fixed versus arbitrary credential-environment-variable naming;
5. endpoint host policy.

No paid rerun is required to resolve these questions.

## Additional campaign finding: stage-output reuse

Several campaign revisions recomputed already-good worker outputs because a sibling stage failed or
truncated.

This is now observed operational pain rather than a hypothetical feature idea.

It remains explicitly outside V0.2. A later bounded campaign-stage reuse/resume feature may be
considered after the current provider milestone.

## Evidence ownership

The authoritative raw execution evidence remains in the preserved B.O.T.S. campaign/run directories.
This report records the human interpretation and accepted operational findings; it does not duplicate
complete model outputs or create a competing raw-evidence store.

Historical failed and incomplete runs remain immutable evidence and were not rewritten after diagnosis.

## Current status and next action

V0.2 design campaign execution is complete.

Status:

**COMPLETE WITH DESIGN CORRECTIONS REQUIRED**

The next action is zero-spend human design adjudication:

1. correct the three blocking contradictions;
2. resolve the remaining human design choices;
3. produce the accepted V0.2 implementation specification;
4. only then authorize implementation.

No additional provider run is required at this stage.

## V0.2 implementation closure

The accepted implementation specification corrected the three blocking design contradictions above:

- schema v2 is closed and adds required top-level `providers`, while schema v1 remains unchanged;
- validated `LocalOpenAIConfig` data is carried by `Job` and only non-secret provider configuration is
  persisted;
- `run_job(job, providers, ...)` is the sole runner form, and every declared stage is routed through
  its provider ID.

The implementation adds one built-in non-streaming `OpenAICompatibleProvider`. It accepts an explicit
HTTP/HTTPS API base, appends `/chat/completions`, optionally reads a configured environment-indirected
Bearer credential, preserves unknown local cost, and uses independent provider-local normalization:
OpenRouter retains its authoritative classifier while the local provider matches it, including
reasoning-only incomplete responses. No retries, streaming, discovery, plugins, planner, tools, RAG,
richer DAG, stage reuse/resume, UI, daemon, or Organisational Memory update is part of this closure;
no live V0.2 provider canary has been performed.

Deterministic offline implementation validation is the current implementation evidence. It covered the
full deterministic test suite, schema-v1 and
schema-v2 examples, compileall, diff whitespace, secret absence, and byte-preservation of the frozen
`docs/OPERATING_PROCEDURE_V1.md`. The implementation candidate remains uncommitted and unmerged to
`main` for human review.
