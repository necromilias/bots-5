# First Useful Campaign Report

## Purpose

This record captures the first genuinely useful low-stakes B.O.T.S. 5 campaign after V0.1
worker-boundary and completion-semantics closure.

The campaign performed a read-only audit of the B.O.T.S. 5 repository using three bounded root
workers plus synthesis. It also exercised the ordinary human-operated validate -> run -> status ->
inspect workflow rather than another conformance-specific canary.

This document is a human review of the observed campaign. Worker and synthesis findings remain
evidence until separately adjudicated; model output does not become project policy automatically.

## Tested identity

- repository: `necromilias/bots-5`
- branch: `v0.1-worker-boundary-hardening`
- tested revision: `ae784504c05bb9cd9560d17b1f5b7fccc3e9a1ca`
- campaign manifest: `examples/first-useful-audit/job.json`
- run ID: `bots5-first-useful-repository-audit-20260829T202248Z-1325dcb1`

The campaign package had previously passed its offline validator, 24/24 selected source hashes,
and 9/9 campaign asset hashes at the tested revision.

## Result

**FAIL - HIGH SUBSTANTIVE VALUE**

The campaign is mechanically failed because the required synthesis stage did not complete normally.

All three root workers completed with exact provider `finish_reason: stop`. Synthesis returned usable
output but terminated with `finish_reason: length` at 4,996 completion tokens against its 5,000-token
ceiling. Persisted synthesis completion was therefore incomplete and the overall run correctly became
`failed`.

This is the intended V0.1 distinction between provider execution success and semantic completion.
The useful-work run therefore provided live evidence that the final completion-semantics fix at
`ae784504...` behaved correctly.

## Stage evidence

| Stage | Model | State | Finish reason | Completion | Total tokens | Cost USD |
| --- | --- | --- | --- | --- | ---: | ---: |
| consistency | `openai/gpt-5.6-luna` | succeeded | `stop` | complete | 25,290 | 0.0096122 |
| operator | `openai/gpt-5.6-luna` | succeeded | `stop` | complete | 25,610 | 0.0100209 |
| adversary | `google/gemini-3.7-flash` | succeeded | `stop` | complete | 30,442 | 0.0361755 |
| synthesis | `google/gemini-3.7-flash` | succeeded | `length` | incomplete | 13,289 | 0.02495475 |

Aggregate provider-reported cost: **USD 0.08076335**.

Aggregate cost status was known and complete.

## Human usefulness review

The campaign was worth the provider cost despite failing its declared completion gate.

The three complete root workers produced evidence-backed repository and operator findings rather than
generic redesign suggestions. The adversary also rejected several plausible false positives, which
materially improved the audit signal.

The visible synthesis output successfully deduplicated and ranked much of the worker material, but
attempted to retain too much detail for its configured ceiling. Its truncation is therefore treated as
a campaign-design failure, not a B.O.T.S. infrastructure failure.

The normal operator workflow was understandable without another orchestrator. `bots5 status` and
`bots5 inspect` exposed the exact failure cause directly:

```text
synthesis: state=succeeded completion=incomplete finish_reason='length'
run: state=failed
```

No conformance-era forensic reconstruction was required to determine what happened.

## Adjudication of campaign observations

The campaign observations were subsequently checked against current implementation and normative
documentation. The accepted disposition is:

1. **Unknown or partial worker cost at the synthesis gate:** documentation/procedure gap, not an
   implementation defect. The gate intentionally compares only the known worker subtotal. Unknown or
   partial worker cost does not itself block synthesis. Documentation must state this explicitly.
2. **Paid-run pricing preflight:** accepted procedure gap. Operating procedure should record the
   highest applicable currently advertised pricing used for conservative planning, while persisted
   provider-reported cost remains final accounting truth after execution.
3. **Token ceilings and completion review:** accepted procedure gap. Output ceilings must be chosen
   deliberately and exact `finish_reason == "stop"` checked after execution.
4. **`result.md` semantics:** accepted documentation defect. `result.md` may exist for usable but
   incomplete synthesis output while the overall run is failed; file presence alone does not prove
   normal completion or acceptance.
5. **Skipped-stage zero-cost provenance:** accepted narrow documentation issue, not a schema change.
   A skipped stage can have harness-known zero cost because no provider request was sent; this must be
   distinguished in wording from provider-reported cost.
6. **Synthesis dependencies versus whole-run success:** accepted documentation clarity issue.
   `depends_on` controls synthesis input and gating, while every declared worker still participates in
   whole-run success.
7. **Relative run-directory semantics:** accepted operator-documentation gap. Manifest `runs_dir` is
   job-relative; CLI `--runs-dir` is CWD-relative.
8. **Routine evidence retention:** accepted procedure gap. Ordinary campaigns should preserve the
   reviewed campaign assets, pinned revision, campaign-defined checksum verification, authoritative run
   tree, usage/cost, and human review without automatically repeating conformance-era ceremony.
9. **Human usefulness acceptance:** accepted procedure gap. Mechanical run state and substantive human
   acceptance are separate checkpoints.
10. **Extreme manifest numeric values:** accepted as a plausible real implementation defect but
    explicitly deferred because its operational consequence is low and it does not block the revised
    useful campaign or Operating Procedure work.

No implementation change is required before the revised useful campaign as a result of findings 1-9.
Finding 10 remains deferred implementation work.

The accepted procedure corrections are captured in
`OPERATING_PROCEDURE_V1_CANDIDATE.md` and supporting normative documentation.

## Campaign-design finding

Do not rerun this campaign unchanged.

The immediate correction is to tighten the synthesis contract so it must prioritise and deduplicate
more aggressively. Increasing the synthesis token ceiling should not be the first response when the
existing output already contained enough information but attempted to write too much of it.

The first-useful campaign package used for the observed run is not currently tracked in this repository,
so its synthesis contract is not modified by this adjudication patch. It must be refreshed in the
reviewed campaign package before the next paid execution.

## Evidence ownership

The authoritative raw execution evidence is the persisted B.O.T.S. run tree for:

`bots5-first-useful-repository-audit-20260829T202248Z-1325dcb1`

This report is the durable human interpretation of that run. It intentionally does not duplicate the
complete raw stage outputs or claim a separate byte-for-byte forensic package.

## Next action

1. use `OPERATING_PROCEDURE_V1_CANDIDATE.md` as the candidate normal operating baseline;
2. tighten the external first-useful campaign synthesis contract;
3. execute one revised useful campaign once;
4. inspect both mechanical completion and substantive usefulness;
5. run the planned controlled failure-path campaign;
6. freeze Operating Procedure v1 only if those campaigns expose no blocking procedure or harness defect.
