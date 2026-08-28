# OpenRouter-first provider boundary

Status: Draft candidate decision.

## Context

B.O.T.S. 5 exists to replace ad-hoc visual orchestration with a small deterministic shell around
nondeterministic model workers.

## Decision

V0 implements one narrow Provider protocol and one OpenRouter implementation.

## Consequences

A later local provider can reuse job semantics without introducing a generic plugin system in V0.
