# Operating Procedure v1

This is the frozen normal operating procedure for B.O.T.S. 5 paid campaigns. It records the accepted
operator lessons from the first useful campaign, the revised useful campaign, and controlled
failure-path validation.

## 1. Pin and review the campaign

Before execution:

- identify the exact repository revision under test;
- review the job manifest, every declared input, every worker contract, and the synthesis contract;
- verify the intended topology, models, parallelism, stage timeouts, whole-run timeout, output-token
  ceilings, synthesis dependencies, and output directory;
- run `bots5 validate JOB.json` before any paid execution.

Validation makes no provider call and creates no run directory.

## 2. Select workers for the actual roles

Before finalizing the campaign manifest, evaluate currently available worker candidates against the
requirements of each declared role.

Do not assume that a generally capable model, a previously successful model, or a model used by an
example campaign is automatically appropriate for the new task.

For each worker and synthesis role, consider as applicable:

- capability for the specific task;
- required context capacity;
- reasoning behavior and reasoning-token overhead;
- ability to follow bounded output contracts and stop normally;
- reliability under the intended output-token ceiling;
- latency;
- provider availability and routing behavior;
- input and output pricing;
- observed quality on comparable work.

Prefer evidence from relevant prior runs when available. A model that produces high-quality work but
repeatedly exhausts its output allowance, violates required output shape, or incurs disproportionate
reasoning cost may be a poor operational choice for a bounded worker role.

Observed campaign behavior overrides prior model-selection rationale. If a model/provider route
repeatedly fails mechanically in a role through truncation, malformed responses, routing instability,
timeouts, or other abnormal completion, remove it from critical-path roles in subsequent campaign
revisions unless there is specific evidence that the failure condition has been eliminated. Do not
preserve model diversity for its own sake. Diversity is subordinate to reliable normal completion and
useful output. Previously selected or previously successful models are not defaults; re-evaluate
alternatives using the newest relevant evidence.

Record the selected model for each role and the reason it was chosen. The reviewed manifest remains
the authority for the exact models used during execution.

Model selection is part of campaign design. B.O.T.S. executes the selected topology; it does not infer
or silently substitute a supposedly better model.

## 3. Check credentials without exposing them

Confirm `OPENROUTER_API_KEY` exists in the execution environment without printing or copying it into
a manifest, report, or run artifact.

## 4. Preflight cost conservatively

Immediately before a paid run:

- record the highest applicable currently advertised input/output pricing available for each selected
  model/provider route;
- calculate a conservative expected upper bound from the campaign's prompt size, stage count, and
  configured output ceilings;
- remember that `stop_before_synthesis_if_known_cost_exceeds_usd` is only a post-worker synthesis
  gate, not a whole-run budget;
- remember that unknown or partial worker cost does not automatically block synthesis.

After execution, persisted provider-reported cost is the accounting truth for requests where the
provider supplied cost telemetry.

## 5. Set output ceilings deliberately

`max_output_tokens` is a hard capacity boundary, not an output target.

Choose each ceiling with enough margin for the required contract output. A stage that returns usable
text with any finish reason other than exact `stop` is incomplete and cannot count as normally
completed evidence.

For synthesis, prefer stronger prioritisation and deduplication over simply increasing the ceiling
when the task can be expressed more compactly.

## 6. Run intentionally once

Execute:

```bash
bots5 run JOB.json
```

Record the printed run ID and resolved run-directory location. Do not automatically rerun a failed or
timed-out paid campaign. Inspect the first run before deciding what changed condition would justify
another execution.

## 7. Inspect completion, not just stage state

Use `bots5 status` and `bots5 inspect` for every stage.

A stage counts as normally complete only when all of the following are true:

- state is `succeeded`;
- completion is complete;
- finish reason is exact `stop`.

The existence of `result.md` is not proof of overall success. Usable incomplete synthesis output may
produce `result.md` while the run is failed.

`depends_on` controls synthesis inputs and dependency gating. Every declared worker still participates
in final whole-run success.

## 8. Interpret run-directory paths correctly

Manifest `output.runs_dir` resolves relative to the job file.

CLI `status --runs-dir` and `inspect --runs-dir` interpret a relative path from the current working
directory. Use an absolute path when inspecting from a different directory.

## 9. Preserve routine evidence

For an ordinary useful campaign, preserve:

- the reviewed campaign assets;
- the exact repository revision under test;
- any campaign-defined source/asset checksum verification;
- the authoritative B.O.T.S. run directory unchanged;
- the run ID and provider-reported usage/cost;
- the completed human review or campaign report.

Do not automatically repeat conformance-era ceremony such as full historical evidence replication,
remote-ref proofs, hostile-input canaries, complete repository hash inventories, or fresh full test
suites unless the campaign is specifically validating implementation, publication, or an anomaly.

## 10. Make a separate human usefulness decision

Technical state and substantive acceptance are independent.

After mechanical inspection, record whether the output:

- answered the intended task;
- stayed inside the declared evidence and role boundaries;
- preserved uncertainty instead of inventing certainty;
- produced traceable, non-generic findings or work product;
- was understandable from the durable artifacts;
- was worth the cost and operator effort.

A mechanically failed run may still contain useful evidence. A mechanically successful run may still
be low value. Neither classification silently changes the other.

## 11. Convert findings only after adjudication

Worker and synthesis findings are evidence, not authority.

Before changing implementation, documentation, or procedure, classify each finding as one of:

- implementation defect;
- documentation/procedure defect;
- acceptable current behaviour;
- defer.

Apply only accepted corrections. Do not treat model ranking or synthesis wording as automatic project
policy.
