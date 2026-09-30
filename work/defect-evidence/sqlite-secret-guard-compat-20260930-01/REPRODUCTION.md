# Reproduction and root cause — Phase 5 secret-guard SQLite compatibility defect

Campaign: `work/defect-evidence/sqlite-secret-guard-compat-20260930-01/`
Baseline HEAD: `9762170099889ecd87d451341a15a29ce7aceae8` ("Phase 10: native desktop surface
over the headless campaign engine"), `main` == `origin/main`, staging empty, no tracked
modifications.

## 1. Reported claim vs independently measured facts

| Claim | Measured | Verdict |
|---|---|---|
| ~370 KB per expression | 369,679 characters / 369,679 bytes for `key` | CONFIRMED |
| 15,296 `replace()` calls | 15,296 | CONFIRMED |
| parenthesis depth ~98 | max paren depth 98 (max nested function-call depth 84) | CONFIRMED |
| repeated into ~4 trigger contexts | exactly 4 triggers | CONFIRMED |
| ~1.5 MB trigger SQL per database | 370,076 + 370,093 + 370,966 + 370,985 = 1,482,120 bytes | CONFIRMED |
| fails on SQLite 3.45.1 | `Parse error ... parser stack overflow` | CONFIRMED |
| succeeds on SQLite 3.51.1 | succeeds on 3.46.0, 3.50.4 and 3.53.4 | CONFIRMED (boundary found at 3.46.0) |
| no declared minimum SQLite version | none in `pyproject.toml`, `README.md`, `docs/` | CONFIRMED |

The four triggers embedding the predicate (all from migration
`0007_phase5_provider_model_configuration._install_phase5_row_guards`, via
`safe_json_object`):

- `phase5_model_catalogue_metadata_validate_insert` — 370,076 bytes
- `phase5_model_catalogue_metadata_validate_update` — 370,093 bytes
- `phase5_capability_fact_provenance_validate_insert` — 370,966 bytes
- `phase5_capability_fact_provenance_validate_update` — 370,985 bytes

## 2. Environment

- Repository interpreter: `.venv/bin/python` 3.12.13, `sqlite3.sqlite_version` 3.50.4.
- Host CLI: `sqlite3` 3.53.4.
- Isolated scratch builds (authorised method: locally built SQLite under the campaign
  scratch path; nothing on the host was installed or replaced):
  - SQLite 3.45.1 (`sqlite3-3450100`, `libsqlite3-3.45.1.so`)
  - SQLite 3.46.0 (`sqlite3-3460000`)
  - Both built from official amalgamations downloaded to the scratch directory.

## 3. Exact failure

Replaying the migration's own generated DDL through an isolated 3.45.1 library
(`scratch/ctypes_sqlite.py`), the only parser/expression failures are the four Phase 5
metadata/provenance triggers:

```
FAIL idx=205 rc=1 len=370076 msg='parser stack overflow'
FAIL idx=206 rc=1 len=370093 msg='parser stack overflow'
FAIL idx=207 rc=1 len=370966 msg='parser stack overflow'
FAIL idx=208 rc=1 len=370985 msg='parser stack overflow'
```

Every other replay failure is an artefact of raw replay (no B.O.T.S. Python UDFs
registered, no FTS5 module in the scratch build, parameter placeholders), not a defect.

The failure occurs while **preparing `CREATE TRIGGER`** during migration 0007; it is not
a runtime DML or data-validity failure.

## 4. Root cause (authoritative, from SQLite source)

- SQLite 3.45.1 `sqlite3.c`: `#ifndef YYSTACKDEPTH / #define YYSTACKDEPTH 100` and **no**
  `YYDYNSTACK`; the LEMON parser uses a fixed 100-entry stack and calls
  `yyStackOverflow` -> `sqlite3ErrorMsg(pParse, "parser stack overflow")`.
- SQLite 3.46.0 `sqlite3.c`: `#define YYDYNSTACK 1`, `#define YYGROWABLESTACK 1`; the
  parser stack grows on the heap.
- Calibration on 3.45.1 inside the frozen migration wrapper: a `replace()` chain of 21
  nested calls compiles; 26 nested calls overflow. A long `AND`/`OR` chain instead trips
  the separate `SQLITE_MAX_EXPR_DEPTH` (1000) limit ("Expression tree is too large").
- Therefore the oversized predicate is a **parser/version limitation**, not an
  application-semantics defect. The predicate's logic is fine; its expression nesting is
  too deep for the fixed stack used by SQLite <= 3.45.x.

## 5. Affected baseline

Stock Ubuntu 24.04 ships SQLite 3.45.1, which cannot execute migration 0007. Databases
written by the defective release therefore only exist on runtimes >= 3.46.0.
