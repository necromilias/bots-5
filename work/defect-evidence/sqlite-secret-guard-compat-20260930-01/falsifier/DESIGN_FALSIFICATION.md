# Design falsification — Phase 5 SQLite secret-guard compatibility candidate

**Campaign:** `work/defect-evidence/sqlite-secret-guard-compat-20260930-01/`
**Repo HEAD:** `9762170099889ecd87d451341a15a29ce7aceae8` (branch `main`)
**Reviewer role:** independent design falsifier (no tracked file modified)
**Date of run:** 2026-09-30

**VERDICT: NOT FALSIFIED.** Across 97,443 adversarial predicate inputs, 6,927 real
`json_tree` keys from 1,510 JSON documents, and 135 trigger-level documents, the
candidate predicate is bit-for-bit equivalent to the baseline predicate. I found
**zero** counterexamples. The candidate compiles as all four Phase 5 trigger shapes on
isolated SQLite 3.45.1, where the baseline fails with `parser stack overflow`.

The claim survives, with the residual uncertainty listed in §8.

---

## 1. Artifacts and exact revisions

| File | SHA-256 |
|---|---|
| `src/bots5/core/secrets.py` (baseline) | `317e0698fe5aab71d0ac0d297bcba59248eef6ecdd5188829956a0cd24b44084` |
| `.../versions/0007_phase5_provider_model_configuration.py` | `46bc12a9cd10b4c6a262b5bc5ca0a02ecc5d60201931a45dffe7fef3cf69eea6` |
| `scratch/candidate_secrets.py` (candidate under test) | `c1a1ec8cc7dcf818d913d5d1c26e1c82ad15bff323271026cb829180a47a5fde` |
| `falsifier/baseline_expr.sql` (column `v`) | `35cca41a7440d3a99eb0347ef858fc636e9f12bb91bea52b653ea701d356d3ae` |
| `falsifier/candidate_expr.sql` (column `v`) | `2e27337f257354c11ed08185697a9b7d4f9600b74abc8ab58e3f98abcebedc78` |

Baseline generated expression for column `v`: **369,321 bytes**, paren depth **98**,
**15,296** `replace(` calls. Candidate: **11,295 bytes**, paren depth **16**, **185**
`replace(` calls. (The campaign's 369,679-byte figure is for column `key`; the delta is
just the repeated 1- vs 3-character column name.)

`git status --porcelain -- src tests docs` is **empty**: no tracked file was touched.
Every artifact was written under `.../falsifier/`.

Environments used:

* project interpreter `.venv/bin/python` 3.12.13, `sqlite3.sqlite_version == 3.50.4`
  (parses the 98-deep baseline expression);
* isolated SQLite **3.45.1** via `scratch/libsqlite3-3.45.1.so` + `scratch/ctypes_sqlite.py`
  and CLI `scratch/sqlite3-3450100`;
* isolated SQLite **3.46.0** CLI `scratch/sqlite3-3460000`.

## 2. Why the "separator removal is unnecessary" argument holds

Baseline inner expression is `E = lower(CAST(col AS TEXT))`, then 19 Unicode
`replace(char(cp)→fold)` steps, then removal of all 66 `_ASCII_NON_ALNUM_CODES`.
Candidate computes `N` = the same lower+19-fold chain and **omits the 66 separator
removals**; it keeps the same `IN (forbidden)` list and the same per-key
count/residual tests, but wrapped in an `EXISTS` over a derived table.

Every character in every forbidden key is an ASCII lower-case letter. Every removed
separator is a non-alphanumeric ASCII code point. Therefore:

1. **Counts are unchanged.** `length(E) - length(replace(E,c,''))` for `c` a key letter
   equals the same expression on `N`, because the only difference between the two
   strings is deleted characters that are never equal to `c`.
2. **The residual test is unchanged.** Baseline residual is `candidate residual` minus
   ASCII separators. `GLOB '*[A-Za-z0-9]*'` matches only `[0-9A-Za-z]`; deleted
   separators are outside that class, so `NOT GLOB …` has the same truth value.
