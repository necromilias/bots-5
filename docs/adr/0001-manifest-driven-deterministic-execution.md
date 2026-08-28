# ADR 0001: Manifest-driven deterministic execution

Status: Accepted for V0 candidate

## Context

Visual/manual orchestration is difficult to reproduce and gives models too much opportunity to
invent execution shape.

## Decision

V0 executes a strict versioned JSON manifest. The harness owns validation, ordering, concurrency,
persistence, and stop conditions.

## Consequences

Jobs are reviewable and repeatable around nondeterministic model calls. Richer DAGs require an
explicit later schema change rather than worker invention.
