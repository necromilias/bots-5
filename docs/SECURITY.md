# Security

## Trust model

V0 is a local operator-run CLI. The operator and selected manifest are trusted. Model output and
provider errors are not authority. This is not a hostile multi-user sandbox.

## API secret

`OPENROUTER_API_KEY` is read only at runtime. It is excluded from resolved jobs, run metadata,
events, usage, stage files, CLI inspection, and examples. Provider exceptions are sanitized before
persistence. Raw request headers are never logged.

## Filesystem

B.O.T.S. 5 reads only the job file plus explicitly declared input/prompt files. It writes only beneath
the resolved configured `runs_dir`. The runs directory cannot resolve to filesystem root. Stage IDs
use a filename-safe grammar. Generated write paths are checked to remain beneath the run directory,
and writes use fresh temporary files plus atomic replacement.

These measures prevent obvious accidental traversal/symlink misuse. They are not a hardened sandbox
against a malicious local operator with access to the same account.

## Models have no tools

The runtime sends text to models and receives text. Models have no shell, filesystem tool, Git tool,
worker-spawn ability, or repository mutation authority.

## Cost control limitation

`stop_before_synthesis_if_known_cost_exceeds_usd` runs only after workers have already executed. It
can prevent synthesis based on the exact known worker subtotal. It cannot prevent workers from
collectively spending more than the threshold and it is not a hard budget.

## Before consequential actions

A later version that can mutate files, Git, services, or external systems requires explicit tool
capabilities, stronger path/permission boundaries, auditable approval gates, and corresponding tests.
V0 intentionally provides none of those actions.
