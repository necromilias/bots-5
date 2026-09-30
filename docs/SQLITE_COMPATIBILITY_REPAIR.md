# SQLite ≤ 3.45.1 parser-compatibility repair

Subject commit: `dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f` —
*Phase 5: make the secret-key SQL guard parser-safe on SQLite <= 3.45.1* (2026-09-30).

This record documents a compatibility repair only. It does **not** establish a minimum supported
SQLite version, an Ubuntu support guarantee, a release-blocking platform, or a fleet support policy.

## The defect

The Phase 5 secret-key raw-DML guard generated a forbidden-key predicate that nested 98 parentheses
and issued 15,296 `replace()` calls, producing a 369,679-character expression. Embedded in the four
migration `0007` triggers, that expression overflowed the fixed one-hundred-entry LEMON parser stack
used by SQLite before 3.46.0 (which had no dynamic stack). Stock Ubuntu 24.04 ships SQLite 3.45.1, so
`CREATE TRIGGER` failed with `parser stack overflow` and the schema could not be created at all on
that runtime.

## The repair

The repair keeps the predicate SQLite-only (no Python user-defined function) and is
semantics-preserving:

- separator removal is no longer materialised. Every forbidden key is a run of ASCII letters, so
  deleting a non-alphanumeric ASCII character can change neither a key's per-letter occurrence counts
  nor whether an ASCII alphanumeric character survives the residual
  `NOT GLOB '*[A-Za-z0-9]*'` test;
- the 19 Unicode case-fold replacements are applied in stages, each stage in its own nested derived
  table, with the per-key residuals computed in a later projection, so no single expression nests
  deeply.

Expression shape: 369,679 characters / 15,296 `replace()` / depth 98 → **10,839 / 185 / depth 16**.
Largest statement in the migration chain: 370,985 → 12,605 bytes.

## Result

- All four triggers now create on SQLite 3.45.1 (**0/4 before, 4/4 after**), and raw-DML behaviour on
  3.45.1 is identical to the current runtime.
- Because the four persisted trigger texts change, exactly **4 of the 44** pinned
  `_PHASE5_SCHEMA_SHA256` entries were updated.

## Equivalence evidence

- Baseline SQL versus repaired SQL shows **0 disagreements** over a 13,443-value corpus and both
  independent falsifiers' larger corpora, with **zero SQL false negatives**.
- The 360 pre-existing SQL-versus-Python over-rejections (305 embedded-NUL fail-closed, 55
  forbidden-key anagrams) are preserved exactly. The anagram over-rejection is recorded as pre-existing
  semantic debt and is out of scope for this compatibility repair.
- Execution coverage was bounded: direct execution evidence covered SQLite 3.45.1, 3.46.0 and 3.50.4,
  with behaviour replayed on 3.45.1 and 3.50.4. Releases older than 3.45.1 were not executed, so the
  structural pre-3.46.0 parser-stack argument is not a claim of exhaustive version testing, and none of
  this establishes a formal minimum SQLite version or support policy.
- **This candidate is not claimed to be exactly equivalent to the Python
  `is_forbidden_secret_key()` predicate.**

## Policy decisions recorded with the repair

- No migration `0013` was added, and old and new schema hashes are not both accepted.
- Databases written by the defective release are disposable and must be recreated.
- No minimum-SQLite support policy and no CI change was made by this commit.

## Validation

The repair's own full-suite T4 recorded **1511 passed, 1 skipped, exit 0**.

## Explicit non-claims

This repair does **not**:

- declare a formal minimum SQLite version;
- guarantee Ubuntu support;
- make Ubuntu or any LTS an Ubuntu release gate;
- establish a fleet support policy.

SQLite 3.45.1 compatibility was repaired and demonstrated; that is the whole claim. The Ubuntu 24.04 /
SQLite 3.45.1 compatibility lane in CI v1 (`docs/CI_V1.md`) exercises this repair. Its status differs
by gate: relative to the aggregate T4 adjudication it is non-gating — it is excluded from
`aggregate.needs`, its evidence is not consumed by the aggregate reconciler, and a lane failure cannot
change `T4_PASS` / `T4_FAIL` — while it remains an ordinary GitHub Actions job with no job-level
`continue-on-error`, so a lane failure currently makes the overall workflow run conclude `failure`.
The lane provides compatibility evidence only and is not a support-policy declaration: it does not
establish Ubuntu 24.04, Ubuntu LTS, or SQLite 3.45.x as a formal supported platform, minimum version,
fleet policy, or release gate.

## Evidence

The committed campaign evidence is under
`work/defect-evidence/sqlite-secret-guard-compat-20260930-01/`, including the design, decision
request, reproduction, equivalence corpus output, falsification records, and final pre-commit report.
