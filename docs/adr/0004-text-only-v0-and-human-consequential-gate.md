# Text-only V0 and human consequential gate

Status: Draft candidate decision.

## Context

B.O.T.S. 5 exists to replace ad-hoc visual orchestration with a small deterministic shell around
nondeterministic model workers.

## Decision

V0 performs no model-directed shell, filesystem mutation outside run artifacts, Git mutation, deployment, or other consequential action.

## Consequences

Useful orchestration can be proven without granting agents operational authority; later mutation features require a new reviewed capability model.
