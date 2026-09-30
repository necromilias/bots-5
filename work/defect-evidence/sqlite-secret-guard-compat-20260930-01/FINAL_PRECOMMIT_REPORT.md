# Final pre-commit report — Phase 5 secret-guard SQLite compatibility defect

Campaign: **B.O.T.S. 5 — BOUNDED SQLITE COMPATIBILITY DEFECT CAMPAIGN** (supervisor
monolith, 2026-09-30; human authority: Mick).
Evidence root: `work/defect-evidence/sqlite-secret-guard-compat-20260930-01/`.
Status: candidate sealed, all validation complete, **stopped at the pre-commit boundary**.
Nothing is staged, committed, pushed, tagged, or merged.

---

## 1. Campaign identity and authority

- Objective: investigate a reported SQLite compatibility defect in the Phase 5 raw-DML
  secret-material guards and repair it **if and only if** that is mechanically possible
  without changing accepted product semantics.
- Governing rule: if the defect is fixable inside the accepted semantic fence, fix it; if
  the fence itself is the problem, stop for Mick.
- Explicitly out of scope: Phase 11, Phase 12, documentation reconciliation, general
  persistence cleanup, refactoring `sqlite.py`, changing Linux support policy.
- Two human decisions were taken during the campaign; both are recorded in §12.

## 2. Baseline identity (verified, unchanged)

| Item | Value |
|---|---|
| Repository | `necromilias/bots-5` |
| Branch | `main` |
| HEAD | `9762170099889ecd87d451341a15a29ce7aceae8` |
| Subject | "Phase 10: native desktop surface over the headless campaign engine" |
| `git rev-parse HEAD` = `main` = `origin/main` | yes |
| Staging area at start and end | empty |
| Tracked files modified before this campaign | none |

## 3. Reported defect, claim by claim

| Reported claim | Measured | Verdict |
|---|---|---|
| `secret_key_forbidden_sql_expression()` generates ~370 KB | 369,679 characters | confirmed |
| 15,296 `replace()` calls | 15,296 | confirmed |
| parenthesis depth ~98 | 98 | confirmed |
| embedded in ~4 triggers, ~1.5 MB of trigger SQL | 4 triggers, 1,482,120 bytes total | confirmed |
| fails on SQLite 3.45.1 (stock Ubuntu 24.04) | `parser stack overflow` while preparing `CREATE TRIGGER` | confirmed |
| succeeds on SQLite 3.51.1 | succeeds on 3.46.0, 3.50.4, 3.53.4 | confirmed; boundary is exactly 3.46.0 |
| no declared minimum SQLite version | none in `pyproject.toml`, `README.md`, `docs/` | confirmed |

## 4. Root cause

SQLite before 3.46.0 compiled its LEMON parser with a **fixed** stack:
`#define YYSTACKDEPTH 100` and no `YYDYNSTACK`. SQLite 3.46.0 added
`#define YYDYNSTACK 1` / `YYGROWABLESTACK 1`, making the stack grow on the heap. The
baseline predicate nests 98 parentheses, which overflows the fixed 100-entry stack once the
surrounding trigger wrapper is added. A separate limit, `SQLITE_MAX_EXPR_DEPTH` (1000),
governs long `AND`/`OR` chains and was not the binding constraint here.

This is a **parser/version limitation, not a semantics defect**: the predicate's logic was
correct, its expression nesting was too deep for SQLite <= 3.45.x.

## 5. Reproduction

- Isolated SQLite builds made locally under the campaign scratch path from official
  amalgamations (3.45.1 and 3.46.0); nothing on the host was installed, replaced or
  mutated.
- Replaying the migration's own captured DDL through an isolated 3.45.1 library, the only
  parser failures are the four Phase 5 triggers; 3.46.0 accepts all of them.
- `work/defect-evidence/sqlite-secret-guard-compat-20260930-01/compat_proof.py` reproduces
  it on the real trigger text: pre-repair 2/6 statements created (the two tables) and all
  four `CREATE TRIGGER` statements fail with `parser stack overflow`; repaired 6/6 succeed.

## 6. Repair (Option A — smaller, shallower, equivalent SQL)

Confined to the string returned by `secret_key_forbidden_sql_expression`:

