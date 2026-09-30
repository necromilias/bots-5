# Implementation falsification — Phase 5 SQLite secret-guard compatibility candidate

**Campaign:** `work/defect-evidence/sqlite-secret-guard-compat-20260930-01/`
**Repo HEAD:** `9762170099889ecd87d451341a15a29ce7aceae8` (branch `main`)
**Reviewer role:** independent implementation falsifier (no tracked file modified)
**Date of run:** 2026-09-30 (session timestamp 2026-09-30)
**Isolation:** every artifact of this review is under
`.../falsifier/impl/`; no tracked file under `src/`, `tests/`, `docs/` was written.

## VERDICT: NOT FALSIFIED

I tried to break the implementation along the eight assigned axes. The sealed
hashes are exact, the tracked diff contains nothing unrelated, all 44 pinned
schema hashes re-derive independently and exactly four triggers changed, the new
predicate is verdict-identical to the pristine pre-repair predicate over 87,732
independently generated adversarial inputs plus 604 JSON documents evaluated
through `json_tree`, the persisted repaired triggers compile and fire correctly
on isolated SQLite **3.45.1** where all four pre-repair triggers fail with
`parser stack overflow`, and the new test file fails pre-repair and passes
post-repair. I found **zero** counterexamples.

Residual uncertainty is listed in §9.

---

## 0. Environment

| Item | Value |
|---|---|
| Python | `.venv314/bin/python` → `/usr/bin/python3.14` (3.14.7) |
| Host sqlite3 (Python stdlib) | `3.53.4` (parses the 98-deep baseline expression) |
| Isolated library | `scratch/libsqlite3-3.45.1.so` via `scratch/ctypes_sqlite.py` |
| Isolated CLIs | `scratch/sqlite3-3450100` = 3.45.1, `scratch/sqlite3-3460000` = 3.46.0 |
| Baseline tree | `work/baseline-t0/src/bots5/core/secrets.py` (35cca… not used; see §1) |

Note: `/dev/shm` is wiped between tool invocations in this harness, so all
persistent scratch/databases were placed under
`falsifier/impl/scratch/`; `--basetemp=/dev/shm/...` was used only inside a
single pytest process, as instructed.

---

## 1. Sealed hashes and tracked diff

Command:

```bash
sha256sum src/bots5/core/secrets.py \
          src/bots5/infrastructure/persistence/sqlite.py \
          tests/test_secret_key_sql_conformance.py
```

Observed — exact match on all three:

```
4948c0cda9d78249a387823f26b59439e5e6ce7d0f7e33ac549ea1d38a1b2836  src/bots5/core/secrets.py
821a0e365bf2d2640751274cb4512114f46aeae704c758c3c0fc5c58ca9a54f8  src/bots5/infrastructure/persistence/sqlite.py
06d23bfaa1dfde33e1ebb8850a2bd547b8e4d5ec21a4f9e47136d9d3626c7f1e  tests/test_secret_key_sql_conformance.py
```

`tests/test_secret_key_sql_conformance.py` is **untracked** (a new file), which is
why `CANDIDATE_TRACKED.patch` lists only the two `src/` files.

Diff review — the working-tree diff is byte-identical to the sealed patch:

```bash
diff <(git diff HEAD -- src/) \
     work/defect-evidence/sqlite-secret-guard-compat-20260930-01/final/CANDIDATE_TRACKED.patch
# -> no output; "PATCH==GITDIFF-EXACT"
git diff HEAD --numstat
# 57  22  src/bots5/core/secrets.py
#  4   4  src/bots5/infrastructure/persistence/sqlite.py
```

The 136-line patch contains exactly two logical changes:

1. `secrets.py`: removes `_ASCII_NON_ALNUM_CODES`, adds
   `_FORBIDDEN_KEY_SQL_ALIAS`/`_CASE_FOLD_STAGE_LIMIT`, and rewrites
   `secret_key_forbidden_sql_expression()` to stage the 19 Unicode folds through
   nested derived tables and to drop separator materialisation.