3. **`E IN forbidden` is subsumed.** If baseline separator-stripped `E` equals a key `K`,
   candidate's `N` is `K` plus only non-alnum ASCII, so candidate's count test for `K`
   matches and its residual is exactly those non-alnum characters ⇒ candidate's residual
   variant fires. Conversely if candidate's `N IN forbidden`, no separators were present,
   so `E = N` and baseline's `IN` fires.
4. **The 19 folds are order-independent.** Target code points are disjoint non-ASCII and
   every fold output is ASCII alphanumeric (verified in §5.7), so no fold output can be a
   later fold target; printing the later stage textually first (stage 2 before stage 1)
   does not change the mapping.
5. The NUL guard `instr(CAST(col AS TEXT), char(0)) > 0` is textually identical in both,
   so any embedded-NUL behaviour is shared (measured in §4).

This is a complete argument for equivalence; the rest of the work was adversarial
empirical confirmation.

## 3. Method

I generated my own corpus (`falsifier/gen_cases.py`) and evaluated the **real** emitted
SQL of both generators. I did not reuse the campaign's corpus.

* **Mirror** (`gen_cases.pred_sql`): an independent Python transliteration of the SQL
  semantics, used only as a fast pre-filter. It is **not** load-bearing: the headline
  result is SQL-vs-SQL.
* **Full SQL** (`full_compare.py`): all 97,443 corpus values inserted by parameter binding
  (so embedded NUL survives; verified `instr('a\x00b',char(0))=2`), then one
  `SELECT (baseline_expr), (candidate_expr)` per 8,000-row chunk, compared as booleans.
* **json_tree** (`json_stress.py`): 1,507 generated nested JSON documents; both predicates
  run inside the true usage shape
  `SELECT count(*) FROM json_tree(?) WHERE key IS NOT NULL AND (<expr>)`.
* **Trigger** (`trigger_test.py`): the migration's real `safe_json_object` wrapper with
  `NEW.metadata_json`; baseline + candidate triggers on 3.50.4, candidate trigger on
  3.45.1.
* **Version replay** (`version_replay.py`): 1,510 docs through the trigger on 3.45.1 and
  3.50.4.
* **Compile** (`full_trigger_compile.py`): all four `_install_phase5_row_guards` trigger
  shapes (metadata insert/update, provenance insert/update) on 3.45.1, 3.46.0, 3.50.4.
* **Structural audit** (`audit_candidate.py`) and **Unicode scan** (`unicode_map_scan.py`).

Corpus composition (97,443 unique values):

* every forbidden key × {upper, title, reverse}, every one of the 66 ASCII separators
  inserted at start / middle / end, digit prefixes/suffixes, extra letters, doubled keys;
* 3,250 targeted strings including every one of the 66 ASCII separators inserted into every
  key at start/middle/end, digit/letter near-misses, and the 19 mapped code points used to
  *stretch* into each key (`paßword`, `paſsword`, `toK…`, `accesﬅoken`, `apiİey`, …);
* 6,440 random strings (6,000 over letters/digits/ASCII punctuation/`\x00`/the 19 mapped
  characters/astral emoji, plus 40 random anagram+separator variants per key);
* an **exhaustive** grammar: all 88,741 strings of length ≤ 4 over the 17-character
  alphabet `{t,o,k,e,n,p,a,s,1,!,X,ß,ſ,İ,K,é,\x00}` (covers mixed case, digits,
  separators, NUL and 6 of the 19 folds in every short combination).

## 4. Raw evidence

### 4.1 Full SQL equivalence over the entire corpus

Command: `.venv/bin/python .../falsifier/full_compare.py`

```
progress 8000/97443 mismatches=0 ...
progress 97443/97443 mismatches=0 elapsed=195.9s
FULL SQL baseline != candidate: 0
```

`falsifier/full_sql_result.json` contains `{"total": 97443, "mismatches": []}`.

### 4.2 Mirror pre-filter

Command: `.venv/bin/python .../falsifier/compare.py`

```
cases loaded 97443
mirror baseline != candidate: 0
mirror baseline != python: 19927 (false positives: 19927 )
mirror candidate != python: 19927 (false positives: 19927 )
SQL subset size 6146
mirror-vs-SQL mismatches: 0
SQL baseline != SQL candidate: 0
```

