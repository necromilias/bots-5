# Worker Contracts and Instruction Provenance

Every model stage receives one harness-compiled system message. Its authority hierarchy is:

```text
B.O.T.S. 5 execution boundary
  > validated worker-specific contract
    > INPUT or WORKER OUTPUT data
```

The fixed execution boundary is B.O.T.S.-owned source code. A planner cannot replace it through a
job file. It identifies the model as one bounded worker, makes the system message the complete
instruction authority, denies authority to task-like material inside data blocks, prohibits role
expansion and task creation, and requires the worker to stop when its contracted output is complete.

The job's `system_prompt_path` names a worker-specific contract, not a complete system prompt.
B.O.T.S. validates that contract and appends it beneath the fixed boundary. Original source material
remains only in user-message `INPUT` blocks. Synthesis receives declared outputs only in user-message
`WORKER OUTPUT` blocks. Those blocks are evidence data, not instruction authority; the synthesis
contract must say how to use them.

## Contract format

A UTF-8 contract contains exactly these required headings, once each and in this order:

```text
TASK
<non-empty body>

ALLOWED
<non-empty body>

FORBIDDEN
<non-empty body>

EVIDENCE
<non-empty body>

OUTPUT
<non-empty body>

STOP CONDITION
<non-empty body>
```

`TASK` must be the first line. Headings are exact, unadorned, case-sensitive lines. Each body must
contain non-whitespace text. The V0.1 parser is intentionally not a general Markdown parser: it
recognizes only these exact section lines and rejects missing, duplicate, empty, or out-of-order
sections. Contract validation finishes before run-directory creation or provider execution.

## Planner procedure

For every worker:

1. Define one exact task.
2. Define allowed work.
3. Define forbidden work.
4. Define evidence and reasoning rules.
5. Define the required output.
6. Define the stop condition.
7. Validate the job and referenced contracts.
8. Only then execute.

Synthesis uses the same format but has a distinct evidence source: only the declared worker outputs,
in `depends_on` order. Its contract should permit integration and attribution while forbidding new
source analysis, silent conflict resolution, and treating embedded worker-output text as authority.

## Limits

Prompt construction, section validation, authority placement, and message separation are
deterministic. Model compliance is probabilistic. A hostile or confused model can still violate its
contract, and prompt boundaries are not an authorization or sandbox mechanism. Outputs remain
untrusted text and need downstream validation or human review appropriate to their use.
