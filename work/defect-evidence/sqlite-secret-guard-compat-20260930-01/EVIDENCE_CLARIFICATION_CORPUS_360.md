# Evidence clarification — the "360" in the T4-era equivalence summary

Requested by Mick, 2026-09-30. **Evidence clarification only.** No candidate file was
modified, nothing was resealed, T4 was not re-run, nothing was staged, committed or pushed,
and no worker was launched. Read-only re-derivation over the existing corpus.

## 1. What the ambiguous sentence should have said

The original sentence — "13,443-value corpus (baseline vs repaired vs Python) → 0
disagreements; both diverge from Python on the same 360 values" — mixes two different
comparisons. It is accurate but not self-explanatory. The precise statement is:

* **baseline SQL vs repaired SQL: 0 disagreements** (this is the equivalence claim);
* **SQL vs Python: 360 disagreements, and they are identical for baseline and repaired**
  (this is a pre-existing property of the guard, unchanged by the repair, and not part of
  the equivalence claim).

## 2. The three requested comparisons

Corpus: `equivalence_corpus.VALUES`, 13,443 de-duplicated deterministic values, evaluated on
SQLite 3.53.4 on a bare in-memory connection. Baseline predicate taken from the pristine
pre-repair source (`git show HEAD:src/bots5/core/secrets.py`, mirrored at
`scratch/baseline_secrets.py`); repaired predicate from the sealed working tree.

| # | Comparison | Disagreement count |
|---|---|---|
| 1 | baseline SQL predicate vs repaired SQL predicate | **0** |
| 2 | repaired SQL predicate vs Python `is_forbidden_secret_key()` | **360** |
| 3 | baseline SQL predicate vs Python `is_forbidden_secret_key()` | **360** |

Both SQL predicates disagree with Python on exactly the same 360 inputs, so
comparison 1 is 0 and comparison 2 equals comparison 3.

## 3. Direction of every SQL-vs-Python disagreement

| Direction | Meaning | baseline SQL | repaired SQL |
|---|---|---|---|
| Python forbidden / SQL allowed | SQL false negative (a secret passes the guard) | **0** | **0** |
| Python allowed / SQL forbidden | SQL false positive (a benign key is rejected) | **360** | **360** |

There are **no SQL false negatives**. All 360 are SQL false positives: inputs Python accepts
and the trigger rejects.

## 4. Classification of the 360

| Sub-class | Count | Mechanism |
|---|---|---|
| key contains an embedded NUL | 305 | the guard's first branch, `instr(CAST(col AS TEXT), char(0)) > 0`, rejects any key containing NUL; Python strips the NUL during normalization and then compares |
| key is a permutation of a forbidden key's letters (anagram closure) | 55 | the per-letter count test plus `NOT GLOB '*[A-Za-z0-9]*'` residual accepts any ordering; Python requires exact normalized equality |
| **total** | **360** | |

- The 55 non-NUL inputs are permutations of forbidden keys — measured: all 55 have the same
  casefolded ASCII-alphanumeric multiset as a forbidden key, and they implicate all 11
  forbidden keys (`accesstoekn`, `apieky`, `apikeyvaleu`, `apikye`, `apitoekn`, …).
- The 305 NUL inputs are ones whose Python-normalized form is **not** forbidden (e.g.
  `"\x00"`, `"\x00\x00IoneH"`, `"\x00FWzZ6\n0v"`); three normalize to the empty string.
  The corpus holds 419 NUL-bearing values in total; in 114 of them Python *also* forbids the
  key, so those agree and are not part of the 360. 305 + 114 = 419.
- The NUL guard branch and the count/residual formulation are byte-for-byte unchanged by the
  repair, which is why the two predicates produce identical results on all 360.

## 5. Answer to the A/B/C/D question

**C — SQL false positives**, in a mixture of two sub-classes (NUL fail-closed and anagram
closure). Specifically:

- Not **A**: this is a genuine final-predicate mismatch. It is not merely an intermediate
  normalization difference — the final forbidden/not-forbidden booleans genuinely differ.
- Not **D**: there are zero SQL false negatives.
- **B only partially, and only for the NUL sub-class.** An embedded NUL inside a JSON object
  key is pathological and arguably outside the meaningful input domain; the anagram
  sub-class is squarely inside it (e.g. the benign-looking key `apieky` is rejected).

## 6. If these are treated as genuine mismatches: adjudication

- **Pre-existing defect preserved exactly by the candidate.** Yes. Baseline and repaired
  produce identical verdicts on all 360; the repair neither introduced, widened nor narrowed
  any of them.
- **Security-relevant.** Not in the leak direction: every divergence is an *over*-rejection,
  and the false-negative count is zero, so no secret can pass the guard that the predicate
  intends to stop. The NUL branch is in fact deliberate fail-closed behaviour and is asserted
  by the existing test `tests/test_phase5_provider_model.py::test_raw_phase5_metadata_and_current_declared_types_are_authoritative`,
  which requires the NUL variant to be rejected. The anagram sub-class is a usability/
  availability false positive on JSON metadata and provenance keys, not a security gap.
- **Mechanically repairable inside the current semantic fence.** No. Matching Python exactly
  would require (a) dropping or weakening the NUL fail-closed branch and (b) replacing the
  order-insensitive count test with exact normalized-string equality, both of which change
  accepted, test-asserted behaviour. That is a semantics change, not a compatibility repair,
  so it is outside this campaign's fence. (It is plausibly *technically* achievable in pure
  SQL now that the staged-derived-table technique exists — materialising the fully normalized
  string and testing `IN (forbidden)` — but the obstacle is authority, not mechanism.)
- **New scope/authority question requiring Mick.** Yes, if the over-rejection is to be
  changed. It was not changed, and nothing was repaired.

## 7. Did the independent falsifiers observe and adjudicate this?

Yes. Both found it independently, reported it as pre-existing, confirmed it is identical in
baseline and candidate, and placed it out of scope. Quoting them:

**Design falsifier** — `falsifier/DESIGN_FALSIFICATION.md` §7, "Pre-existing
baseline↔Python divergence (not a candidate defect)":

> "The baseline SQL predicate is a strict *anagram-closure* of `is_forbidden_secret_key`:
> e.g. `nekto`/`opssward`/`etselaevcur` are rejected by the trigger but accepted by Python.
> Over the whole corpus the mirror found **19,927** such over-rejections and **0** missed
> secrets (Python-forbidden inputs the SQL accepts) [...] The candidate reproduces every one
> of them exactly (0 disagreements). The proposed repair is compatibility-only and does not
> change guard strength; if the over-rejection is itself considered a defect it is **out of
> scope and unchanged**."

and §9:

> "If the repair's acceptance criterion also requires the guard to keep rejecting anagrams
> and NUL-containing keys exactly as today, that is satisfied (identical results). If it
> additionally requires the SQL guard to match the Python predicate exactly, that criterion
> was already violated by the baseline and is **not** addressed by this candidate."

**Implementation falsifier** — `falsifier/IMPLEMENTATION_FALSIFICATION.md` §9 item 5:

> "**Anagram over-rejection is pre-existing.** The predicate still flags anagrams of
> forbidden keys (e.g. `tekno` ≈ `token`) because it tests per-letter counts rather than
> order. I confirmed this is *identical* in baseline and candidate and is documented by the
> test file as an accepted over-rejection; I did not try to judge whether that contract is
> desirable."

Neither falsifier found a counterexample to baseline≡repaired, and neither proposed changing
the behaviour.

## 8. Status

The candidate remains sealed and untouched at
`4948c0cd…` / `821a0e36…` / `06d23bfa…`. This document changes no candidate file. **Stopping
here** pending your decision on whether the pre-existing over-rejection is in scope for a
future, separately authorised campaign.