### 4.3 `json_tree` usage context

Command: `.venv/bin/python .../falsifier/json_stress.py`

```
docs 1507 keys 6927
json_tree baseline != candidate: 0
json_tree baseline != python ground truth: 95
  PYDISAGREE '[{"opssward": [], "erlvcetasue": []}, 1]' base 2 cand 2 py False
  PYDISAGREE '{"etselaevcur": null}' base 1 cand 1 py False
  ...
```

All 95 divergences from Python are the **same over-rejection** in baseline and candidate
(anagram of a forbidden key, e.g. `opssward`/`etselaevcur`); see §7.

### 4.4 Trigger level and 3.45.1 compile

Command: `.venv/bin/python .../falsifier/trigger_test.py`
(`falsifier/trigger_test_output.txt`)

```
docs: 135
baseline(3.50) vs candidate(3.50) trigger diffs: 0
baseline-vs-python MISSED secrets (SQL accepted, python forbidden): 0
baseline-vs-python OVER-REJECTIONS (python benign, SQL rejected): 4
   OVER '{"nekto":1}'
   OVER '{"enotk":1}'
   OVER '{"pa\u1e98ssord":1}'
   OVER '{"password\u0000x":1}'
python sqlite version 3.50.4
3.45.1 candidate trigger CREATE OK; replay rows 135
candidate(3.50) vs candidate(3.45.1) diffs: 0
3.45.1 baseline trigger CREATE: False
```

Note the NUL case: `{"password\u0000x":1}` — Python `json.loads` gives key
`password\x00x`, Python normalize → `passwordx` (benign), but both SQL predicates reject
it via the shared `instr(...,char(0))` branch. Candidate reproduces the baseline exactly,
including this over-rejection.

### 4.5 Four real trigger shapes on 3.45.1 / 3.46.0 / 3.50.4

Command: `.venv/bin/python .../falsifier/full_trigger_compile.py`

```
== 3.45.1 (ctypes) candidate ==
  metadata_insert: rc=0 ... provenance_update: rc=0
== 3.45.1 (ctypes) baseline ==
  metadata_insert: rc=1 parser stack overflow
  metadata_update: rc=1 parser stack overflow
  provenance_insert: rc=1 parser stack overflow
  provenance_update: rc=1 parser stack overflow
== 3.46.0 (CLI) candidate ==  all rc=0
== 3.46.0 (CLI) baseline ==  all rc=0
== 3.50.4 (python) candidate: all rc=0
== 3.50.4 (python) baseline:  all rc=0
```

Candidate: **4/4 triggers on 3.45.1 and 3.46.0 and 3.50.4**. Baseline: **0/4 on
3.45.1**, 4/4 on 3.46.0/3.50.4.

### 4.6 End-to-end version replay (3.45.1 vs 3.50.4)

Command: `.venv/bin/python .../falsifier/version_replay.py`

```
docs 1510
baseline(3.50) vs candidate(3.50): 0
candidate(3.50) vs candidate(3.45.1): 0
```

### 4.7 Structural audit of the generated candidate SQL

Command: `.venv/bin/python .../falsifier/audit_candidate.py`

```
fold pairs as mapping == expected mapping: True
disjoint targets: True
all folds are ASCII alnum: True
empty-replacement chars emitted: [97,99,...,122]      # letters only
empty-replacement chars == residual key chars: True   # no ASCII separator removed
IN list == sorted forbidden: True
residual columns: ['0'..'10']
counts match: True
casefold aliases: ['2','1','0']                       # 3 stages, 10+9 folds
```

The only `char(N), ''` removals in candidate SQL are the 20 distinct key letters used for
the residual columns; none of the 66 `_ASCII_NON_ALNUM_CODES` appears. The forbidden list,
per-key count constants, residual character sets and the 19-entry fold mapping all match
`src/bots5/core/secrets.py` exactly.

### 4.8 Unicode case-fold map completeness

Command: `.venv/bin/python .../falsifier/unicode_map_scan.py`