1. **Separator removal is no longer materialised.** Every forbidden key is a run of ASCII
   letters, so deleting an ASCII non-alphanumeric character can change neither a key's
   per-letter occurrence counts nor whether an ASCII alphanumeric character survives the
   residual `NOT GLOB '*[A-Za-z0-9]*'` test. The 66 `replace()` calls that removed
   `_ASCII_NON_ALNUM_CODES` are gone, and the now-unused constant was removed with them.
2. **The 19 Unicode case-fold replacements are applied in stages**, each stage in its own
   nested derived table, and the 11 per-key residuals are computed in a later projection.
   No single expression nests deeply.

Result: the expression drops from 369,679 characters / 15,296 `replace()` calls / depth 98
to 10,839 characters / 185 `replace()` calls / depth 16; the largest statement in the whole
migration chain drops from 370,985 bytes to 12,605 bytes.

Rejected alternatives: Python UDF (forbidden by the raw-DML authority contract); declaring
a minimum SQLite version (forbidden platform policy); flat all-alphanumeric count sum
(equivalent but ~160 KB per trigger and near the expression-depth limit); GLOB/LIKE patterns
(cannot express order-insensitive multiset equality); recursive CTE character walk
(order-sensitive, would change accepted semantics); editing historical migration 0007
(unnecessary — the generator is imported at replay time — and prohibited).

## 7. Semantic equivalence to the pre-repair predicate

**Scope of the claim.** The candidate preserves the *existing raw-DML enforcement semantics
exactly* and introduces **no false-negative regression**. It is **not** claimed to be exactly
equivalent to the Python predicate `is_forbidden_secret_key()`; it was not exactly equivalent
before the repair either (see the pre-existing divergence below and
`EVIDENCE_CLARIFICATION_CORPUS_360.md`).

- **Structural argument.** The forbidden vocabulary is pure ASCII letters; separator
  characters are non-alphanumeric ASCII. Deleting them cannot change the counts of any key
  letter, nor whether a residual ASCII alphanumeric character remains. The `IN (forbidden)`
  clause is retained and is subsumed by the residual variant.
- **Executed corpus.** `equivalence_corpus.py` compares the pristine pre-repair predicate
  (extracted with `git show HEAD:src/bots5/core/secrets.py`), the repaired predicate and the
  Python predicate over 13,443 deterministic values:
  - **baseline SQL vs repaired SQL: 0 disagreements** (the equivalence claim);
  - **repaired SQL vs Python: 360 disagreements; baseline SQL vs Python: 360** — the same
    360 inputs in both cases;
  - direction: **0 SQL false negatives** (no Python-forbidden key is allowed by SQL) and
    **360 SQL false positives** (Python-allowed keys the SQL guard rejects) — 305 keys
    containing an embedded NUL and 55 anagrams of forbidden keys.

  Raw output: `EQUIVALENCE_CORPUS_OUTPUT.txt`; full breakdown and adjudication:
  `EVIDENCE_CLARIFICATION_CORPUS_360.md`.
- **Pre-existing divergence, preserved exactly.** The 360 SQL-vs-Python differences are a
  property of the *baseline* predicate, unchanged by this repair. The embedded-NUL rejection
  is deliberate, fail-closed and asserted by the existing test
  `test_raw_phase5_metadata_and_current_declared_types_are_authoritative`. The anagram
  over-rejection is **genuine pre-existing semantic debt**, accepted as out of scope for a
  compatibility repair and recorded for a future campaign in §14. Neither is introduced,
  widened or narrowed here.
- **Independent adversarial falsification** (separate model instance, its own generators,
  campaign corpus deliberately not reused): 97,443 inputs, 6,927 real `json_tree` keys from
  1,507 nested documents, exhaustive enumeration of all length-<=4 strings over an
  adversarial 17-character alphabet, all 19 mapped code points, separators inserted into
  every key — **0 divergences from the pre-repair predicate**. Report:
  `falsifier/DESIGN_FALSIFICATION.md`.
- **Preserved accepted behaviours**: fail-closed on any embedded NUL; order-insensitive
  rejection of forbidden-key anagrams; the `(0x01F0,"j")` Unicode mapping; benign keys such
  as `apricotKey` still accepted.

