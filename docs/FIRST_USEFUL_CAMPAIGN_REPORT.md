# First Useful Campaign Report

## Purpose

This record captures the first genuinely useful low-stakes B.O.T.S. 5 campaign after V0.1
worker-boundary and completion-semantics closure.

The campaign performed a read-only audit of the B.O.T.S. 5 repository using three bounded root
workers plus synthesis. It also exercised the ordinary human-operated validate -> run -> status ->
inspect workflow rather than another conformance-specific canary.

This document is a human review of the observed campaign. Worker and synthesis findings remain
evidence until separately adjudicated; this report does not convert model output into project
policy or implementation authority.

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

The campaign is mechanically failed because the required synthesis stage did not complete
normally.

All three root workers completed with exact provider `finish_reason: stop`. Synthesis returned
usable output but terminated with `finish_reason: length` at 4,996 completion tokens against its
5,000-token ceiling. Persisted synthesis completion was therefore incomplete and the overall run
correctly became `failed`.

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

The three complete root workers produced evidence-backed repository and operator findings rather
than generic redesign suggestions. The adversary also rejected several plausible false positives,
which materially improved the audit signal.

The visible synthesis output successfully deduplicated and ranked much of the worker material, but
attempted to retain too much detail for its configured ceiling. Its truncation is therefore treated
as a campaign-design failure, not a B.O.T.S. infrastructure failure.

The normal operator workflow was also understandable without another orchestrator. `bots5 status`
and `bots5 inspect` exposed the exact failure cause directly:

```text
synthesis: state=succeeded completion=incomplete finish_reason='length'
run: state=failed
```

No conformance-era forensic reconstruction was required to determine what happened.

## Observations requiring adjudication

The following are observations from the completed worker outputs and partial synthesis. They are not
accepted fixes or policy.

1. **Unknown or partial worker cost at the synthesis gate.** The current gate compares the known
   worker subtotal; unknown components do not themselves block synthesis. The intended operator
   policy requires explicit adjudication.
2. **Paid-run pricing preflight.** The prior OpenRouter pricing-surface discrepancy still leaves a
   procedure gap for conservative preflight estimates.
3. **Token ceilings and completion review.** Normal operation should make ceiling adequacy and exact
   `finish_reason == "stop"` review explicit. This campaign demonstrated the consequence directly.
4. **`result.md` semantics.** Usable incomplete synthesis output may exist while the overall run is
   failed. File existence alone therefore cannot mean accepted final result.
5. **Skipped-stage zero-cost provenance.** A skipped provider stage incurs zero provider cost, but
   current metadata does not distinguish synthetic skip-zero from provider-reported zero. Whether
   that distinction is worth implementing remains open.
6. **Synthesis dependencies versus whole-run success.** `depends_on` controls synthesis input and
   gating, while all declared workers still participate in final run success. Operator wording can
   make that distinction clearer.
7. **Relative run-directory semantics.** Manifest-relative `output.runs_dir` and CLI-relative
   `--runs-dir` use different reference points and can surprise an operator changing directories.
8. **Routine evidence retention.** Ordinary useful runs need a smaller explicit retention procedure
   rather than repeating conformance-era evidence ceremony.
9. **Human usefulness acceptance.** Technical run state and substantive human acceptance should be
   explicit separate checkpoints in Operating Procedure v1.
10. **Extreme manifest numeric values.** A worker identified a plausible low-risk overflow path in
    numeric validation. This can wait pending direct verification.

## Campaign-design finding

Do not rerun this campaign unchanged.

The immediate correction is to tighten the synthesis contract so it must prioritise and deduplicate
more aggressively. Increasing the synthesis token ceiling should not be the first response when the
existing output already contained enough information but attempted to write too much of it.

## Evidence ownership

The authoritative raw execution evidence is the persisted B.O.T.S. run tree for:

`bots5-first-useful-repository-audit-20260829T202248Z-1325dcb1`

This report is the durable human interpretation of that run. It intentionally does not duplicate
the complete raw stage outputs or claim a separate byte-for-byte forensic package.

## Next action

1. preserve this run and report;
2. adjudicate the observations individually;
3. convert only accepted observations into implementation, documentation, or procedure changes;
4. tighten the synthesis contract;
5. execute one revised useful campaign;
6. decide whether Operating Procedure v1 is ready to freeze only after that campaign.

No implementation change is justified solely because this synthesis reached its output ceiling.