```
code points (>=0x80) whose casefold contains an ASCII alnum: 19
NOT in the 19-entry map (baseline SQL gap): 0
```

Scan of all of U+0080–U+10FFFF (surrogates skipped) against Python 3.12.13: the 19-entry
`_UNICODE_CASEFOLD_ASCII_MAP` is exactly the set of code points whose `casefold()`
contributes an ASCII alphanumeric. This also confirms the residual non-ASCII handling in
§2.3 cannot hide a casefold-produced ASCII letter.

### 4.9 Human-auditable raw table

`falsifier/evidence_table.md` (excerpt; full table in the file). "agree" compares
baseline SQL vs candidate SQL only.

| value | baseline SQL | candidate SQL | Python | agree |
|---|---|---|---|---|
| `'token'` | 1 | 1 | True | yes |
| `'nekto'` (anagram) | 1 | 1 | False | yes |
| `'to!ken'` | 1 | 1 | True | yes |
| `'apricotKey'` | 0 | 0 | False | yes |
| `'token1'` | 0 | 0 | False | yes |
| `'tokén'` | 0 | 0 | False | yes |
| `'paßword'` | 1 | 1 | True | yes |
| `'paſsword'` | 1 | 1 | True | yes |
| `'paẘssord'` | 1 | 1 | False | yes |
| `'passẘord'` | 1 | 1 | True | yes |
| `'toKen'` | 1 | 1 | True | yes |
| `'apiİey'` | 0 | 0 | False | yes |
| `'refreſhtoken'` | 1 | 1 | True | yes |
| `'passwordé'` | 1 | 1 | True | yes |
| `'péssword'` | 0 | 0 | False | yes |
| `'password\x00x'` | 1 | 1 | False | yes |
| `'\x00token'` | 1 | 1 | True | yes |
| `'token\n'` | 1 | 1 | True | yes |
| `'a'*100000` | 0 | 0 | False | yes |
| `'!'*100000 + 'token'` | 1 | 1 | True | yes |
| `'🙂token'` | 1 | 1 | True | yes |
| `NULL` | None | None | False | yes |

## 5. Attempted counterexample classes

| Attack | Result |
|---|---|
| every ASCII separator inserted into every key (all 3 positions) | no divergence |
| mapped chars used to stretch into keys (`paßword`, `paſsword`, `accesﬅoken`, `toK…`) | no divergence |
| mapped char as the *only* content / doubled / mixed with NUL | no divergence |
| case variation (`TOKEN`, `Token`, `tOkEn`) | no divergence |
| anagrams of every key (rotations, sorted letters, random shuffles) ± separators | no divergence (both reject; Python accepts) |
| NUL at start/middle/end, NUL-only, NUL via `CAST(blob AS TEXT)` | no divergence (both take the shared `instr(char(0))` branch) |
| digits mixed in, repeated letters, extra letters | no divergence |
| empty string, `NULL`, single chars | no divergence |
| very long keys (100k chars), long separator runs, 4 MB-ish repeated strings | no divergence |
| astral / combining / non-mapped non-ASCII (`🙂`, `é`, U+0307) | no divergence |
| exhaustive ≤4-char strings over an adversarial 17-char alphabet (88,741) | no divergence |
| json_tree with nested objects/arrays, integer array index keys, unicode escapes | no divergence |
| candidate generator bug hunt (alias, ordering, stages, empty stage, coercion, correlation) | none found; audit confirms |

## 6. Candidate generator bug inspection (explicit)

* **Alias resolution:** `_bots5_casefold_0..3`, `_bots5_stage_0..2`, `_bots5_normalized`,
  `_bots5_normalized_key`, `_bots5_residual_0..10` — no shadowing; `json_tree` exposes no
  column with these names. The innermost `key` correctly resolves to the correlated
  `json_tree.key` (confirmed behaviorally by §4.3/§4.4).
* **Stage splitting:** 19 folds → 10 + 9, in source order when read inner-to-outer. Folds
  are order-independent (disjoint targets, ASCII-alnum outputs, audited in §4.7).
