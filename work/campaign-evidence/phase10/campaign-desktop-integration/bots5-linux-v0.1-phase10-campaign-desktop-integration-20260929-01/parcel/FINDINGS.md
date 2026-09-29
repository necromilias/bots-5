# Pre-campaign findings to verify, attack and either adopt or refute

These are inspection findings, not a pre-decided implementation.

## F-01 — regeneration is an engine/evidence change, not a button

Current `StageRecord`/storage exposes one record/output per stage ID. Phase 10 requires
sibling worker attempts and preserved original evidence. A UI-only overwrite is
forbidden.

## F-02 — no generic retry/resume/reuse authority

V0.2 explicitly deferred stage reuse/resume. Phase 10 authorizes selected terminal
worker regeneration and explicit synthesis rerun, not arbitrary retry/resume or
automatic continuation of interrupted paid work.

## F-03 — approval has a TOCTOU problem

The job contains filesystem paths; validation/preflight can happen earlier than
execution, and runner reads referenced bytes at execution. Approval must be bound to
the exact bytes/configuration that execute, not merely to a pathname that can change.

## F-04 — provider change is not explicitly granted on regeneration

Product authority explicitly permits an explicit **model** change. Preserve the
original provider route by default. If changing provider is required/desirable and not
mechanically implied, present it as a HUMAN_SEMANTIC_FORK.

## F-05 — live cost cannot mean invented money

The harness persists provider-reported cost when available. It does not have a
campaign pricing registry and non-streaming stages generally report cost at terminal
completion. Show known subtotal + unknowns truthfully. Do not invent token-dollar
progress.

## F-06 — paid preflight pricing source is currently absent

OPv1 requires conservative preflight pricing from advertised rates, but source
inspection found no campaign pricing registry/lookup. Determine whether existing
authority settles operator-entered rates, provider lookup, cached metadata, unknown
cost + approval, or another path. If not, stop for Mick with explicit options.

## F-07 — live progress is state progress, not necessarily token streaming

The old campaign providers are non-streaming for the harness. Phase 10 does not
silently authorize redesigning provider execution into streaming merely to animate UI.
Worker output may become expandable when persisted.

## F-08 — filesystem evidence remains authority

UI events/callbacks/polling are projections. `run.json`, stage evidence, usage,
events and outputs remain independently reconstructable. Do not create a competing
SQLite campaign truth.

## F-09 — shutdown/provider uncertainty must remain truthful

Active campaign execution is consequential external work. Cancellation cannot claim
remote compute/billing stopped if unknown, and uncertain external acceptance cannot
silently retry.

## F-10 — old run directories must remain inspectable

Evidence evolution must not rewrite V0/V0.2 historical directories in place. Additive
schema/version/read compatibility is required.

## F-11 — interrupted run is not automatically resumable

A process crash can leave durable state claiming `running`. UI may classify/display
the durable partial evidence but must not fabricate recovery, completion or automatic
resume.

## F-12 — New Job is presentation state

`New Job` clears/selects the current desktop campaign working context. It must not
delete or mutate historical run evidence.

## F-13 — regeneration and synthesis rerun need fresh spend approval

A prior run's approval does not silently authorize another provider request. Define
the exact re-approval/preflight binding.

## F-14 — synthesis staleness must be mechanical

After selecting a regenerated worker sibling, any synthesis dependent on the old
selection becomes stale relative to the current dependency set. Explicit rerun
creates a new synthesis attempt and preserves the old one.

## F-15 — durable result view may expose a UX-persistence fork

Product language says durable result view and that completed/failed results remain
visible until `New Job`. Architecture allows desktop state in app DB while campaign
evidence stays filesystem-authoritative. Determine whether restart persistence of the
selected loaded/result campaign is mechanically settled. If not, present a human fork
rather than guessing.

## F-16 — Phase 9 administrative truth outranks stale tracked status prose

OrgMem Decision 0012 records Phase 9 landed/closed. Some tracked docs embedded in the
Slice E commit still describe a pre-commit candidate because final landing happened
after those bytes were sealed. Phase 10 closure may refresh current-state docs inside
its accepted fence, but must not rewrite historical evidence or fabricate a future
Phase 10 commit.

## F-17 — Phase 9 deferred cleanup is not Phase 10

Retained-installation cleanup remains deferred. Do not smuggle it into campaign
desktop work.

## F-18 — candidate-seal governance is non-negotiable

Slice E exposed a process error where post-seal review was requested before the
implementation seal had actually been materialized. Phase 10 must materialize and
verify the exact implementation seal before any independent implementation review.
Launched parcels remain immutable; authority change creates a successor parcel.
