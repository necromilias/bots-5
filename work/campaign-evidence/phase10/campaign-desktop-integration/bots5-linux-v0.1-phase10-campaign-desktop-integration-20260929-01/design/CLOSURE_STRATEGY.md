# Closure strategy and pre-commit boundary

Scope: what "Phase 10 is technically complete" means, what evidence must exist, and what is
explicitly **not** implied. Sources: CONTRACT §"Objective", §"Human gates";
`parcel/SCOPE.json` `later_gates`; `parcel/VALIDATION.md` §"Independent review sequence";
`parcel/BASELINE.json` (Phase 9 closure precedent); FINDINGS F-16/F-18.

## 1. Technical completion definition

Phase 10 is technically complete only when **all** of the following hold:

1. The accepted Phase 10 behavior is operator-reachable from the native desktop.
2. The headless campaign engine remains independently usable (CLI `validate/run/status/
   inspect` unchanged in shape and exit codes; regeneration/rerun available headlessly).
3. Filesystem run evidence remains authoritative and fully reconstructable, including
   current-attempt selection and synthesis staleness, with no competing truth store.
4. Regeneration and stale-synthesis semantics are append-only, truthful and mechanically
   reconstructable; no historical evidence was rewritten.
5. Historical V0/V0.2 run directories remain readable (golden-fixture proof).
6. An exact frozen implementation candidate, covered by a materialized manifest+seal,
   passes the required validation, independent review and final oracle through the sealed
   pre-commit boundary, byte-identical after the final T4.

## 2. Required evidence set at the boundary

- design: all `design/*` artifacts and the exact `seals/design/**` seal chain;
- continuation: Mick's verbatim adjudication, successor campaign record, immutable
  parcel-v2, re-lint results, rendered implementation prompts;
- implementation: candidate path list, per-file SHA-256, candidate manifest + seal;
- validation: every gate's exact command, selector, collected count, exit code, and
  candidate/seal identity; the final T4 record;
- review: fresh Qwen3.8 implementation falsification; targeted MiniMax lifecycle review
  (if material); targeted Step proof-sufficiency review (if material); classification and
  repair records; resealed candidate identity;
- optional Jamba final review (only if invoked; max one; normally unused);
- oracle: fresh MiMo final implementation oracle against the exact seal + validation;
- routing: model/effort requested/configured/effective for every launch; honest recording
  of any capability limitation (e.g. no per-child effort parameter);
- `reports/COMMIT_BOUNDARY_REPORT.md` reconciling the above.

## 3. Pre-commit boundary state

At the stop point:

- **nothing is staged**, **nothing is committed**, **nothing is pushed**;
- no ref is mutated (no tag/branch creation or movement) by this campaign;
- OrgMem is untouched;
- `work/**` and unrelated untracked material are preserved;
- the sealed candidate exists as working-tree bytes, byte-identical to the seal.

The campaign stops here and reports; it does not infer further authority.

## 4. Separate consequences (never inferred)

| Consequence | Owner |
|---|---|
| Technical completion (this campaign) | supervisor + evidence |
| Substantive acceptance | Mick only |
| Stage and commit | Mick only |
| Push / landing | Mick only |
| OrgMem mutation | Mick only |

Design acceptance is not commit authority. Implementation authority is not substantive
acceptance. Commit is not push. Push is not OrgMem landing.

## 5. Documentation status discipline (F-16)

Some tracked docs still describe a pre-commit Phase 9 state while OrgMem Decision 0012 and
`parcel/BASELINE.json` record Phase 9 closed and landed. Phase 10 must not treat that stale
prose as current-state authority, and the base fence intentionally does **not** refresh it:
doc refresh is optional scope, not required by the accepted Phase 10 obligations, and would
add unreviewed prose risk to a milestone boundary. If Mick wants current-state doc refresh,
it should be a separately accepted, explicitly listed fence addition in parcel-v2.

## 6. Failure to close

If any required element cannot be completed inside the accepted fence, the campaign stops
with a concrete blocker (authority, budget, or a genuine product/authority fork) rather than
broadening scope. A recurring semantic defect class requires a fresh different-family
diagnosis before another mutation attempt. No historical evidence is ever "fixed" to make a
gate pass.