2. `sqlite.py`: replaces exactly the four `_PHASE5_SCHEMA_SHA256` entries for the
   four triggers that embed that predicate.

No unrelated hunk, no formatting churn, no changed docstring outside the function,
no edit to the migration, and no new migration. **Confirmed clean.**

---

## 2. Independent re-derivation of `_PHASE5_SCHEMA_SHA256`

Script: `falsifier/impl/derive_hashes.py` (builds a fresh DB through
`tests._authority_test_support.upgrade_database`, then
`sha256(_normalise_sql_fragment(sql))` over every `sqlite_master` object).

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python \
  work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/impl/derive_hashes.py
```

Raw output (`impl/derive_hashes.log`):

```
objects in _PHASE5_SCHEMA_OBJECTS: 44
objects found in fresh sqlite_master: 44
entries in _PHASE5_SCHEMA_SHA256: 44
MISMATCH COUNT: 0
declared-but-not-checked: []
checked-but-not-declared: []
--- four repaired trigger hashes (derived) ---
phase5_capability_fact_provenance_validate_insert 831fea2f41765985d349d4dc87bab9bd9d6e440be0b03f914c36703eda70f5f8 match
phase5_capability_fact_provenance_validate_update 5bc95210f6f6d0c6c18231bfd632bfc1cc585b68058e68caa8630c15adbedfb2 match
phase5_model_catalogue_metadata_validate_insert 86686f757da51fd58140d221537cb0f53f75406d7d4062433b126942e5dad683 match
phase5_model_catalogue_metadata_validate_update 87dde170a31c36cd52a493e21ed627d25bdeb16d3d48b667a6aade5ae17c3ccc match
```

To rule out a self-referential hash table (i.e. the DB and the pin were produced
by the same faulty code), I built a **pristine pre-repair database** from
`work/baseline-t0/src` and diffed all 44 objects candidate-vs-baseline
(`impl/baseline_hashes.py`, `impl/baseline_hashes.log`):

```
bots5 source: .../work/baseline-t0/src/bots5/__init__.py
baseline declared entries: 44 derived: 44
baseline's own declared-vs-derived mismatches: {}     # the pre-existing pin is self-consistent
objects whose SQL differs candidate-vs-baseline: 4
  DIFF phase5_capability_fact_provenance_validate_insert
  DIFF phase5_capability_fact_provenance_validate_update
  DIFF phase5_model_catalogue_metadata_validate_insert
  DIFF phase5_model_catalogue_metadata_validate_update