* **Empty stage:** not reachable with the current map; if the map were empty the code would
  still produce a valid single `SELECT lower(...) AS _bots5_casefold_0` stage.
* **Residual/count pairing:** both loops iterate `sorted(_FORBIDDEN_SECRET_KEYS)` with the
  same index, so `_bots5_residual_i` always belongs to the key in `conditions[i]`.
* **Quoting:** the substitution values are fixed key letters / fold strings (no quotes or
  backslashes), so the single-quoted literals cannot break.
* **Type coercion:** `CAST({column} AS TEXT)` and `lower()` are byte-identical to baseline.
* **NULL:** `EXISTS` over all-NULL conditions yields no row ⇒ false; baseline `NULL OR …`
  is not-true ⇒ same. Verified on the `NULL` row.
* **Correlated subquery cost:** candidate computes the fold chain once per row inside the
  `EXISTS`; the row guard only runs for metadata/provenance writes.

## 7. Pre-existing baseline↔Python divergence (not a candidate defect)

The baseline SQL predicate is a strict *anagram-closure* of `is_forbidden_secret_key`:
e.g. `nekto`/`opssward`/`etselaevcur` are rejected by the trigger but accepted by Python.
Over the whole corpus the mirror found **19,927** such over-rejections and **0** missed
secrets (Python-forbidden inputs the SQL accepts), and the `json_tree` stress found
**95** more. The candidate reproduces every one of them exactly (0 disagreements). The
proposed repair is compatibility-only and does not change guard strength; if the over-
rejection is itself considered a defect it is **out of scope and unchanged**.

## 8. Residual uncertainty — what I could NOT verify

1. **No formal proof of the finite engine, only a structural argument plus exhaustive
   length-≤4 search.** The argument in §2 is complete for the current vocabulary and map,
   but is not machine-checked in a proof assistant.
2. **SQLite version coverage.** Semantics were executed on 3.45.1, 3.46.0 and 3.50.4 only.
   Compile was tested on those three; behaviour was replayed on 3.45.1/3.50.4. SQLite
   versions older than 3.45.1 (e.g. 3.4x earlier) were **not** executed; the candidate's
   parser depth is only 16 and its SQL constructs are ancient, so a failure there is very
   unlikely, but untested.
3. **The full Alembic migration was not re-run end-to-end.** I reconstructed the four
   `_install_phase5_row_guards` trigger *shapes* from the migration source and compiled
   them; I did not execute migration 0007 through Alembic (which would be the campaign's
   replay, not this task's scope). Nothing in the generator is migration-framework
   dependent.
4. **Unicode-data version.** The `unicode_map_scan` completeness result is relative to
   Python 3.12.13's Unicode tables. A future/other Python could add a casefold-to-ASCII
   code point; that would affect baseline and candidate identically (both would inherit
   the gap), so it does not affect candidate≡baseline.
5. **The campaign's separate equivalence corpus was deliberately not used**, so these
   results are independent of it. I also did not evaluate the alternative candidate shapes
   present in `scratch/` (`cand_trigger.sql` etc.); only `scratch/candidate_secrets.py` as
   specified.
6. **No tracked file was modified** and nothing was installed or replaced on the host.

## 9. Verdict

**NOT FALSIFIED.** The candidate is exactly semantically equivalent to the baseline
`secret_key_forbidden_sql_expression` for every input I could devise, including every
attack class named in the task, and it makes all four Phase 5 triggers compile on SQLite
3.45.1 where the baseline does not. No counterexample exists in 97,443 predicate inputs,
6,927 `json_tree` keys, or 1,510 end-to-end trigger documents.

If the repair's acceptance criterion also requires the guard to keep rejecting anagrams and
NUL-containing keys exactly as today, that is satisfied (identical results). If it
additionally requires the SQL guard to match the Python predicate exactly, that criterion
was already violated by the baseline and is **not** addressed by this candidate.

### Reproduce

```bash
cd /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/gen_sql.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/gen_cases.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/compare.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/full_compare.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/json_stress.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/trigger_test.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/version_replay.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/full_trigger_compile.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/audit_candidate.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/unicode_map_scan.py
.venv/bin/python work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/evidence_table.py
```
