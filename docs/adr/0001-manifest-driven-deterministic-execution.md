# Manifest-driven deterministic execution

Status: Draft candidate decision.

## Context

B.O.T.S. 5 exists to replace ad-hoc visual orchestration with a small deterministic shell around
nondeterministic model workers.

## Decision

V0 execution topology and limits are declared in a strict, versioned JSON manifest validated before work starts.

## Consequences

The harness is predictable and reviewable; schema changes become explicit compatibility decisions.
