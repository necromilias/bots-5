# Design candidate — parser-safe, semantics-preserving forbidden-key predicate

Status: **proposed, not yet applied to tracked files.** Independently falsified by a
separate model instance (see `falsifier/DESIGN_FALSIFICATION.md`).

## 1. Accepted semantics being preserved

`secret_key_forbidden_sql_expression(column)` must keep producing a SQLite-only boolean
predicate equivalent to the current migration-owned predicate, evaluated against
`json_tree(...).key`, without any Python UDF. The current predicate is:

```
instr(CAST(col AS TEXT), char(0)) > 0
OR s IN (<11 forbidden keys>)
OR for each key k: ( occurrence counts of every character of k in s equal k's counts
                     AND (s with every character of k removed) NOT GLOB '*[A-Za-z0-9]*' )
```

where `s` is `lower(CAST(col AS TEXT))`, then the 19 `_UNICODE_CASEFOLD_ASCII_MAP`
replacements, then removal of all 66 `_ASCII_NON_ALNUM_CODES` characters.

The accepted predicate is deliberately **stricter than Python** in two ways, and both are
preserved exactly:

- any key containing an embedded NUL is rejected (fail-closed), whereas Python only
  rejects it when the *normalized* form is forbidden;
- the count/residual test is order-insensitive, so an anagram of a forbidden key is
  rejected although Python's predicate allows it.

## 2. Candidate repair

Two changes, both confined to the string returned by
`secret_key_forbidden_sql_expression`:

1. **Do not materialise separator removal.** The 66 `_ASCII_NON_ALNUM_CODES`
   `replace()` calls are dropped. Removing a character that is not one of a forbidden
   key's letters cannot change (a) that key's per-letter occurrence counts, or (b)
   whether a residual ASCII alphanumeric character remains after the key's letters are
   removed. The counts and the residual test are therefore evaluated directly on the
   case-fold-mapped text. The `IN (<forbidden>)` clause remains (now redundant, subsumed
   by the per-key test, but harmless and explicit).
2. **Split the 19 Unicode replacements across nested derived tables.** Because the LEMON
   parser of SQLite <= 3.45.x accepts only ~24 nested function calls inside the frozen
   migration wrapper, the 19 `replace()` calls are applied in stages of at most 10, each
   stage a separate derived table. The final projection also computes the 11 per-key
   residual values, so the outer `WHERE` is shallow.

Generated shape (schematic):

```
(instr(CAST(key AS TEXT), char(0)) > 0 OR EXISTS (
   SELECT 1 FROM (
     SELECT _bots5_casefold_2 AS _bots5_normalized_key,
            replace(...(_bots5_casefold_2)...) AS _bots5_residual_0, ...
     FROM (
       SELECT replace(<9 further map ops on _bots5_casefold_1>) AS _bots5_casefold_2 FROM (
         SELECT replace(<10 map ops on _bots5_casefold_0>) AS _bots5_casefold_1 FROM (
           SELECT lower(CAST(key AS TEXT)) AS _bots5_casefold_0)))) AS _bots5_normalized
   WHERE _bots5_normalized_key IN (...) OR (counts AND residual NOT GLOB ...) OR ...))
```

## 3. Why it works on the failing class

- Every expression in the generated SQL nests only a few function calls, far below the
  fixed 100-entry parser stack of SQLite <= 3.45.x.
- It uses only SQLite built-ins (`lower`, `replace`, `char`, `instr`, `length`, `CAST`,
  `GLOB`, `EXISTS`, derived tables). No UDFs, no temp schema, no connection state, no
  mutable external state, no application-side initialisation. Raw-DML connections
  therefore still enforce the guard.

## 4. Evidence (measured)

- Generated expression for `key`: 369,679 chars / 15,296 `replace()` calls / paren depth
  98 (baseline) -> the repaired per-trigger SQL is at most **12,605 bytes**.
- Largest statement in the whole migration chain: 370,985 -> **12,605 bytes**.
- Isolated SQLite 3.45.1 replay of the complete captured migration DDL: baseline has 4
  `parser stack overflow` failures; repaired candidate has **0** parser/expression
  failures.
- Isolated SQLite 3.45.1 runtime DML with the repaired metadata trigger: all forbidden
  variants rejected, benign keys accepted; the only difference from Python is the
  pre-existing order-insensitive rejection of `aipkey`, which the baseline also rejects.
- Semantic conformance corpus (`equivalence_corpus.py`): 13,443 values, comparing the
  baseline SQL predicate, the candidate predicate, and Python. Baseline vs candidate:
  **0 disagreements**. Both diverge from Python on the same 360 values — **0 SQL false
  negatives** (no Python-forbidden key allowed by SQL) and **360 SQL false positives**
  (305 keys containing an embedded NUL, 55 anagrams of forbidden keys) — unchanged by the
  repair. The repair is therefore equivalent to the *pre-repair SQL predicate*, not to the
  Python predicate; see `EVIDENCE_CLARIFICATION_CORPUS_360.md`.
- SQLite 3.50.4 and 3.53.4 continue to accept the repaired form.

## 5. Behaviour by case

- **Fresh database creation** (A): migration 0007 runs with the repaired generator and
  succeeds on both <= 3.45.x and >= 3.46.0.
- **Upgrade from older B.O.T.S. revisions** (B): Alembic replays 0007 with the repaired
  generator; succeeds on both runtime classes.
- **Database already created with the existing large trigger SQL** (C): unchanged by a
  generator fix; it keeps the old 1.5 MB triggers. It remains readable on SQLite >= 3.46.0
  and becomes unreadable on SQLite <= 3.45.x (`malformed database schema ... parser stack
  overflow`), which no SQL or `PRAGMA writable_schema` operation can repair on that
  runtime (verified).
- **Movement of an existing database between runtimes** (D): newly written databases
  become portable in both directions; pre-repair databases remain one-way.
- **Startup trigger validation**: trigger names, markers (`json_tree(new.metadata_json)`,
  `json_tree(new.provenance_json)`), abort messages and behaviour are all preserved, so
  `sqlite.py` validation and `_validate_phase5_trigger_behavior` need no change.

## 6. Mutation fence (if approved)

- `src/bots5/core/secrets.py` — replace the body of `secret_key_forbidden_sql_expression`
  and update its docstring; `_ASCII_NON_ALNUM_CODES` becomes unused and would be removed
  (verify no other reference).
- `tests/test_phase5_provider_model.py` — keep the existing raw-DML assertions; add the
  semantic-conformance corpus.
- No migration change is required for cases A and B. Case C/D is the open decision.

## 7. Alternatives rejected

- Python UDF: forbidden (raw-DML authority contract).
- Declaring a minimum SQLite version (>= 3.46.0): forbidden (platform policy).
- Flat all-alphanumeric count sum: semantically equivalent but ~160 KB per trigger and
  close to the expression-depth limit; strictly worse than the pipeline.
- GLOB/LIKE pattern matching: cannot express order-insensitive multiset equality.
- Recursive-CTE character walk: order-sensitive, would change accepted semantics.
- Editing historical migration 0007: not required (the generator is imported at replay
  time), and prohibited.