**Closure wording.** The candidate is semantically equivalent to the pre-repair SQL predicate
(0 disagreements over the corpus and both falsifiers' larger corpora) and preserves the
existing raw-DML enforcement semantics exactly, with zero false-negative regression.

## 8. Compatibility proof

`compat_proof.py`, run against isolated SQLite 3.45.1 and the current runtime:

| Trigger set | 3.45.1 `CREATE TRIGGER` | 3.45.1 DML | current DML |
|---|---|---|---|
| pre-repair | 0/4 (all `parser stack overflow`) | n/a | n/a |
| repaired | 4/4 | 74 rejections, identical to current | 74 rejections |

No forbidden key in the corpus is missed on 3.45.1, and the 3.45.1 and current-runtime
verdict sets are identical (symmetric difference empty). The four extra rejections relative
to Python are the accepted quirks (NUL fail-closed and anagram closure).

## 9. Pinned-schema consequence (this is why `sqlite.py` was touched)

`_PHASE5_SCHEMA_SHA256` in `src/bots5/infrastructure/persistence/sqlite.py` pins the SHA-256
of the normalised `sqlite_master.sql` of every Phase 5 object, and `_validate_phase5_schema`
runs on every authoritative open. Because the four trigger texts change, exactly four of the
44 entries had to change; nothing else moved (measured). Without that update even a freshly
migrated database is rejected with `current Phase 5 schema trigger is not migration-authoritative`.

## 10. Test strategy and results

| Case | Requirement | Result |
|---|---|---|
| A | Python↔SQL conformance corpus | `tests/test_secret_key_sql_conformance.py` — never misses a forbidden key; over-rejects only in the two accepted classes; benign vocabulary accepted |
| B | raw-DML proof without UDFs | bare `sqlite3.connect(":memory:")`, no UDFs; migrated database rejects every forbidden-key variant and accepts benign nested metadata |
| C | old-runtime proof | `compat_proof.py` on isolated SQLite 3.45.1 (§8) |
| D | current-runtime proof | full suite on Python 3.14.7 / SQLite 3.53.4 |
| E | fresh database | `upgrade_database` on a new path |
| F | upgrade database | built a database at `0006_phase4_workspace`, upgraded it to `0012_phase9_archive_import`: the Phase 5 triggers are re-created from the repaired generator (11,222 / 12,131 bytes, not 370 KB) and `_validate_phase5_schema` passes |
| G | existing/persisted-trigger database | measured: not repairable on <= 3.45.x by any SQL or pragma path; decision in §12 |
| H | startup trigger validation | `SQLiteAppStateStore.open` → `_validate_open_connection` → `_validate_phase5_schema` + `_validate_phase5_trigger_behavior` all pass |
| I | regression discrimination | `test_generated_predicate_stays_within_the_fixed_parser_stack_budget` fails against the pre-repair tree (depth 98) and passes against the repaired tree (depth 16) |
| T4 | full suite, once | §14 |

## 11. Candidate seal

| File | SHA-256 |
|---|---|
| `src/bots5/core/secrets.py` | `4948c0cda9d78249a387823f26b59439e5e6ce7d0f7e33ac549ea1d38a1b2836` |
| `src/bots5/infrastructure/persistence/sqlite.py` | `821a0e365bf2d2640751274cb4512114f46aeae704c758c3c0fc5c58ca9a54f8` |
| `tests/test_secret_key_sql_conformance.py` | `06d23bfaa1dfde33e1ebb8850a2bd547b8e4d5ec21a4f9e47136d9d3626c7f1e` |

Artifacts: `final/CANDIDATE_TRACKED.patch`, `final/CANDIDATE_DIFFSTAT.txt`,
`final/CANDIDATE_FILE_SHA256SUMS.txt`, `final/TRACKED_STATUS.txt`,
`final/DIFF_CHECK.txt` (empty = clean).

## 12. Human decisions

**First decision (superseded).** Asked whether existing databases should be migrated.
Mick selected "generator repair only" on the basis of my statement that pre-repair databases
would remain usable on SQLite >= 3.46.0.

**Correction.** That statement was wrong. `_PHASE5_SCHEMA_SHA256` pins the exact persisted
trigger text, so completing the repair necessarily makes the application reject every
pre-repair database on **every** SQLite version with
`current Phase 5 schema trigger is not migration-authoritative`. Measured acceptance matrix
on real databases: pre-repair table + pre-repair DB accepted; repaired table + repaired DB
accepted; **repaired table + pre-repair DB rejected**. There is no Phase 5 schema-repair
path in the codebase.

**Second decision (governing).** Re-consulted with the corrected matrix. Mick selected
**"Generator fix only; existing DBs must be recreated."** Therefore: no forward migration
`0013`, no `migration_runner.py` change, and pre-repair databases are deliberately rejected
and must be recreated. This is an accepted, explicitly authorised limitation, recorded here
rather than worked around.

## 13. Prohibited-action compliance

No stage, commit, push, amend, merge, rebase, tag, ref or remote mutation; no OrgMem
mutation; no Phase 11/12 work; no paperwork reconciliation; no system/global package
installation; no host SQLite replacement; no weakening of the security predicate; no imposed
minimum SQLite version; no `pyproject.toml` support-policy change; no new dependency; no CI
infrastructure created; no change to the C VFS, DataRootAuthority, restore/backup, migration
files or `migration_runner.py`; no `git clean`; no unrelated untracked material deleted,
moved or staged; no opportunistic repair of unrelated findings.

## 14. Results, limitations and follow-up

- **T4 full suite** (run once, on the sealed candidate, after every other check):
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv314/bin/python -m pytest -o addopts= -p no:cacheprovider -q --basetemp=/dev/shm/bots5-secretguard-t4`
  → **exit 0; 1511 passed, 1 skipped in 979.58s**. Collection is complete (1512 collected =
  1511 passed + 1 skipped); the skip is the pre-existing opt-in local-provider test, and no
  provider was contacted. Raw log: `final/T4_FULL_SUITE.log`. The seal was re-verified
  afterwards: all three hashes `OK`, HEAD still `9762170099889ecd87d451341a15a29ce7aceae8`,
  `git diff --check` clean, only the two `src/` files modified and nothing staged.
- **Independent implementation falsification**: **NOT FALSIFIED**
  (`falsifier/IMPLEMENTATION_FALSIFICATION.md`). Independent re-derivation of all 44
  `_PHASE5_SCHEMA_SHA256` entries matched a freshly migrated database (0 mismatches), and a
  candidate-vs-baseline diff of all 44 objects showed exactly and only the four expected
  triggers changed. 87,732 independently generated adversarial inputs plus 510 real
  `json_tree` keys produced zero divergences from the pristine pre-repair predicate. The
  verbatim persisted candidate triggers compile on isolated SQLite 3.45.1 while the
  pre-repair ones fail with `parser stack overflow`. Test discrimination reconfirmed
  (candidate 7 passed; pre-repair 1 failed / 6 passed). The falsifier changed nothing tracked
  and re-verified the seal hashes at the end.
- **Limitation (authorised)**: databases written by the defective release carry the oversized
  triggers, are rejected by the repaired application, and must be recreated. On SQLite
  <= 3.45.x they cannot be opened at all, and no SQL or `PRAGMA writable_schema` path can
  repair them there. Governing policy (Mick): no migration `0013`, no dual acceptance of
  old/new schema hashes, pre-repair development databases are disposable and must be
  recreated.
- **Recorded future semantic/policy question — forbidden-key anagram over-rejection.**
  The SQL guard tests per-letter counts plus a residual check, so it rejects any ordering of
  a forbidden key's letters (e.g. `apieky`, `nekto`) while the Python predicate
  `is_forbidden_secret_key()` requires exact normalized equality. This is **genuine
  pre-existing semantic debt**: it is a fail-closed false positive, it is identical before and
  after the repair, and it is **out of scope for this compatibility repair**. It is recorded
  here for a future, separately authorised semantic/policy campaign. Do not change it as part
  of this defect. `EVIDENCE_CLARIFICATION_CORPUS_360.md` carries the full analysis, including
  the 305 embedded-NUL over-rejections, which are deliberate/test-asserted behaviour rather
  than debt.
- **Follow-up recommendation (not implemented here)**: the project declares no minimum
  SQLite version and has no CI infrastructure; a platform-support statement plus a
  version-matrix check would prevent a recurrence. Creating that infrastructure is outside
  this campaign's fence and was not done — no minimum-SQLite policy and no CI change was
  performed.
- **Human adjudication (Mick, 2026-09-30)**: the sealed candidate is substantively accepted;
  the SQL-vs-Python divergence is accepted as pre-existing behaviour preserved exactly; no
  candidate-changing work is authorised; staging and commit are authorised, push is not.
- **Model-usage ledger**: 2 of the 12 permitted child launches were used (1 design
  falsifier, 1 implementation falsifier). No worker spawned workers.
