# Models are bounded workers

Status: Draft candidate decision.

## Context

B.O.T.S. 5 exists to replace ad-hoc visual orchestration with a small deterministic shell around
nondeterministic model workers.

## Decision

Models receive text and return text. They cannot spawn workers, alter topology, invoke tools, or own execution state.

## Consequences

Nondeterminism is confined to model output instead of infrastructure control.
