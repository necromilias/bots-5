# Human decision request — existing databases carrying the oversized Phase 5 triggers

Status: awaiting Mick. No tracked file has been modified.

## The decision

How should B.O.T.S. treat a database that was **already written** by the defective release
(its `sqlite_schema` holds the four ~370 KB Phase 5 triggers), when the repair is shipped?

This is a migration / support-policy choice. Current authority does not make it.

## Why existing authority does not choose

- No minimum SQLite version and no database-portability policy is declared anywhere in
  `pyproject.toml`, `README.md` or `docs/`.
- `migration_runner.py` tracks one head (`0012_phase9_archive_import`); the trigger text
  is not part of the revision identity. A generator-only fix therefore produces the same
  revision number with different persisted trigger SQL, so an existing head database is
  never re-migrated.
- Making an existing database compatible requires either adding a forward migration that
  replaces the persisted triggers, or explicitly declaring pre-repair databases out of
  scope. Those are materially different guarantees.

## Established facts (see REPRODUCTION.md and DESIGN.md)

1. The reported defect (fresh migration / `CREATE TRIGGER` failure on SQLite 3.45.1) is
   fully repairable inside the accepted semantic fence; the design is `DESIGN.md`.
2. A pre-repair database is unreadable on SQLite <= 3.45.x:
   `malformed database schema (g) - parser stack overflow (11)`.
3. On that runtime it **cannot be repaired by any SQL or pragma path**: even
   `PRAGMA writable_schema=ON; UPDATE sqlite_schema ...` fails at schema initialisation,
   and `DROP TRIGGER` fails too. Verified against an isolated 3.45.1 build.
4. On SQLite >= 3.46.0 such a database opens normally, and a forward migration could
   `DROP`/`CREATE` the four triggers there, after which it would also open on <= 3.45.x.
5. A forward migration is therefore **not** needed for fresh creation or for upgrades from
   any earlier revision — those replay migration 0007 with the repaired generator and
   already succeed on both runtime classes.
6. Adding a forward migration would require a new revision plus changes to
   `migration_runner.py` head bookkeeping (`_HEAD`, `_PRIOR_REVISIONS`,
   `_SUPPORTED_REVISIONS`, `_MIGRATION_CHAIN`) and the chain/recovery/journal logic around
   it, and would still not help a database that is only ever opened on <= 3.45.x.

## Options

**Option 1 (recommended) — generator repair only.**
Ship the repaired predicate. Fresh creation and every upgrade path work on SQLite 3.45.1
and newer. All databases written from then on are portable in both directions. Pre-repair
databases keep their existing triggers; they remain usable on SQLite >= 3.46.0 and are not
guaranteed on <= 3.45.x.
- Consequences: smallest correct change; no migration-chain risk; the reported defect is
  eliminated. A pre-repair database on stock Ubuntu 24.04 must be recreated or first
  upgraded on a >= 3.46.0 runtime.
- Cost: one documented non-blocking limitation.

**Option 2 — generator repair plus one forward migration (`0013`) that drops and recreates
the four Phase 5 triggers.**
- Consequences: pre-repair databases are proactively converted when next opened on a
  runtime that can still read them (>= 3.46.0), after which they are portable to 3.45.x.
  It still cannot convert a database that is only ever opened on <= 3.45.x.
- Cost: new migration file duplicating the four trigger definitions (0007 may not be
  edited), plus migration-runner head/chain/recovery bookkeeping and migration tests — a
  disproportionate change to a delicate, security-relevant subsystem for a downgrade path,
  and two sources of truth for those trigger definitions.

**Option 3 — defer.** Treat existing-database portability as out of scope for this defect
and handle it, with the minimum-SQLite question, in the platform-support reconciliation.

## Recommendation

Option 1. It fixes the reported defect completely, keeps the change inside the pre-authorised
fence (`secrets.py` plus tests), writes version-portable databases from now on, and does not
touch the migration/recovery machinery for a benefit that only materialises when a
pre-repair database is moved to an older SQLite runtime. If a forward migration is wanted,
Option 2 is mechanically feasible and remains inside the campaign fence.

