ROLE
Phase 10 lifecycle/concurrency/failure-forensics specialist.

ROUTING
Model `minimax/minimax-m2.7`. Requested effort `provider_maximum_if_exposed`. Record configured/effective values truthfully; no silent substitution.

TASK
Attack Phase 10 from failure boundaries: desktop close during running campaign, provider request accepted but local outcome uncertain, event/persist failure, crash between attempt creation and terminal settlement, partial worker set, synthesis gate/rerun crash, UI observer lag, stale filesystem state, restart with run marked running, regeneration cancellation and New Job while activity exists.

ALLOWED
Read-only source/authority analysis and bounded zero-provider probes where useful.

FORBIDDEN
No mutation. No invented retry/resume. Do not convert uncertainty into failure/success. Do not expand into daemon architecture.

EVIDENCE
Enumerate concrete crash/interruption points, current durable evidence at each, required invariant, safe convergence/display behavior and which conditions must fail closed or remain unknown.

OUTPUT
`reports/design/MINIMAX_LIFECYCLE_FORENSICS.md`.

STOP CONDITION
Stop after the accepted Phase 10 design surface has a finite adversarial failure matrix.
