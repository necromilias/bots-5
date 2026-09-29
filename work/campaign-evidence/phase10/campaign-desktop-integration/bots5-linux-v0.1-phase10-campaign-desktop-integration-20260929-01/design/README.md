# Design output directory

The supervisor creates, at minimum:

- `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md`
- `PREFLIGHT_APPROVAL_STATE_MACHINE.md`
- `CAMPAIGN_EVIDENCE_EVOLUTION.md`
- `REGENERATION_AND_STALE_SYNTHESIS.md`
- `DESKTOP_SURFACE_AND_LIFECYCLE.md`
- `MUTATION_FENCE.json`
- `IMPLEMENTATION_SEQUENCE.md`
- `VALIDATION_PLAN.md`
- `CLOSURE_STRATEGY.md`

The design seal must enumerate/hash the exact design artefacts before GPT-6 Luna
falsification. If design bytes change, preserve the old seal and generate a new seal.
