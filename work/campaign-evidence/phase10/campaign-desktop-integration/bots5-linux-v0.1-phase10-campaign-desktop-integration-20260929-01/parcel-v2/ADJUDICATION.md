# Mick adjudication — Phase 10 design acceptance and implementation authority

Recorded verbatim from Mick's decision, 2026-09-29, in response to
`reports/oracle/MIMO_FINAL_DESIGN_ORACLE_v4.md` and the design gate presentation.

Precedence: this adjudication is the highest authority in the parcel-v2 precedence order.

---

## Verbatim record

I accept Phase 10 design seal v4 9739e87cd42459973a600e2d1b2b19408671f93e82fbff2d13c71b11e57ed9c1 as the implementation design and grant Phase 10 implementation authority inside the unchanged sealed mutation fence.
Human semantic forks are adjudicated as follows:
HSF-1: Branch A. Preserve OPv1 §4. For any paid approval, require operator-supplied currently advertised input/output rates together with recorded source, observation time, route and the accepted fixed conservative upper-bound basis. Do not add a pricing registry, automatic network lookup, or OPv1 waiver.
HSF-2: 2a. Use filesystem-only restart persistence. Do not add an application-database campaign pointer, migration, or associated fence expansion.
HSF-3: 3a. Phase 10 uses explicit campaign/run path or run ID. Do not add run-directory enumeration/browsing infrastructure.
HSF-4: 4a. Add RunState.CANCELLED for explicit operator cancellation. Stage-level cancellation uses error_type="cancelled". HSF-4 governs the run-level state. Generic run failure remains FAILED with its own internal_error; sibling stages cancelled during that failure retain their own cancelled cause rather than inheriting the run failure. Durable cancelled_pending after hard kill remains interrupted/uncertain and is never represented as successful or automatically retried.
HSF-5: 5a. Provider route remains locked. Explicit model changes permitted by the accepted regeneration design do not authorize provider-route switching.
I accept oracle residuals O-1 through O-4 as non-blocking design errata. Do not mutate or replace design seal v4. Record the following normalized corrections in the retained continuation and immutable parcel-v2 before any implementation worker launches:
O-1: a non-dispatched terminal synthesis is classified NOT_APPLICABLE before the staleness predicate is evaluated; update task-facing wording accordingly.
O-2: validation must explicitly prove that a sibling cancelled during generic run failure has error_type == "cancelled" while the run remains FAILED with internal_error.
O-3: the stage-level cancellation label is cancelled; HSF-4 changes the run-level state only.
O-4: standardize the new serialized field name as api_key_source.
These are mechanical task-facing clarifications, not a new design round, scope expansion, or authority to alter unrelated semantics.
Amend the MiMo-V2.6 Flash campaign budget from 3 launches to 4. The fourth launch is reserved exclusively for the fresh final implementation oracle against the final frozen/reseeded implementation candidate. It may not be spent on design, implementation, repair, diagnosis, intermediate review, or substitution. No other model or repair budget resets.
Persist this adjudication verbatim, create the retained continuation/successor campaign record and immutable parcel-v2, rerun v0.3 structural/semantic/prompt consistency checks, and then proceed autonomously through implementation, impact-based validation, independent implementation falsification/review, bounded repairs, candidate resealing, final T4, evidence reconciliation and the reserved MiMo implementation oracle.
If a concrete defect is fixable inside the accepted fence, fix it. If the fence itself, accepted product semantics, dependency boundary, or one of these adjudications must change, STOP FOR MICK.
Stop at the sealed pre-commit boundary for substantive acceptance.
This does not authorize staging, commit, push, ref mutation, OrgMem mutation, dependency changes, Phase 11/12 work, or anything outside the accepted Phase 10 fence.

---

## Recorded effect

- **Design seal v4 is frozen and must not be mutated or replaced.** It remains the
  implementation design of record. The O-1…O-4 corrections below are task-facing
  clarifications carried by parcel-v2; they are not a new design round and no design
  artifact was edited for them.
- **Implementation authority is granted inside the unchanged sealed mutation fence**
  (`design/MUTATION_FENCE.json`: 9 modify, 8 add). The fence did not expand.
- **HSF-1 → Branch A**, **HSF-2 → 2a**, **HSF-3 → 3a**, **HSF-4 → 4a**, **HSF-5 → 5a**.
  No pricing registry, no network lookup, no OPv1 waiver; no app-DB campaign pointer or
  migration; no run-directory enumeration infrastructure; no provider-route switching.
- **MiMo V2.6 Flash cap 3 → 4.** The fourth launch is reserved exclusively for the fresh
  final implementation oracle against the final frozen/resealed implementation candidate.
  It may not be used for design, implementation, repair, diagnosis, intermediate review or
  substitution. No other family cap and no repair-wave budget resets.
- **Not authorized:** staging, commit, push, ref mutation, OrgMem mutation, dependency
  changes, Phase 11/12 work, or anything outside the accepted Phase 10 fence.

## Structured form

See `parcel-v2/ADJUDICATION.json` for the machine-readable record and
`parcel-v2/NORMALIZED_CORRECTIONS.md` for the O-1…O-4 task-facing clarifications.