## Blocked / unblocked

- Blocked on the answer: implementation, the implementation falsifier, the final oracle and
  the final T4 all wait on whether the candidate set is `{secrets.py, tests}` or also
  includes migration `0013` and its runner bookkeeping.
- Not blocked: all reproduction, root-cause, design and compatibility evidence above.

---

# CORRECTION (measured after the first decision) — re-consultation required

The first decision (generator repair only) was made against a statement of mine that is
**wrong**: I said pre-repair databases "remain usable on SQLite >= 3.46.0". They do not,
once the repair is complete.

## What was missed

`src/bots5/infrastructure/persistence/sqlite.py` pins the *exact persisted trigger text*:

- `_PHASE5_SCHEMA_SHA256` (line 627) maps each Phase 5 schema object to the SHA-256 of its
  normalised `sqlite_master.sql`.
- `_validate_phase5_schema` (line 1421) compares the live database against that table and
  raises `current Phase 5 schema trigger is not migration-authoritative: <name>` on any
  mismatch. It is called from `_validate_open_connection`, i.e. on every authoritative open.

The four repaired triggers necessarily produce different SQL, so their four hashes **must**
change (they are the only four entries that move: measured 4 mismatches out of 44 objects).
The campaign fence explicitly allows this (`sqlite.py` may be touched "only if current
trigger validation must be updated to recognize or validate the semantically equivalent
repaired trigger form").

## Measured acceptance matrix (real databases, real validator)

Built one database with the pre-repair tree (`work/baseline-t0`, triggers 370,062-370,971
bytes) and one with the repaired tree (triggers ~11-12 KB), then ran
`_validate_phase5_schema` against both, with the old and the corrected hash table:

| hash table | database | result |
|---|---|---|
| pre-repair (today) | pre-repair | ACCEPTED |
| pre-repair (today) | repaired | REJECTED |
| corrected | repaired | ACCEPTED |
| corrected | pre-repair | **REJECTED** |

So a generator-only repair rejects every database written by the defective release, on
**every** SQLite version, at startup. There is no Phase 5 schema-repair path in the codebase
(grep: the only "migration-authoritative" failures are hard `RuntimeError`s).

This makes the forward migration materially more attractive than it appeared, and it is the
one thing the campaign pre-authorises for exactly this case ("If already-landed databases
require a new forward migration to replace persisted trigger definitions, adding ONE
narrowly scoped new migration may be considered").

## Corrected options

**Option A — generator repair + hash update, no migration.**
Existing pre-repair databases are rejected at startup everywhere; the operator must recreate
them (or migrate them by hand on a runtime that can still read them).
Cheapest; no migration-chain risk; hard behavioural break for existing databases.

**Option B (recommended) — generator repair + hash update + one forward migration `0013`
that drops and recreates the four Phase 5 triggers.**
Existing pre-repair databases are upgraded in place the next time they are opened on a
runtime that can still read them (SQLite >= 3.46.0), after which they are accepted
everywhere, including SQLite 3.45.1. On SQLite <= 3.45.x they still cannot be opened at all
(the schema cannot be parsed). Requires the new revision file, its trigger SQL, and the
`migration_runner.py` head/chain bookkeeping the campaign pre-authorises.

**Option C — generator repair + hash update that accepts either the old or the new trigger
text for those four triggers only.**
No migration, and existing pre-repair databases keep working on SQLite >= 3.46.0 exactly as
today; still unreadable on <= 3.45.x. Cost: `_validate_phase5_schema` must accept two forms
for four objects, i.e. the "one authoritative schema text" property becomes a two-element
set. Both forms are semantically identical, so this is not a security weakening, but it is a
loosening of the schema-authority check.

## Recommendation

Option B. It is the only option that repairs the reported defect *and* does not silently
break databases that already exist, and the campaign pre-authorised exactly this migration.
Option A is defensible only if no real B.O.O.T.S. database written by the defective release
exists yet — a product fact only Mick can confirm.