differs as expected: True
total triggers in baseline db: 121
```

**Exactly and only the four expected objects changed; the other 40 are
byte-identical after normalisation.** Cross-checking the migration source
confirms the causal claim: `secret_key_forbidden_sql_expression` is consumed only
at
`migrations/versions/0007_phase5_provider_model_configuration.py:305`, feeding
`safe_json_object` → the two metadata triggers and `safe_capability_provenance`
→ the two provenance triggers. No other trigger embeds it.

Expression size for the record (`key` column / `new.metadata_json` column):

| | baseline | candidate |
|---|---|---|
| bytes (`key`) | 369,679 | 10,839 |
| paren depth (`key`) | **98** | **16** |
| `replace(` calls | 15,296 | 185 |

---

## 3. Semantic equivalence vs the pristine predicate (own corpus)

Script: `falsifier/impl/equivalence.py` loads the baseline module under an
isolated name (`baseline_secrets_implfals`) and compares `WHERE` verdicts on a
freshly generated, non-reused corpus.

Corpus shape (seed `20260930`): separator insertions at multiple positions for
every forbidden key and benign word × all uppercase/title variants; every one of
the 19 Unicode fold code points alone/doubled/repeated/embedded/interleaved with
separators; digits; alternating case; shuffled anagrams; empty/whitespace;
NUL-prefixed/suffixed/embedded; non-ASCII (`é`, CJK, emoji, `İ`/`ı`); very long
strings (100,000 chars; `password-`×10,000; 50,000 separators + `token`;
`ß`×20,000; full ASCII printable ×2,000); and 40,000 seeded random strings over a
hostile alphabet.

```
corpus size: 56340
baseline expr bytes: 369321 candidate expr bytes: 10835
baseline true count: 14280 candidate true count: 14280
PREDICATE: identical verdicts on all 56340 corpus values
json documents: 604
json_tree matched: baseline 510 candidate 510
JSON_TREE: identical key verdicts
python-forbidden missed by candidate SQL (sample): 0
FAILURES: none
```

The `json_tree` comparison replicates the real trigger shape:
`SELECT d.id, jt.fullkey FROM docs d, json_tree(d.j) jt WHERE jt.key IS NOT NULL
AND (<predicate on jt.key>)`, over nested objects/arrays (depths to 3, keys at
multiple `fullkey` levels), with both predicates for column `jt.key`.

An **exhaustive separator-position sweep** (`impl/sep_sweep.py`) additionally
inserts every one of the 66 ASCII non-alphanumeric code points
(`[c for c in range(128) if not chr(c).isalnum()]`) at **every position** of every
forbidden key (single and doubled, plus trailing), every fold code point at every
position, and every separator at every position of nine benign controls:

```
sweep values: 31392
baseline true: 23355 candidate true: 23355
only-baseline: 0
only-candidate: 0
python-forbidden missed: 0
```

Total distinct adversarial inputs compared: **87,732** (56,340 + 31,392) plus 604
JSON documents / 510 matched `json_tree` keys. **No divergence of any kind.**

---

## 4. Implementation-bug audit

Script: `falsifier/impl/implementation_audit.py` (`impl/implementation_audit.log`).

* **Persisted trigger markers and messages survive.** For all four repaired
  triggers, every declared `_PHASE5_TRIGGER_MARKERS` substring is present in the
  stored `sqlite_master.sql`, including `json_tree(new.metadata_json)` and
  `json_tree(new.provenance_json)`, the `before insert/update of …` clause, and
  the exact `RAISE(ABORT, 'Phase 5 … is malformed')` text:

  ```
  phase5_model_catalogue_metadata_validate_insert  len=11222 markers=True raise="Phase 5 model catalogue metadata is malformed"
  phase5_model_catalogue_metadata_validate_update  len=11239 markers=True raise="Phase 5 model catalogue metadata is malformed"
  phase5_capability_fact_provenance_validate_insert len=12112 markers=True raise="Phase 5 capability provenance is malformed"
  phase5_capability_fact_provenance_validate_update len=12131 markers=True raise="Phase 5 capability provenance is malformed"
  ```

  Note `_PHASE5_TRIGGER_MARKERS` for these entries does **not** require the abort
  message (only some other triggers do), but the message is intact anyway.

* **`_validate_phase5_trigger_behavior` still passes.** A fresh candidate database
  opened through the authoritative path invokes both validators and both
  complete without raising:
  `authoritative open invoked validators: {'schema': 1, 'behavior': 1}` — this
  includes the four live probe statements that must abort on
  `'{"TOKEN":"probe"}'` metadata and provenance.

* **`_ASCII_NON_ALNUM_CODES` is fully unreferenced in the candidate.**
  `hasattr(candidate_module, '_ASCII_NON_ALNUM_CODES') == False`; a repo-wide grep
  finds it only inside `work/baseline-t0/` and the pre-existing design-falsifier
  scratch copies — never in `src/` or `tests/`.

* **Alias resolution / stage splitting.** Expression shape:
  `bytes=10867, nesting_depth=16, replace_calls=185, alias=_bots5_normalized_key,
  stage_limit=10`. 19 folds split as 10 + 9 into two derived stages
  (`_bots5_casefold_stage_0/1/2`), plus a third derived table for the residuals.

* **Empty-stage / stage-limit robustness.** Monkeypatching
  `_CASE_FOLD_STAGE_LIMIT` to every value `{1,2,3,5,9,10,18,19,20,25}` (19 stages
  down to 1 stage, including the configured 2-stage case) produced **no SQL
  errors and no missed forbidden key** for any setting. There is no configuration
  of the current 19-entry map that yields an empty stage; even an empty map would
  fall back to `_bots5_casefold_0` without a dangling alias.

* **Correlated-subquery correctness.** A per-row trigger probe on a table with
  `('benign','token','apricotKey','password','key','secret')` yielded
  `accepted / rejected / accepted / rejected / accepted / rejected` — i.e. the
  correlated derived table is re-evaluated against each `NEW` row and does not
  leak or cache a previous verdict. (`'key'` is benign: `key` is **not** in the
  forbidden vocabulary; my initial expectation of rejection was wrong and was
  corrected.)

* **Alias-collision probe.** Generating the predicate for a column literally named
  `_bots5_casefold_0` still returns only `['token']` from
  `('token','benign')`, so the internal alias cannot be shadowed by a hostile
  column name.

* **Paren/parse balance** was separately validated by compiling (see §5), which is
  the real proof.

---

## 5. Isolated SQLite 3.45.1 compile and runtime

I did **not** reconstruct the trigger shapes; I extracted the **verbatim
persisted trigger SQL** from the freshly migrated candidate and pristine baseline
databases and executed `CREATE TRIGGER` on the isolated library/CLIs. This tests
the real artifact that the migration writes.

Script: `falsifier/impl/compile_triggers.py`
(`impl/compile_triggers_result.json`). Database sizes prove the difference
immediately: candidate trigger SQL 11.2–12.1 KB vs baseline 370.0–371.0 KB.

```
candidate 3.45.1 failures: []
baseline  3.45.1 unexpectedly-compiled: []
DISCRIMINATION OK
```

Raw results on `libsqlite3-3.45.1.so` (library version string `3.45.1`):

| trigger | candidate rc | baseline rc / error |
|---|---|---|
| `phase5_capability_fact_provenance_validate_insert` | 0 (ok) | 1 `parser stack overflow` |
| `phase5_capability_fact_provenance_validate_update` | 0 (ok) | 1 `parser stack overflow` |
| `phase5_model_catalogue_metadata_validate_insert` | 0 (ok) | 1 `parser stack overflow` |
| `phase5_model_catalogue_metadata_validate_update` | 0 (ok) | 1 `parser stack overflow` |

Runtime behavior on 3.45.1 (`impl/behavior_345.py`, `impl/behavior_345.log`;
rc 19 = `SQLITE_CONSTRAINT`, message = the trigger's `RAISE(ABORT,…)`):

| case | result |
|---|---|
| metadata INSERT `{"apricotKey":1}` | accepted |
| metadata INSERT `{"nested":{"token":1}}` | **aborted** |
| metadata INSERT `{"to-ken":1}` | **aborted** |
| metadata INSERT `{"api⟨KELVIN⟩ey":1}` (folds to `apikey`) | **aborted** |
| metadata INSERT key containing NUL (`'{"a' \|\| char(0) \|\| 'b":1}'`) | **aborted** |
| metadata INSERT `{"apißkey":1}` (normalises to `apisskey`, benign) | accepted |
| metadata UPDATE benign / `{"PASSWORD":2}` | accepted / **aborted** |
| provenance INSERT legal / `{"secret":1}` / illegal field | accepted / **aborted** / **aborted** |
| provenance UPDATE benign / `{"TOKEN":1}` | accepted / **aborted** |

All abort messages were the exact persisted strings. `BEHAVIOR_345 OK`.

Closing checks (`impl/closing_checks.py`): the same persisted candidate trigger
set also compiles under the isolated **3.46.0** CLI (`sqlite3-3460000`, rc 0), and
the host migration path already compiles on Python's 3.53.4. So the candidate is
valid across 3.45.1 / 3.46.0 / 3.53.4.

---

## 6. Test-file discrimination

Command shape exactly as assigned (candidate first, then the pristine tree first
on `PYTHONPATH`; `tests/` is taken from the candidate checkout so the same test
file runs against both `bots5` trees):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:.              .venv314/bin/python -m pytest \
  -o addopts= -p no:cacheprovider -q tests/test_secret_key_sql_conformance.py --basetemp=/dev/shm/falscand
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=work/baseline-t0/src:. .venv314/bin/python -m pytest \
  -o addopts= -p no:cacheprovider -q tests/test_secret_key_sql_conformance.py --basetemp=/dev/shm/falsbase
```

* **Candidate:** `7 passed, 1 warning in 1.03s` (exit 0).
* **Pristine pre-repair:** `1 failed, 6 passed` (exit 1). The failing test is
  exactly the intended regression guard:
  `test_generated_predicate_stays_within_the_fixed_parser_stack_budget` →
  `assert 98 <= 24`.

The test file therefore **discriminates**: it fails on the pre-repair tree and
passes on the candidate. (The pre-repair migration tests pass on the host 3.53.4
because that SQLite has the dynamic parser stack; the 3.45.1 compile failure is
covered by §5.)

---

## 7. Other consumers and authoritative open

Repo-wide grep for the changed symbols finds:

* `secret_key_forbidden_sql_expression` — defined once, imported/used only at
  `migrations/versions/0007_phase5_provider_model_configuration.py:8,305`,
  plus the conformance test. No other consumer.
* `_ASCII_NON_ALNUM_CODES` — no candidate `src`/`tests` reference.
* `_PHASE5_SCHEMA_SHA256` — read only by `_validate_phase5_schema`
  (`sqlite.py:1433`). No external pin duplicates it.
* `_UNICODE_CASEFOLD_ASCII_MAP` — unchanged and still consumed by both the
  function and the test.

A freshly migrated database opens through the authoritative path
(`SQLiteAppStateStore.open` → `_validate_phase5_schema` +
`_validate_phase5_trigger_behavior`) with no error (§4). As a bonus regression
check, the Phase 5 / Phase 7 suites pass on the candidate:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python -m pytest \
  -o addopts= -p no:cacheprovider -q tests/test_phase5_provider_model.py \
  tests/test_phase7_migration_authority_faults.py --basetemp=/dev/shm/falsreg
# 90 passed, 1 warning in 55.18s
```

Finally, the intentionally-unsupported case behaves as Mick specified: opening a
database written by the **defective pre-repair release** under the candidate
raises

```
RuntimeError: current Phase 5 schema trigger is not migration-authoritative:
phase5_model_catalogue_metadata_validate_insert
```

i.e. no silent repair and no new migration — the defective DB is rejected and
must be recreated. This is the intended policy, confirmed.

---

## 8. Git cleanliness

```bash
sha256sum src/bots5/core/secrets.py src/bots5/infrastructure/persistence/sqlite.py \
          tests/test_secret_key_sql_conformance.py   # all three match the seal
git status --porcelain --untracked-files=no          #  M src/... (x2) only  -> the sealed candidate
git diff --check                                     # no output, exit 0
git diff --numstat -- src tests docs                 # 57/22 and 4/4, i.e. the seal; docs/tests tracked: none
git rev-parse HEAD                                   # 9762170099889ecd87d451341a15a29ce7aceae8
```

The only tracked modifications are the two sealed `src/` files (whose hashes match
the seal byte-for-byte). The new test file is untracked by design. **I changed
nothing tracked.** All of my artifacts are new files under
`.../falsifier/impl/` (plus its `scratch/` databases).

---

## 9. Could not verify / residual uncertainty

1. **Full repository test suite not run.** I ran the conformance file (both
   trees), `test_phase5_provider_model.py`, and
   `test_phase7_migration_authority_faults.py` (90 passed). I did not execute the
   entire suite, so an unrelated regression in an untested area is not formally
   excluded. The change surface is small and only migration 0007 consumes the
   function, so the risk is low but nonzero.
2. **No 3.46.0 shared library.** 3.46.0 was exercised only through the isolated
   CLI (`sqlite3-3460000`), not through the ctypes library path. 3.45.1 and
   3.53.4 bracket it, and 3.46.0 compiled fine.
3. **Migration not re-run end-to-end under 3.45.1.** I compiled the *persisted*
   trigger SQL verbatim on 3.45.1 rather than running Alembic/SQLAlchemy against
   the ctypes library. This is a stronger test of the object that failed
   (`CREATE TRIGGER`) but does not cover SQLAlchemy's own SQL emission under that
   driver; the pre-existing design falsification covered replay, and the host
   migration produced byte-identical SQL.
4. **3.45.1 `char(0)` semantics.** The embedded-NUL path aborts on 3.45.1 as
   required, but I did not independently characterise whether `char(0)` is the
   empty string on every SQLite version. This behaviour is identical in baseline
   and candidate (`instr(CAST(col AS TEXT), char(0)) > 0` is unchanged), so it is
   not a repair-introduced risk.
5. **Anagram over-rejection is pre-existing.** The predicate still flags anagrams
   of forbidden keys (e.g. `tekno` ≈ `token`) because it tests per-letter counts
   rather than order. I confirmed this is *identical* in baseline and candidate
   and is documented by the test file as an accepted over-rejection; I did not
   try to judge whether that contract is desirable.

---

## Appendix — exact commands

All commands run from
`/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1`. `D` denotes
`work/defect-evidence/sqlite-secret-guard-compat-20260930-01` below.

```bash
# hashes + diff
sha256sum src/bots5/core/secrets.py src/bots5/infrastructure/persistence/sqlite.py tests/test_secret_key_sql_conformance.py
diff <(git diff HEAD -- src/) $D/final/CANDIDATE_TRACKED.patch

# 44-hash re-derivation (candidate) and pre-repair diff
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python $D/falsifier/impl/derive_hashes.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=work/baseline-t0/src:. .venv314/bin/python $D/falsifier/impl/baseline_hashes.py

# equivalence + exhaustive sweep
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python $D/falsifier/impl/equivalence.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python $D/falsifier/impl/sep_sweep.py

# implementation audit (markers, aliases, stages, correlation)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python $D/falsifier/impl/implementation_audit.py

# isolated 3.45.1 compile + behavior, 3.46 CLI, defective-DB rejection
PYTHONDONTWRITEBYTECODE=1 .venv314/bin/python $D/falsifier/impl/compile_triggers.py
PYTHONDONTWRITEBYTECODE=1 .venv314/bin/python $D/falsifier/impl/behavior_345.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python $D/falsifier/impl/closing_checks.py

# test discrimination
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python -m pytest -o addopts= -p no:cacheprovider -q tests/test_secret_key_sql_conformance.py --basetemp=/dev/shm/falscand
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=work/baseline-t0/src:. .venv314/bin/python -m pytest -o addopts= -p no:cacheprovider -q tests/test_secret_key_sql_conformance.py --basetemp=/dev/shm/falsbase

# regression + git cleanliness
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python -m pytest -o addopts= -p no:cacheprovider -q tests/test_phase5_provider_model.py tests/test_phase7_migration_authority_faults.py --basetemp=/dev/shm/falsreg
git status --porcelain --untracked-files=no; git diff --check
```

Artifacts: `impl/derive_hashes.py|.log`, `impl/baseline_hashes.py|.log`,
`impl/equivalence.py|.log|equivalence_result.json`,
`impl/sep_sweep.py|.log|sep_sweep_result.json`,
`impl/implementation_audit.py|.log|.json`,
`impl/compile_triggers.py|.json`,
`impl/behavior_345.py|.log|.json`,
`impl/closing_checks.py|.json`,
`impl/pytest_candidate.log`, `impl/pytest_baseline.log`, `impl/pytest_regression.log`.
