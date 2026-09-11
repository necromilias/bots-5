# Linux v0.1 Phase 6 implementation report

## Current candidate status and authority boundary

This is the cumulative, unstaged, uncommitted Phase 6 implementation report.
The historical failed and blocked candidates and their evidence are retained
below as candidate-specific history; none is silently normalized into a pass.
The authoritative current status is the final repair campaign recorded at the
end of this document. Stage G accepted that exact candidate as **TECHNICAL
CLOSURE CANDIDATE** after the original Audit A/B findings F1-F4 and A5, the
Stage A-C design gate, the same-Sol implementation and correction work, four
fresh Stage F reviews, the one authorized Stage F repair batch, three closure
addenda, and complete deterministic validation.

The repair campaign began from the clean canonical checkout at `HEAD = main =
89fb52979c67b8b1c822a7efb3c5a67946611bdf`, with `origin/main =
c9efdb374e37be94bb9ab68abd45e8ed718c3437` and an empty index. Immediately
before this Stage H report-only update the accepted implementation comprised
exactly 11 modified tracked code/test paths, 2,847 insertions and 395 deletions;
its binary Git diff SHA-256 was
`364ca190cb438f05d75155e565a841549f84abaf046290430f48b5ff2367324e`.
This report edit is the twelfth modified tracked path and necessarily changes
the complete working-tree diff and any seal that includes the report; it does
not change the accepted code/test diff.

No provider, network, credential, Organisational Memory, or Phase 7 operation
occurred in this repair leg. The earlier explicitly authorized local Ollama
evidence at `192.168.50.223` remains historical evidence and was not rerun.
No commit, stage, push, fetch, pull, checkout, or ref mutation is claimed.
Stage I independent closure audit and Stage J publication/closure have not
occurred.

Normal Phase 6 sends are v3-only in the default configuration. Historical
v1/v2 attempts remain readable; an explicit legacy Phase 5 configuration is
the only compatibility path. The normal path requires the closed deterministic
context plan and exact capability/settings facts before persistence or
dispatch. The candidate remains deliberately unstaged and uncommitted.

## Human policy and repaired findings

The repair preserves the policy that mandatory instructions, the current user,
selected eligible attachments, and the deterministic envelope must fit exactly;
unknown, ambiguous, missing, stale, corrupt, ineligible, or over-budget facts
fail before durable user/assistant/attempt/plan/reference state or backend
dispatch. Historical context is an active-lineage whole-turn suffix. Excluded
history occurs exactly once in the frozen source list with its exclusion reason;
no turn is split or silently summarized. Raw original attachment bytes remain
content-addressed and reconstructable.

The convergence repair claimed to address and added regressions for these
previously confirmed findings:

* duplicate historical IDs caused by whole-turn exclusion;
* capability-present/v2 fallback and start-of-turn capability, override,
  settings, selection, catalogue, and connection authority races;
* forged canonical JSON, duplicate/upper-case/non-SHA wire digests, and
  inconsistent budget, envelope, source, and plan fields;
* context-budget limits that were arithmetically valid but did not bind to the
  frozen `limits.context_tokens` capability's value, provenance, semantics, or
  adapter, including raw/current-schema/restart boundaries;
* extra raw message/attempt references, v2 context plans, inert trigger
  replacements, and contradictory current-schema rows;
* forged attachment representation identity, missing or mismatched durable
  payloads, unsafe roots/objects, FIFO sources, and capture/reconcile and
  GC/reattach reservation races;
  failure to release another capture's reservation;
* P6-ATT-01: separate-store startup reconciliation could clear a live capture
  marker and permit GC before the first capture committed;
* P6-RC-01: reopening a completed v3 request rejected a legitimate later
  capability override because it compared immutable historical evidence with
  mutable current capability state;
* attachment-bearing regeneration, all 0001-0008 upgrade starts, and the
  cross-chat asynchronous attachment button refresh.

The persistence boundary validates the actual current tables and rows on open,
checks every durable payload against its owned regular object, and uses a
distinct per-capture reservation lease plus filesystem lock for
capture/reconciliation/collection. A v3 plan's context limit is bound to the
frozen resolved capability before normal start and at raw/current-schema
boundaries. SQLite guards enforce the structural and semantic invariants
available to SQLite; no claim is made that SQLite authenticates a fully
privileged attacker.

## Accepted attachment redesign contract

The accepted redesign requires one B.O.T.S. core/data-root owner, a pinned
descriptor-backed lifetime lease, and one attachment transition authority.
Bootstrap, migration, and direct store opens must bind to the exact lease for
the root they mutate; competing stores must reject before database or
filesystem mutation. Multiple windows may share the same core/store; restart
may reacquire only after the prior owner has ended.

Capture, blob/attachment records, reference transitions, GC, and recovery must
be serialized by one attachment service boundary. A GC decision needs a fresh
linearization point under the same service authority as publication/reference
creation. Reservation ownership, if retained, must be opaque and bound to the
issuing service, exact digest, and live descriptor; copied strings, wrong
owners, and double release must not release a live lease.

Filesystem transitions must use descriptor-relative/no-follow Linux operations
through pinned root/component identities, not a path check followed by later
pathname mutation. Lock/unlock/descriptor cleanup failures must be surfaced.
Migration scripts 0001-0008 remain byte-identical to the supplied baseline;
only uncommitted 0009 is changeable. The post-build findings below establish
that the earlier candidate did not yet meet those requirements. They remain
preserved as historical evidence; the Stage G accepted repair record at the
end of this document supersedes that former current-status conclusion.

## File inventory

Changed or added candidate paths, in deterministic path order:

* `src/bots5/bootstrap/desktop.py`
* `src/bots5/core/__init__.py`
* `src/bots5/core/application.py`
* `src/bots5/core/context.py`
* `src/bots5/core/generation.py`
* `src/bots5/core/ports.py`
* `src/bots5/core/provider_configuration.py`
* `src/bots5/desktop/window.py`
* `src/bots5/domain/models.py`
* `src/bots5/domain/provider.py`
* `src/bots5/infrastructure/attachments.py`
* `src/bots5/infrastructure/authority_lock.py`
* `src/bots5/infrastructure/data_root_authority.py`
* `src/bots5/infrastructure/generation/openai_compatible.py`
* `src/bots5/infrastructure/persistence/migration_runner.py`
* `src/bots5/infrastructure/persistence/migrations/versions/0009_phase6_context_attachments.py`
* `src/bots5/infrastructure/persistence/phase3_validation.py`
* `src/bots5/infrastructure/persistence/phase6_schema.py`
* `src/bots5/infrastructure/persistence/phase6_validation.py`
* `src/bots5/infrastructure/persistence/schema.py`
* `src/bots5/infrastructure/persistence/sqlite.py`
* `tests/test_desktop_draft1.py`
* `tests/test_phase1_persistence.py`
* `tests/test_phase2_persistence.py`
* `tests/test_phase3_generation.py`
* `tests/test_phase5_provider_model.py`
* `tests/test_phase6_context_attachments.py`

This report is excluded from the candidate seal.

## Deterministic validation actually run

The focused Phase 6 suite now contains 27 deterministic tests and passed
27/27. It includes explicit negative reproductions for every repair item
listed above, including raw SQLite reference/plan/representation attempts,
restart refusal, inert guards, migration starts at each supported revision,
filesystem reservations and distinct lease interleavings, regeneration, and
the UI chat-switch race. Wave 3 directly covers a forged v3 budget limit and
restart mismatch, plus two same-digest capture leases held through GC until
the final owner releases.

The following checks completed on the final repair tree:

* `.venv/bin/python -m pytest -o addopts='' -p no:cacheprovider
  tests/test_phase6_context_attachments.py tests/test_phase5_provider_model.py
  -q` — **91 passed**.
* `.venv/bin/python -m pytest -o addopts='' -p no:cacheprovider` — **371
  passed, 1 skipped** (372 collected). The skip is the existing explicitly
  opt-in local-provider test; no provider was contacted.
* `.venv/bin/python -m pytest -o addopts='' -p no:cacheprovider
  tests/test_phase2_persistence.py tests/test_phase6_context_attachments.py
  -q` — **46 passed**.
* `.venv/bin/python -m pytest -o addopts='' -p no:cacheprovider
  tests/test_desktop_draft1.py -q` — **13 passed**.
* `.venv/bin/python -m compileall -q src tests` — passed.
* `git diff --check` — passed.

The focused Phase 6 run exited 0 with **27 passed**; the combined Phase 5
companion run exited 0 with **91 passed**. The migration subset exited 0 with **46
passed**; compileall exited 0 and `git diff --check` exited 0. No provider,
network, or managed credential boundary was exercised.

No live acceptance, provider call, network access, managed-credential read,
paid call, commit, stage, push, or ref mutation is claimed by this report.

## Historical W4 evidence and redesign review boundary

Fresh read-only Terra rechecks verified the exact candidate seal and report
hash above before and after their probes. The history/persistence recheck
passed: a completed v3 request frozen at `32768` survived a normal later
context override to `32769` and a legitimate settings mutation, with its
snapshot, plan, canonical digest, and wire digest byte-identical; pre-start
CAS still rejected changed authority before durable start or backend dispatch.

The redesign writer claimed to address the stale GC snapshot, forged
reservation release, and post-init objects-directory replacement findings:

* GC now snapshots and rechecks under one service lock and fresh write
  transaction.
* Reservation release requires the live opaque owner handle.
* Root/layout identity replacement and unsafe lock entries fail closed.

The focused suite asserts competing-store rejection before mutation,
release/reacquisition reconciliation, and same-core operation. Those writer
tests did not establish the stronger architecture invariants; the independent
post-build results follow.

## Redesign post-build review — BLOCKED

Three fresh, read-only Terra/xhigh reviews independently verified the candidate
seal and report identity before and after disposable hostile probes. They found
the redesigned candidate still fails the accepted attachment authority model:

* `P6-ATT-FS-01`: attachment operations still use pathname `lstat`/`open` and
  verify/`unlink` sequences. A replacement of `objects/<prefix>` with a
  symlink after verification made orphan reconciliation delete an external
  sentinel; source replacement between check and open also captured different
  bytes. The claimed descriptor-relative invariant is false.
* `P6-GC-01` / `P6-OWN-B-01`: a supplied authority lease for an unrelated
  root can open and mutate the actual database, and replacement of a lock/root
  pathname can admit a second owner. This is split-brain mutable authority.
* `P6-GC-02`: recovery still enumerates durable blobs before the filesystem
  lock/authoritative deletion decision. A concurrent normal ingestion can
  commit an attachment whose bytes the stale recovery subsequently removes;
  reopen then fails durable verification.
* `P6-OWN-B-02`: a live reservation handle can be released through a wrong
  digest or another manager, making the true owner collectible.
* `P6-GC-03`: raw SQLite deletion of a historically referenced attachment
  succeeds with foreign keys off, and startup accepts the resulting dangling
  references.
* `P6-GC-04`: replacing a required guard with a semantically inert false
  trigger still passes current-schema validation.

The resource-lifetime/malformed-state domain was not run to a separate verdict:
the first three domains already independently demonstrated a systemic failure
of the accepted authority design. Under the authorised convergence rule, no
small-omission repair batch was dispatched. This campaign therefore stops for
human direction rather than continuing a patch chain. No live acceptance,
final Sol closure audit, staging, or commit authorization is claimed.

## Candidate seal methodology

The deterministic candidate seal is computed as the SHA-256 of the per-file
`sha256sum` lines for all modified and untracked non-ignored candidate paths,
excluding this report itself, in the exact path order listed in the File
inventory above. The final value is recorded below only after the final repair
validation and report update:

`89ca4c2ea1423e059bb7e1e62314d9388319c936bf788f7980771480b8ecf945`

## Known non-goals and caveats

Full archive backup/import/export, search, multimodal conversion, OCR, provider
transformation, automatic retries or substitution, public-provider acceptance,
Phase 7+, live acceptance, and final closure adjudication remain unperformed.
The focused and full deterministic tests establish only local evidence for this
unstaged blocked candidate; they do not override the systemic post-build
authority findings recorded above.

# Authoritative corrected-design recovery record

This section supersedes the historical candidate disposition above while
preserving it as evidence. The earlier text describes seal
`89ca4c2ea1423e059bb7e1e62314d9388319c936bf788f7980771480b8ecf945`,
whose report SHA-256 was
`952effa0b16a6e45234ffc51d6e8578e9a28bbd609ead19b536556cc445c750a`.
Neither that candidate nor its four ordinary repair waves is represented as
successful.

## Starting authority and preserved failed evidence

The corrected-design and recovery campaigns remained anchored throughout to:

`HEAD = main = origin/main = c9efdb374e37be94bb9ab68abd45e8ed718c3437`

Phase 5 is closed and published only at that revision. Phase 6 remains entirely
unstaged and uncommitted. The failed first corrected-design implementation was
preserved before recovery with:

- Stage D candidate seal:
  `a62576d2a8ff4c78749d075d193a3cdc34ac44e9591af499fc6aed6bd1584042`;
- Stage F supervisor adjudication SHA-256:
  `c74be88b844964d6860083c5a1bf1be1665df2547bfebcee2c8f7b495e90369f`;
- forensic manifest SHA-256:
  `4e66bad8e0dbf8489a7e096800736024f604c53c95687d56388d8acf0430770d`;
- complete tracked patch SHA-256:
  `e22cf7493a9a087b4fd673743a57c758e0693bbaf41a0b80cbd2ebb085556ee5`;
- complete 30-path failed-tree archive SHA-256:
  `47dbc1fc6436766aded25412d4cc24cf5d252a8115c20946befac98236854af2`.

The preserved Stage E failure was 85 focused Phase 5/6 tests passed and 6
failed; the full suite was 365 passed, 6 failed, 1 skipped. That implementation
had replaceable fallback locking, pathname SQLite access, unsafe descendant
reopening, incomplete migration recovery, reconciliation/GC races, and
close/descriptor lifetime failures. It was rejected systemically and was not
treated as a compatibility target.

## Corrected design gate

The corrected design gate passed before either implementation campaign. Its
authoritative artifacts are external to the repository under the campaign
output directory:

- Stage A fresh Sol/xhigh authority architecture:
  `PHASE6_STAGE_A_AUTHORITY_ARCHITECT_CHECKPOINT.md`, SHA-256
  `d62b98f7837c9e0bf3dc4c947a2854e26b34079f65be24a75751ef8c439b3c65`;
- four independent Stage B Terra pre-mortems:
  - root/process `429af7949b7904b4e6b04c64f8e9b37810796a1f156b834eba9453bd50837c80`;
  - filesystem/path `f8b86f1807eb26f03602485e3c5df31bdd85ccbd54eb6274ce0d68a1abc25d65`;
  - SQLite/crash `59a49308ccf397aea0fe479ab8b4da17ffd6a6d170752d1c59b57f69d573ab3e`;
  - lifecycle/concurrency `69edcad5d45756b0f00d453e457da8fee5be551737adf3d0aa8bfba6fa1a58c3`;
- same-Sol Stage C reconciliation:
  `5e684052bd91a4dd37b89d96b627fbdf0a8dfc018dd7960224aec7c3c57f0d49`;
- Stage C supervisor adjudication:
  `4154f67a8193750b0d81b128e76210ae2b34440df24fea222e9cf24410494e73`;
- C2 close/GC/migration delta:
  `af3c72543f6bcfd5edf663004fc7045d83fdc94ffeeb8cd7f2de7f06dac4a74c`;
- C2 supervisor adjudication:
  `35a4c3dff2660dbd6948f2e3cb6d618cfd05212512d6c22db30b1a831bb240c1`;
- C3 candidate-journal/fresh-ABSENT delta:
  `28200770ba622350c4f8015001e8cf9153ce05a7773f4d17494dd474652ab68e`;
- final C3 supervisor adjudication:
  `56beb44a1a3c16625b31ee69f2c7068d6d3bfa2cc2b3e9c7a8ed32bd6a9e188c`.

The Terra attacks forced explicit logical-root lifetime through physical
release, close accounting, per-operation admission, rooted SQLite VFS,
descriptor-only attachment mutation, fresh GC authorization, durable D0-D4
deletion intent, hash-free `PREPARING`, first source hash at
`SOURCE_QUIESCED`, exact candidate-plus-hot-journal disposal, validated restore,
and true `ABSENT` no-replace publication. C2/C3 targeted reviews and
adjudications resolved the design omissions. The final design gate disposition
was **PASSED**; no later implementation finding reopened or changed it.

## Recovery implementation R1/R2

One fresh Sol/xhigh worker was the sole recovery implementation writer. Luna
was not used. It replaced the failed authority, attachment, migration, and
SQLite-opening substrate with:

- one kernel-anchored `DataRootAuthority` with abstract AF_UNIX logical claim,
  retained hierarchy/fixed-descendant/inode claims, exact trust/mount policy,
  fork invalidation, CLOEXEC, and socket-last release;
- a compiled native rooted SQLite VFS using synthetic instance names and
  retained database/directory/temp capabilities for runtime, validation,
  Alembic, WAL intake, rollback journals, and private candidates;
- one store-owned private attachment filesystem service, flat strict internal
  identities, stable opened-source hashing, no reservation bearer protocol,
  durable staging/publication states, and D0-D4 GC recovery;
- unconditional SQLite history/lifecycle guards and exact DDL plus destructive
  rollback-only startup probes;
- strict journal-v3 migration graphs for every 0001-0008 start and explicit
  fresh `ABSENT` publication.

The R1 handoff SHA-256 was
`4b3201bad5098f20aa5083eff86790acfb60701e70730f193f3fb2f1fd2a1214`.
Its first implementation seal was
`c4c4043ee360e15178f42262c54cbe535a3c6634e4066409ca44b4836bb0ca6d`.
Independent supervisor R2 evidence, SHA-256
`ec78ea64c8fdcce623b2c8e9d44ead776624c1a0dd9f5b4c425376ab04bbd250`,
confirmed 442/442 focused, 210/210 Phase 1-5 compatibility, and 780/780 full
non-opt-in tests, plus compile, native `-Werror` build/load, diff/index,
migration-identity, ref, and offline/provider boundaries.

## R3 reviews and one bounded R4 repair

Four fresh Terra reviewers assessed the same `c4c4043...` seal under the
authorized 3+1 schedule. Their reports were:

- root/process/close — **BLOCKED**, SHA-256
  `820f9e435726012f6af32ebdad1021d9cb43af4bde4212a4446c887c0e90510b`;
- filesystem/descriptor containment — **PASS WITH CAVEATS**, SHA-256
  `608dd4c50157d468ba5bca7dea9d0702c2a2a89c5c04ccb0d74c3620adf04277`;
- SQLite/VFS/migration/crash — **BLOCKED**, SHA-256
  `eb31bd201380f60da5ed69551e3acc09327816c4b6f39773875f20dffe134476`;
- lifecycle/GC/concurrency — **BLOCKED**, SHA-256
  `fc0079219d7e54a4c1779416cc06cb614f6b2ef77c5c9e9fb139dbc8d7e55e58`.

They independently reproduced three implementation omissions: public
concurrent close callers re-entered VFS teardown; `CLOSING` revoked an already
admitted attachment operation; and a FIFO migration-journal leaf blocked
before type validation. They did not reproduce the failed Stage F split-brain,
sentinel, intermediate-link, pathname SQLite, stale live reconciliation,
standalone manager, raw-DML, trigger replacement, or late-publication failures.

The supervisor adjudication SHA-256 was
`8e662cb132c110d54655d267b7e182bef0dcae23e021a52a2da597e16514c4d9`.
It classified all three blockers as bounded implementation omissions inside
the already-accepted design, not design failures. The same Sol writer used the
single authorized repair batch. Only `pyproject.toml`,
`data_root_authority.py`, `migration_runner.py`, and the Phase 6 test file
changed relative to the R1 seal. The repair added one close owner with joining
waiters/result propagation, private whole-operation lease depth through
`CLOSING`, nonblocking no-follow journal opening, and Python 3.14 metadata.

The R4 Sol report SHA-256 was
`de0781d25c7a2f921d5bee81b32ae0a101c1555e1bf6b93e5bbf890f43f6b81e`.
Ten tests were added. Final exact-candidate gates were 452/452 focused,
210/210 compatibility, and 790/790 full, all with zero failures/skips. Two
fresh targeted Terra verifiers then returned **VERIFIED**:

- close/admission, SHA-256
  `7e23c2b01ffb19c06481d5154b3acd4a0d0d2d86d8f360c576a22df63e93f26d`;
- journal/metadata, SHA-256
  `2e76f22159e44133aa1e1d1a2bf0885c6e38abbb397d3696558417996e262928`.

No second repair batch was used or authorized.

## Final deterministic evidence

The governing commands and results on the current candidate are:

- `.venv314/bin/python -m pytest -q tests/test_phase6_context_attachments.py`
  — **452/452 passed**, 0 failed, 0 skipped, exit 0;
- the exact thirteen-file Phase 1-5 aggregate recorded in the R4 handoff —
  **210/210 passed**, 0 failed, 0 skipped, exit 0;
- `.venv314/bin/python -m pytest -q tests --ignore=tests/test_phase3_local_qwen.py`
  — **790/790 passed**, 0 failed, 0 skipped, exit 0;
- `.venv314/bin/python -m compileall -q src tests` — exit 0;
- native VFS rebuild with C11 `-Wall -Wextra -Werror`, followed by rooted-VFS
  load/open-count/mixing test — exit 0 and 1/1 passed;
- `git diff --check` — exit 0;
- `git diff --cached --quiet` — exit 0;
- `git diff --exit-code HEAD --` migrations 0001-0008 — exit 0.

The final suite used an autouse socket blocker and mock transports, excluded
the explicit local live test, removed `OPENROUTER_API_KEY`, and made no provider
or network request. The managed command sandbox denies mandatory abstract
AF_UNIX binding with `EPERM`; authoritative kernel tests ran unchanged in the
permitted host environment. Python 3.14.7, SQLite 3.53.4, SQLAlchemy 2.0.52,
Alembic 1.19.1, and pytest 9.1.1 were observed. The native x86-64 ELF SHA-256
was `e222f885a1fd24af2a1bb821581fd2fc97f801d4940021d7be46cf91e6b1c33a`;
it is ignored and rebuilt from sealed C source.

Committed migrations 0001-0008 remain byte-identical with SHA-256 values:

`15b7a409d35e3313db288f201e67d2e23c0a89e6f90058ad367dc879034e2da1`,
`7ddd512e48e75518c2730b0881a49e0f639fcf6d378e6e6a68b937da2f157c74`,
`2058cbaf6ed630d354d126edb58307b54ec9f2518b4d50810d4ced3025af4cd4`,
`3d7187d735fd3caa6fbabcc622b47dde5209403bab886b971bf63a007437b253`,
`2afd16e2f7d21f27cb27aea87d7cbb135c37892900de5745eb8ae1c0dacd41c6`,
`b670b4d26f11e22c94bf30283ab840512d261b07fba7ae7bd92ce27ff1a821a4`,
`46bc12a9cd10b4c6a262b5bc5ca0a02ecc5d60201931a45dffe7fef3cf69eea6`,
and `de23ea29c3c9749adb7ef01ce5d8a4ce34425798972524d743d2b5152aac47b9`.

## R5 bounded local Ollama acceptance

The final governing live run was **PASS**. Its report SHA-256 is
`08a0819d2aba3af0f67ae9230de447d0c528b02a8aa407213c5316371db01b3e`.
It used only `http://192.168.50.223:11434/v1`, model
`qwen3.5:35b-a3b`, temperature 0, output limit/reserve 256, reasoning effort
exactly `none`, no credential, and the existing `Provider.complete()` seam.

The local model returned the attachment-only token exactly with
`finish_reason=stop`, request ID `chatcmpl-706`, returned model
`qwen3.5:35b-a3b`, usage 196 prompt / 33 completion / 229 total tokens, no
reasoning/cost metadata, and 2.609688885975629 seconds duration. The selected
attachment was ingested twice into one SHA-256 blob, the exact text bytes were
read through the authority service, and the final version-3 plan recorded its
attachment source, canonical digest, wire digest, code-point budget, reserve,
and headroom.

Three total local calls are preserved: an initial 64-token/unset-reasoning call
failed correctly as `empty_model_response`; a 256-token/`none` call proved
content use but retained a 64-unit harness reserve; the final call aligned the
plan and request at 256 and is the accepted result. No call was public or used
a credential.

The live probe is deliberately bounded. The product has no exact Phase 6
model-token accounting adapter registered for generic/local HTTP connections;
those configured application sends fail closed. The probe therefore used the
sealed deterministic plan wire through the provider-completion seam and does
not claim a production local-HTTP accounting adapter. Persisted plan/lineage,
budget, and backend-request invariants are deterministic evidence from the
452/790 gates, not inferred from the live call.

## Exact current inventory and implementation seal

The working tree contains 30 modified tracked paths and 13 untracked files.
The index is empty. The 42 report-excluding implementation paths are:

```text
pyproject.toml
src/bots5/bootstrap/desktop.py
src/bots5/core/__init__.py
src/bots5/core/application.py
src/bots5/core/context.py
src/bots5/core/generation.py
src/bots5/core/ports.py
src/bots5/core/provider_configuration.py
src/bots5/desktop/window.py
src/bots5/domain/models.py
src/bots5/domain/provider.py
src/bots5/infrastructure/app_paths.py
src/bots5/infrastructure/attachments.py
src/bots5/infrastructure/authority_lock.py
src/bots5/infrastructure/data_root_authority.py
src/bots5/infrastructure/generation/openai_compatible.py
src/bots5/infrastructure/native/__init__.py
src/bots5/infrastructure/native/build_rooted_vfs.py
src/bots5/infrastructure/native/rooted_sqlite_vfs.c
src/bots5/infrastructure/persistence/migration_runner.py
src/bots5/infrastructure/persistence/migrations/env.py
src/bots5/infrastructure/persistence/migrations/versions/0009_phase6_context_attachments.py
src/bots5/infrastructure/persistence/phase3_validation.py
src/bots5/infrastructure/persistence/phase6_schema.py
src/bots5/infrastructure/persistence/phase6_validation.py
src/bots5/infrastructure/persistence/schema.py
src/bots5/infrastructure/persistence/sqlite.py
src/bots5/infrastructure/persistence/transition_guard.py
src/bots5/infrastructure/rooted_sqlite_vfs.py
tests/__init__.py
tests/_authority_test_support.py
tests/test_desktop_draft1.py
tests/test_phase1_core.py
tests/test_phase1_desktop.py
tests/test_phase1_paths_lock.py
tests/test_phase1_persistence.py
tests/test_phase2_core.py
tests/test_phase2_persistence.py
tests/test_phase3_generation.py
tests/test_phase4.py
tests/test_phase5_provider_model.py
tests/test_phase6_context_attachments.py
```

The only additional untracked file is this report. Its hash is computed and
recorded externally after each report-only update. The previous final-audited
implementation candidate seal was:

`ce006bb9ea93b2e27889f6182c59890ac0d0301d327e8ae67f4c17658fa32358`

That candidate's audit disposition and blockers are preserved below. The new
FCR report-excluding implementation candidate seal is:

`376d02af1bb807fc2271eeadbe2949a6653f780bc4a17b43f0d553b501ac6809`

No implementation code, test, schema, migration, native source, or candidate
metadata may change after this FCR seal. The new final audit may cause only a
report append and report-hash recomputation.

## Cumulative model and repair telemetry

- Luna: **6**.
- Terra: **57** model dispatches, including four completed R3 general reviews,
  two completed fresh targeted R4 verifications, and one filesystem-review
  dispatch rejected by the environment safety filter before inspection/report,
  plus the three fresh FCR targeted reviewers.
- Sol: **10**, including the previous final closure auditor, fresh FCR sole
  repair writer, and new fresh FCR final closure auditor.
- Historical ordinary implementation repair waves: **4**.
- Historical failed attachment-redesign campaign: **1**.
- Corrected design gate: **PASSED** after C2/C3 reconciliation.
- First corrected-design Luna implementation: **FAILED** at Stage F.
- Current recovery post-review repair: **1** bounded same-Sol batch used; no
  second batch used or authorized.
- Final bounded FCR implementation repair: **1** fresh-Sol batch used.
- Optional FCR micro-repair after targeted review: **0**.

## Remaining assumptions, caveats, and prohibited scope

- The Phase 6 threat boundary does not claim protection against arbitrary
  malicious same-UID process/injected-code rewriting of private database,
  schema, or attachment leaves. The reproduced verify-to-unlink final-leaf
  swap after private GC capture remains an explicit excluded-boundary caveat.
- Privileged bind-mount creation and alternate-EUID probes were unavailable;
  no positive runtime claim is made for those probes. Mount-ID, EUID, ACL,
  mode, type, link-count, no-follow, alias, and replacement checks did run.
- Supported deployment requires truthful Linux abstract AF_UNIX within one
  network namespace, `openat2`, `statx`, descriptor `flock`, `renameat2`
  exchange/no-replace, and file/directory fsync behavior. Missing/uncertain
  primitives fail closed.
- Python `_sqlite3`, migration/runtime SQLite, and the native VFS must share
  one dynamically loaded SQLite instance. A private/mismatched instance fails
  closed. Python 3.14 dependency deprecation warnings are retained as warnings.
- The native `.so` is target-specific and ignored; deployments rebuild it from
  the candidate C source.
- No Organisational Memory query/update, Phase 7, search, Android, daemon,
  remote client, tool/plugin, RAG, scheduling, campaign UI, public provider,
  public credential, or publication action occurred.
- No stage, commit, push, fetch, pull, merge, rebase, cherry-pick, reset, stash,
  checkout/switch, tag, branch, force, or ref mutation occurred.

## Previous final audit status

Exactly one fresh, distinct, read-only GPT-5.6 Sol/xhigh closure auditor
inspected the immutable
`ce006bb9ea93b2e27889f6182c59890ac0d0301d327e8ae67f4c17658fa32358`
implementation seal and pre-audit report SHA-256
`2b99897527c06a2c6f8b6138f900240e18e0c2740b3ca02919c545b5278a6dee`.
The audit report is
`PHASE6_STAGE_R7_FINAL_SOL_AUDIT.md`, 15,952 bytes, SHA-256
`d6426cad9e7bd0e7dcbf8731b6435edc65f0ffa8d18d36a3a0e7df2dafdd4796`.
Its exact disposition is **BLOCKED**.

The final auditor reproduced two failures with deterministic disposable probes:

1. Current-schema normalization is not quote-aware. A stopped-process edit to
   quoted trigger literals—including the adapter ID accepted by the v3 context
   plan insert guard—survived startup validation and reached READY. This
   violates the accepted exact effective-definition/startup-rejection contract.
2. A retained closed `_AttachmentFS` helper could perform
   `capture_to_stage()` after authority shutdown when its stored numeric
   descriptors were reassigned to unrelated directories. This violates the
   accepted post-close capability and descriptor-reuse contract.

The auditor also established a third blocker directly from native close control
flow: `rooted_sqlite_vfs.c` discards `close()` errors for its private database,
claim, and temporary-directory descriptor duplicates, then reports successful
VFS destruction. Close uncertainty can therefore be reported as complete
physical release and allow normal logical-root release, contrary to the
accepted release-barrier and `FAILED_CLOSED` requirements.

Fresh auditor checks preserved the refs, empty index, implementation seal, and
pre-audit report hash. Native C11 `-Wall -Wextra -Werror` build, rooted-VFS 1/1,
the exact 210/210 Phase 1-5 compatibility aggregate, compile, diff, and
0001-0008 migration-identity checks passed. A redundant fresh focused Phase 6
run was stopped at displayed 47% after the independent blockers were
established and is not claimed as a completed pass; the sealed R6 evidence
remains 452/452 focused and 790/790 full on the same candidate.

The R5 local Ollama observation remains a bounded valid content-use result, not
a product-path generic/local HTTP accounting claim. The final audit classified
that limitation and the documented same-UID final-leaf and unavailable
privileged-probe limitations as bounded caveats rather than blockers.

No post-audit implementation repair or mutation occurred. The final report
SHA-256 is recorded externally after this report-only update.

# Final bounded post-audit implementation recovery

The prior final-audit BLOCKED disposition above remains historical authority
for the `ce006bb9...` candidate. New human authorization permitted exactly one
fresh Sol/xhigh repair writer to correct only F1, F2, and F3 without reopening
the accepted C3 design gate.

## FCR-0 preservation

Before mutation, the supervisor reverified `HEAD = main = origin/main =
c9efdb374e37be94bb9ab68abd45e8ed718c3437`, divergence `0 0`, an empty
index, 30 modified tracked paths, 13 untracked files, `git diff --check` exit
0, blocked candidate seal `ce006bb9...`, this report's then-current SHA-256
`e5d4aeb694b4ed835499125d164e84f52a60c715b9d9fb923844aef822a0975f`,
and final audit SHA-256 `d6426cad...`.

The external FCR-0 manifest SHA-256 is
`d9f91386b1f00abd63298077a076b6c75006b9294b66a2d706226340b965b8be`.
It records a 237,084-byte full-index tracked patch SHA-256
`457ad90f7c1d043a32ad0c7d161f0d66d97bc49ebc4f59d52df1572ee21b3970`
and a 201,002-byte exact modified/untracked archive SHA-256
`4a9b26b85a65ee8556b405019e2153ccd6eb03872ed3346975a9ce10da30e994`.

## FCR-1 exact implementation corrections

The fresh sole Sol/xhigh writer changed exactly six paths relative to
`ce006bb9...`:

```text
src/bots5/infrastructure/attachments.py
src/bots5/infrastructure/data_root_authority.py
src/bots5/infrastructure/native/rooted_sqlite_vfs.c
src/bots5/infrastructure/persistence/phase6_schema.py
src/bots5/infrastructure/rooted_sqlite_vfs.py
tests/test_phase6_context_attachments.py
```

F1 now tokenizes schema SQL with quoted content opaque: single-quoted strings,
double-quoted identifiers, backtick identifiers, bracket identifiers, doubled
quote escapes, case, Unicode, adjacency, and comments remain material. Only
genuinely insignificant external token layout and the canonical `IF NOT
EXISTS` storage difference are normalized. Startup exact-object comparison—not
marker acceptance—rejects materially changed quoted trigger content before
READY.

F2 gives `_AttachmentFS` an irreversible lifetime. Close invalidates its own
state, authority reference, mount identity, and four stored descriptor
identities before delegating physical close. Every consequential filesystem
entry point crosses `_live()` before a syscall and requires the exact helper,
authority state/admission, PID, mount identity, and stored descriptors. A
retained object therefore cannot act through recycled fd numbers after normal,
closing, failed-close, detached, or fork lifecycle boundaries.

F3 invalidates the three native rooted-VFS private descriptor fields before
one close attempt each, records exact `RELEASED`/`UNKNOWN` inventory, never
retries an ambiguous close, and propagates every non-success through the
native/Python boundary. `DataRootAuthority` incorporates that inventory,
selects terminal `FAILED_CLOSED`, admits no work, and retains the logical-root
claim when physical release is uncertain. Clean close still releases normally.
Test-only fault modes cover pre-issue failure and ambiguous-after-close outcome
for the database-directory, main-claim, and temp-directory slots.

The FCR-1 Sol report is 13,925 bytes, SHA-256
`bec9277dc004445a93d59f5a5aba5ce52f71cac4a4d811f77c29b637c0605c27`.
The writer found no accepted-design contradiction.

## FCR-2 deterministic validation

The supervisor independently rebuilt the native library from the repaired C
source using C11 `-Wall -Wextra -Werror`; the artifact SHA-256 was
`5244cc535c66d4ca32559046af7967065b9490970d9e344acbfbcc0bf7290af0`.
Governing results were:

- focused F1/F2/F3 selection: **36/36 passed**, exit 0;
- complete mandatory Phase 6 suite: **466/466 passed**, zero
  errors/failures/skips, exit 0, JUnit SHA-256
  `62c3001df6db1f875fcd8b062d16e7feb94a807c5e1ce35c28e741d6a79d4ba8`;
- exact Phase 1-5 compatibility aggregate: **210/210 passed**, zero
  errors/failures/skips, exit 0, JUnit SHA-256
  `2c1659db816455779c315e921c1c33edf9b085b4f85ff1f41e94527d83266be9`;
- complete non-live deterministic suite: **804/804 passed**, zero
  errors/failures/skips, exit 0, JUnit SHA-256
  `7899dcff0a9ded699aae6f99aff350f1916b47e3edf6c7df9933e0d2c321a54d`;
- rooted-VFS close/identity selection: **8/8 passed**, exit 0, JUnit SHA-256
  `f3164db6a402d3a5d250fc0ccdec49bfd0a5a40ea7a6be7103cfbd4f0d641d48`;
- compile, native build, migration identity, diff, index, refs, and candidate
  seal: exit 0.

An initial supervisor Phase 6 run lost its live command handle during an app
context refresh before terminal evidence and was not counted. An initial
compatibility run was invalidated when disposable forced-death artifacts filled
`/tmp`; pytest reported `ENOSPC`. Only exact FCR-created test directories were
removed, completed evidence was preserved, and the clean 210/210 rerun above
is governing. Neither event changed candidate bytes.

Committed migrations `0001_desktop_state.py` through
`0008_catalogue_refresh_outcomes.py` remained byte-identical with the eight
previously recorded hashes. Ordinary validation removed `OPENROUTER_API_KEY`,
used mock/socket-blocking boundaries, excluded the explicit local-live test,
and made no provider/network request. The required abstract AF_UNIX authority
tests ran unchanged on the permitted local host because the managed sandbox
denies that primitive with `EPERM`.

The external FCR-2 validation report SHA-256 is
`92ab57351a92cb0a5ca5b552c054764bb1b1e418b10d5b473320d344ede65853`.

## FCR-3 targeted Terra reviews

Exactly three fresh independent Terra reviewers ran concurrently without
receiving peer conclusions before completion:

- F1 schema semantics: **PASS**, 34/34 focused tests plus real startup/tamper
  probes; report SHA-256
  `087cf984a2f5fe664943f1b12d8960c008aa849950ebe604fa9f54f011b09a6e`;
- F2 attachment capability lifetime: **PASS**, 14/14 focused tests plus an
  independently syscall-instrumented 26-method probe across clean-close,
  CLOSING, FAILED_CLOSED, and fork states; report SHA-256
  `fba2eafecb521e119cdbd7929b541cb9236ca4571596c8f1863dfddfe2d2d3b0`;
- F3 native VFS shutdown: **PASS**, fresh `-Werror` build, 7/7 native close
  outcomes, and rooted-VFS identity 1/1; report SHA-256
  `36ab8e252d608ce6c0dfe55141664020de48bbe8acd1b6a0ce478cd9b43e1091`.

The supervisor classified F1, F2, and F3 as **resolved**. No invalid/duplicate,
accepted caveat, unresolved blocker, or design contradiction remained. The
optional same-Sol micro-repair count is **zero**. The FCR-4/FCR-5 adjudication
report SHA-256 is
`4054d2078dc18b3c9f02cee532715f77df12055613a25247bd3638fdaf5bf66b`.

## FCR-5 live acceptance decision

The existing local Ollama acceptance is retained without rerun. These repairs
change only fail-closed schema readiness, attachment-helper lifecycle rejection,
and authority teardown reporting/claim retention. They do not change successful
attachment ingestion bytes, selection, context construction or budgeting,
persisted plan/wire bytes, provider request contents, response handling, or the
`Provider.complete()` seam. The 466/804 deterministic gates and F2 review also
exercise normal live attachment behavior.

The governing retained result remains local endpoint
`http://192.168.50.223:11434/v1`, model `qwen3.5:35b-a3b`, exact
attachment-only output, `finish_reason=stop`, and usage 196 prompt / 33
completion / 229 total tokens. Its report SHA-256 remains
`08a0819d2aba3af0f67ae9230de447d0c528b02a8aa407213c5316371db01b3e`.
No new live, public, provider, credential, or network request occurred.

## New FCR seal and pending closure audit

The new immutable report-excluding candidate seal is:

`376d02af1bb807fc2271eeadbe2949a6653f780bc4a17b43f0d553b501ac6809`

At sealing, all three refs remained
`c9efdb374e37be94bb9ab68abd45e8ed718c3437`, divergence `0 0`, the index
was empty, the tree remained unstaged/uncommitted with 30 modified tracked and
13 untracked files, and `git diff --check` passed. The report hash is recorded
externally immediately after this FCR-6 report-only update.

Cumulative telemetry before the new final audit is 6 Luna, 57 Terra, and 9
Sol. Historical telemetry remains: four ordinary repair waves, one failed
attachment redesign, one previous bounded Sol implementation repair, and this
one final bounded FCR Sol repair. The optional FCR micro-repair count is zero.

Exactly one new fresh, distinct, read-only GPT-5.6 Sol/xhigh closure audit was
dispatched against this seal. No implementation code, test, schema, migration,
native source, or candidate metadata changed after sealing; only this report
was updated to record the disposition and final report hash.

## New FCR final Sol/xhigh closure audit

Exactly one new fresh, distinct, read-only GPT-5.6 Sol/xhigh auditor inspected
the immutable
`376d02af1bb807fc2271eeadbe2949a6653f780bc4a17b43f0d553b501ac6809`
candidate seal and pre-audit report SHA-256
`7a6c3428931f1b9c58d9bd3590cdb0f6b28cecd9b5028dfb640798df240c9e32`.
The external report is 25,730 bytes, SHA-256
`a4a46c6f703a79d6870ac353565afaacf9de68eed2c565b61c495bb18cc03b49`.
Its exact disposition is **PASS WITH CAVEATS**.

The auditor independently reverified the entire authority/design/evidence
chain, both historical sealed candidates, the exact six-path FCR delta, all
refs/index/status, the implementation-report hash, and the 42-path candidate
seal. It reviewed the actual schema, attachment, authority, native/Python VFS,
migration, store, context, UI, and provider-boundary code rather than relying
on report totals.

Fresh audit execution included:

- F1/F2/F3 focused selector: **36/36 passed**, exit 0;
- additional live/admitted-helper and shared-VFS checks: **4/4 passed**;
- all required Phase 6 table/index definitions: **8/8 passed**;
- migration/restart/identity/FIFO-journal selection: **3/3 passed**;
- independent F1 token probe: 26 canonical objects, 107 quoted/blob tokens
  preserved opaque, and eight material collision pairs distinguished;
- direct material CHECK alteration: rejected before READY with
  `FAILED_STARTUP`;
- fresh native C11 `-Wall -Wextra -Werror` build, compile, diff, empty-index,
  ref, migration preservation, and seal checks: exit 0;
- direct parsing/re-hashing of the surviving FCR-2 JUnit artifacts confirming
  466/466 Phase 6, 210/210 Phase 1-5, 804/804 full, and 8/8 rooted-VFS, with
  zero errors/failures/skips.

The audit classified F1 quote-aware schema comparison, F2 irreversible
attachment capability lifetime, and F3 native VFS close-outcome propagation as
**resolved**. No implementation or design blocker remains. No post-audit repair
was made or authorized.

The retained local Ollama acceptance decision was independently accepted. The
six FCR changes affect only fail-closed startup/lifecycle/teardown behavior and
do not change successful attachment/context/wire/provider behavior, so no new
live call was necessary.

The disposition carries four bounded caveats already within accepted Phase 6
scope:

1. arbitrary malicious same-UID in-process/ptrace/descriptor theft, raw live
   page/schema rewriting, custom UDF/extension/VFS behavior, and hostile private
   GC-name replacement after atomic exchange remain outside the threat model;
2. CAP_SYS_ADMIN/mount, privileged bind, alternate-EUID, deliberately split
   namespace, lying filesystem, and kernel-compromise probes are excluded or
   unavailable;
3. supported operation requires the documented Linux abstract AF_UNIX,
   `openat2`, `renameat2`, `STATX_MNT_ID`, truthful local flock/fsync, and one
   shared SQLite instance, with unsupported environments failing before READY;
4. generic/local HTTP has no exact Phase 6 model-token adapter, so normal sends
   fail before dispatch; the retained Ollama result remains the accepted
   provider-seam content-use observation rather than an adapter claim.

None of these caveats is an observed invariant failure. Final cumulative model
telemetry is 6 Luna, 57 Terra, and 10 Sol. The optional FCR micro-repair count
is zero. The final implementation-report hash is recorded externally after
this report-only update.

## Post-closure whitespace hygiene and continuity audit

After human adjudication closed Phase 6, the first commit gate exposed one
staged-only whitespace defect in the previously untracked
`src/bots5/infrastructure/native/__init__.py`: a redundant blank line at EOF.
One bounded post-closure hygiene repair removed only the second terminal LF,
changing that file from 78 to 77 bytes and SHA-256
`90887f3dde0a43baf04574358452d07cf0d479fbf76f1932d4796d9be2aad3d9`
to
`6a162e79955c6e208edbf50739a922bbc70b54748013a493f861c9912efdf512`;
mode `0644`, encoding, docstring content, filename, imports, comments, and code
were unchanged.

Both `git diff --check` and `git diff --cached --check` against the exact
43-path Phase 6 inventory passed after the repair. Compileall and direct native
package import sanity checks exited 0. The substantive 804-test closure suite
was not rerun because the exactly one fresh read-only Sol/xhigh delta auditor
independently found identical AST, marshalled code, bytecode, constants, line
tables, and runtime `__doc__`, and returned
**PHASE 6 CLOSURE CONTINUITY: PASS**.

The prior closed report-excluding candidate seal
`376d02af1bb807fc2271eeadbe2949a6653f780bc4a17b43f0d553b501ac6809`
is superseded solely by this authorized one-byte hygiene delta. The new final
report-excluding candidate seal is:

`4c94506ea5191e303b8dffe328d631511b65dcc04f21f415d7088ae46c65f990`

The index remained empty and no commit, push, fetch, ref mutation, provider
request, Organisational Memory operation, or Phase 7 action occurred.
Cumulative model telemetry is now 6 Luna, 57 Terra, and 11 Sol. The new final
implementation-report SHA-256 is recorded externally after this report-only
update.

## Phase 6 reopened after independent Astra falsification

Date: 2026-09-09 Australia/Melbourne.

The earlier Phase 6 campaign, technical-closure reports, deterministic results,
local Ollama acceptance, one-byte continuity repair, final seal, telemetry, and
human-authorized local commit remain historical evidence exactly as recorded
above.  They are not current closure authority.  After local commit
`89fb52979c67b8b1c822a7efb3c5a67946611bdf`, two independent Astra audits
reproduced defects in the committed candidate.  Phase 6 was therefore reopened
under one new bounded repair authorization.  This section does not claim final
Phase 6 closure: the repaired candidate still requires a separate external
Astra closure audit.

### Reopen identity and immutable inputs

The repair campaign started and ended its implementation work with branch
`main`, `HEAD` and local `main` at
`89fb52979c67b8b1c822a7efb3c5a67946611bdf`; parent and `origin/main` remained
`c9efdb374e37be94bb9ab68abd45e8ed718c3437`.  The commit subject remained
`Implement Linux v0.1 Phase 6 context and attachments`.  The initial index and
worktree were clean.  No additional local preservation branch was present in
the discovered refs; the only local branch was `main` and it was not moved.

The committed report-excluding candidate seal was independently recomputed from
the 42 changed implementation paths as
`4c94506ea5191e303b8dffe328d631511b65dcc04f21f415d7088ae46c65f990`.
The committed implementation-report SHA-256 was independently recomputed as
`4f040f1f58f57b656a0858c0273a538a6da476e10e2905d66fef41233d1ebb59`.

The supplied immutable audit inputs were verified before design work:

| Input | SHA-256 / verification |
|---|---|
| Previously supplied FULL Phase 6 bundle | `fc2dd3cd6d93403882601630d31876c497cffd966c2912a7eb4dd68527310394`; ZIP integrity passed |
| Astra audit A archive | `5f4d74e1b8fd612dad2a9ec89fa6e5e62afd94875d0dcc4c55e897e759f4b369`; ZIP integrity and its 39-entry manifest passed |
| Astra audit A report | `fb2919c66c8581a657827591f3dc7e7920d3da64110e4fadee05d434208f8467` |
| Astra audit A reproduction record | `a4f04ed95c7c97fb1d039b6b40352cebe1fb427c2f6deeccbe4ef994ed230a12` |
| Astra audit B report | `86326b3bfff05088befd089bf6a2e9f9fb49bac7cae00cd3370fa590fec4e684` |
| Astra audit B evidence archive | `1d897a584d483a67ec8e496a6bf315ee52385c8f56e4f070fb2dd65ec602144b`; ZIP integrity and its 24-entry inventory passed |

### Stage A — independent finding adjudication and repair architecture

Fresh Sol/xhigh architect session
`01a080ae-8325-7350-a013-9ab959b2c3d9` independently reproduced or traced the
findings and produced `STAGE_A_SOL_REPAIR_ARCHITECTURE.md`, SHA-256
`e6c95130fafed8fdd1177ffb14ff9e169ea6ef61fa9c1cbfe33f15379aa67af1`.
Its adjudication was:

| Finding | Disposition |
|---|---|
| Audit A publication recovery barrier | **ACCEPTED BLOCKER**, duplicate of the publication edge in F2 |
| Audit A GC/deletion recovery barrier | **ACCEPTED BLOCKER**, duplicate of the GC edge in F2 |
| Audit A capture-directory durability risk | **UNPROVEN RISK** as physical-loss evidence, but its missing ordering was accepted and repaired under F2 |
| Audit A rollback-journal directory risk | **UNPROVEN RISK** as physical-loss evidence, but its missing ordering was accepted and repaired under F3 |
| Audit A rejected-root descriptor leak | **ACCEPTED NON-BLOCKING**, directly reproduced and included in the bounded repair |
| F1 retained directory enumeration / Btrfs | **ACCEPTED BLOCKER**, causally separate from F2/F3 |
| F2 unfinished attachment namespace durability | **ACCEPTED BLOCKER**, encompassing Audit A's publication, GC, and capture edges |
| F3 rooted SQLite durability and deletion authorization | **ACCEPTED BLOCKER**, encompassing Audit A's journal-ordering risk |
| F4 application close result replay | **ACCEPTED NON-BLOCKING**, no premature authority release but a false-success API result |

The accepted directory observation model retained the authority descriptors for
containment, identity and `fsync`, but rejected retained directory streams as
fresh inventory authority.  Each authoritative inventory now uses a distinct
input-free native `openat2(retained_fd, ".", ...)` open file description,
validates it against the retained directory identity and post-bootstrap
baseline, enumerates it once, closes it successfully before exposing the tuple,
and reopens for every later observation.  It does not trust mutable pathnames,
`/proc/self/fd`, or `dup`.

The attachment model now separates namespace visibility from durability at
capture creation, staging-row authorization, capture-to-stage,
stage-to-canonical, verified deduplication cleanup, GC authorization,
object/tombstone exchange, both unlinks, rowless cleanup, D4 recovery and
intent deletion.  Destination-first cross-directory barriers preserve at least
one durable attributable payload.  Recovery unconditionally retries required
namespace barriers, obtains fresh inventories, and cannot erase the only
durable intent until all barriers and transactional reference rechecks succeed.

The rooted SQLite model now classifies rollback journals and permitted WAL
intake, tracks a pending parent-directory barrier from SQLite
`SQLITE_OPEN_CREATE`, fsyncs the journal file and database parent before the
first dependent main-page write, fsyncs journal deletion even when SQLite's
`syncDir` argument is false, and fails closed on unsupported sync flags or any
barrier/close uncertainty.  Pre-existing sidecars are freshly inventoried and
receive a pre-recovery database-parent barrier.  Every migration writer verifies
`DELETE` plus `FULL` before its first write.  The `ready -> deleting` transaction
must commit and then pass an authority-owned main-database plus database-parent
durability fence before any irreversible attachment filesystem action.

Close/lifetime corrections use data-only terminal close results and explicit
`OPEN`, `CLOSING`, `CLOSED`, and `FAILED` state; completed tasks and original
exception/traceback graphs are not retained.  All descriptors have one owner
from open through `RELEASED` or `UNKNOWN`; uncertain ownership is never retried
by descriptor number and pins same-process authority until process exit.

The directly relevant public authorities recorded by Stage A were Linux
`fsync(2)`, SQLite's synchronous pragma documentation, SQLite's Atomic Commit
document, SQLite's Unix VFS implementation, and SQLite's `sqlite3_io_methods`
and `sqlite3_vfs` contracts.  No physical power-cut test was claimed.

### Stages B and C — four independent design pre-mortems and reconciliation

Four fresh Terra/xhigh reviewers attacked the design independently:

| Session / specialty | Report SHA-256 | Concrete result |
|---|---|---|
| `01a080da-06b4-7932-bc55-4984cee7ce28`, filesystem/Btrfs | `f9b500b1dd2528c1e3ed23243ab0182a175b1930adf2fc7918cc9f01744edece` | Fresh-view class accepted; required a dedicated literal-dot ABI, immutable directory comparison and post-bootstrap baseline; same-UID replacement remained an accepted out-of-threat caveat. |
| `01a080da-6120-7732-ac05-49bd882e29b7`, SQLite VFS | `e1fd766b0ef7c830117c15a03cd4c4f4993a5849347f5fcabdf8026d5655034f` | Blocked creation-only pending state; required `OPEN_CREATE` treatment for existing opens, WAL intake barrier, DELETE/FULL migration readback, cache-spill/reopen tests and truthful proof-FD close handling. |
| `01a080da-b84d-78b0-9646-b656f42a2913`, attachment recovery/GC | `9b2d72f3fbd745fe53d37a447a45c583c9ac0fe2d5f8cfe1553e65721f934d03` | Blocked source-first cross-directory barriers and incomplete exchange-state recovery; required destination-first persistence, total exchange/content classification and rowless-cleanup sealing. |
| `01a080df-ff7f-7183-a3ce-65f308cafe01`, root/shutdown/lifetime | `7c5317309b6c9d9656b18b01354a995568bbc63eb14d8facb11a64597567d2be` | Blocked exceptional shared-close tasks and the outer qasync cancellation hole; required data-only drivers, complete FD ownership, terminal pinning and production bridge-path tests. |

The same Stage A architect performed the one permitted reconciliation.  Its
`STAGE_C_SOL_RECONCILIATION.md` SHA-256 is
`6cc9204bc63d0ed2d1f703f942f7195cff9b0fb58b05528fc227d228448daeda`.
The supervisor independently classified every Terra item in
`STAGE_C_SUPERVISOR_ADJUDICATION.md`, SHA-256
`2a9353fbefb7bc002599bf4ce72d8fce248bf6e4fbebbd2575c3f7a37124e677`,
and found no unresolved blocker in Btrfs freshness, attachment ordering,
rollback durability, cross-filesystem authorization, recovery intent,
containment or lifecycle.  No schema or migration revision was authorized.

### Stage D — sole implementation writer

Fresh Sol/xhigh sole-writer session
`01a080f6-daec-7132-8da1-379d64ebb032` implemented only the reconciled
architecture.  Its initial writer report SHA-256 is
`b13a04698035b0e3bb83078c02bf7d906717aef263cc38e20a2b863e1d3782f0`.
The same writer corrected one Stage E compatibility omission in scalar
native-close error classification; that addendum SHA-256 is
`11d58cc8f2e73728c38e52ebf39d83f7bfb8359b0c50e0b497a9583dcd692b12`.
No second implementation writer participated.

The final report-excluding repair file set is exactly:

```text
src/bots5/bootstrap/desktop.py
src/bots5/core/application.py
src/bots5/desktop/session.py
src/bots5/infrastructure/attachments.py
src/bots5/infrastructure/data_root_authority.py
src/bots5/infrastructure/native/rooted_sqlite_vfs.c
src/bots5/infrastructure/persistence/migration_runner.py
src/bots5/infrastructure/persistence/sqlite.py
src/bots5/infrastructure/rooted_sqlite_vfs.py
tests/test_phase4.py
tests/test_phase6_context_attachments.py
```

This implementation report is the twelfth modified tracked path.  There are no
untracked repository paths and the index is empty.  Migrations `0001` through
`0009`, schema definitions, context code and provider code are unchanged.

### Stage E — deterministic validation

The pre-review Stage E supervisor record SHA-256 is
`a76851debf5e675518f0f28d6030707cd50739852f3704391a3bbbc22336980e`.
It records repeated Btrfs recovery passes plus tmpfs controls, actual
fresh-inventory observation, publication/GC/capture sync fault and second-fault
recovery, rollback-journal/main-write/delete ordering, database deletion
authorization fences, descriptor-leak/ownership tests, application/runtime
close replay, migration/restart, concurrency, schema/raw-DML, context/wire,
Phase 1-5 and prior Phase 6 preservation.

After the narrow scalar-classification correction, the complete 833-test
candidate aggregate was 832 passed, one deliberately opt-in local-provider
test skipped, and zero failed.  Native `-Wall -Wextra -Werror`, Python
compilation, migration immutability and `git diff --check` all passed.

### Stage F — four fresh post-build reviews and one bounded repair batch

Four different fresh Terra/xhigh reviewers inspected the implementation:

| Session / specialty | Initial disposition | Report SHA-256 | Supervisor disposition |
|---|---|---|---|
| `01a081d0-5464-7000-92da-974e6ea32fa3`, filesystem/Btrfs | BLOCKED | `72d370e0cf4e15e967f7293374f54672f49391be95e9c315abd06c1236b3078e` | Accepted: failed pre-physical close was not strongly pinned; direct fresh view overlapped lifecycle finding. |
| `01a081d0-cfbb-7893-b8fd-452efe8a0334`, SQLite VFS | BLOCKED | `0462c805fe8bea4e83a28ae1f7e3186871d5e5ae5e3afbec4dfaab127b509a84` | Accepted: pre-registration native cleanup close uncertainty could escape `UNKNOWN`. |
| `01a081d1-4454-7d03-bd32-d8975a8b70f3`, attachment recovery/GC | PASS WITH CAVEATS | `847022236a44bb1fbed531807fbe06e5a5c90d6891fc6f9f7e623baf49b86310` | No repair; retained the explicit no-physical-power-loss evidence limit. |
| `01a081d9-8a7a-7cc1-adc9-a56548020f22`, lifecycle/collateral | BLOCKED | `f7006d2e62a9ff4ae54dbfba527dbe64eafc4f779a2805ff79682fbbe73e79f0` | Accepted: direct fresh-view admission during `CLOSING`; accepted real bridge-path test strengthening. |

Supervisor adjudication SHA-256
`0ed7c7e89f76fc383f1d7965c29621bd3c35ce6b2f98f3305e27dd27813cace0`
classified these as narrow omissions inside the accepted repair architecture.
The same sole writer performed the campaign's one permitted Stage F repair
batch: strong-pin every failed close with retained authority, account for every
native pre-registration close as `RELEASED` or `UNKNOWN`, serialize direct
fresh-view admission with close, and cover the real session/bridge qasync path.
Writer batch report SHA-256:
`f7360ed62d932eb08573428192e2bd93a96caf4fa4310442487a62b5b18abc4e`.

Independent post-batch validation passed 18/18 focused R1-R4 tests.  The final
complete aggregate collected 851 tests: the short-path Btrfs shard passed 849,
skipped the one deliberately opt-in local Qwen test, and failed zero; the
explicit tmpfs production fresh-inventory node passed 1/1.  Aggregate:
**850 passed, 1 skipped, 0 failed**.  Native warning-as-error build, compileall,
migration immutability and diff hygiene remained green.  The post-repair
validation record SHA-256 is
`a56d770656e247f2acf559d92280a4d21241e2b23a39c6ca1e60ec89c9ae8e8f`.

The three initially blocking reviewers then issued targeted closure addenda:

| Specialty | Closure | Addendum SHA-256 |
|---|---|---|
| Filesystem/Btrfs | PASS | `4acbc261a1ad644ca161203bb8f79a116394aa7243efe9a8c17e035be83dddad` |
| SQLite VFS | PASS | `8352e9d5b85bc0beca9cc1ebb8a4c105a158e81dacbe689407bd9be7bd8e68ba` |
| Lifecycle/collateral | PASS | `c493abb22899db64a23b3b1155e4d906393cb905e7917c2478392c38ecc02b8d` |

No second repair batch was used or authorized.

### Stage G — local acceptance disposition

The repair affects how successful attachment ingestion and deletion become
durable, but not the attachment bytes delivered to context construction,
context selection/budgeting, provenance, deterministic wire, provider
configuration, or completion seam.  The final deterministic suite preserves
those semantic snapshots.  The prior local Ollama acceptance therefore remains
applicable only to the unchanged end-to-end semantic path and is not reused as
durability evidence.  No new live call was made.  Stage G record SHA-256:
`9e40d3a6dfb710699047503e3b1299614a958c746ea367a2052ac0ce1d242fe3`.

### Exact repair-leg worker ledger

Prior cumulative telemetry remains unchanged at **6 Luna / 57 Terra / 11 Sol**
and retains its documented reconstruction ambiguity.  It is not arithmetically
normalized against stable worker IDs.

The exact new repair leg before the pending Stage I audit used ten workers:

| Stable session ID | Tier | Role | Disposition |
|---|---|---|---|
| `01a080ae-8325-7350-a013-9ab959b2c3d9` | Sol/xhigh | Stage A architect/adjudicator and one Stage C reconciliation | READY FOR IMPLEMENTATION; Stage C accepted |
| `01a080da-06b4-7932-bc55-4984cee7ce28` | Terra/xhigh | Stage B filesystem/Btrfs pre-mortem | Findings resolved/caveated at Stage C |
| `01a080da-6120-7732-ac05-49bd882e29b7` | Terra/xhigh | Stage B SQLite VFS pre-mortem | Findings resolved/caveated at Stage C |
| `01a080da-b84d-78b0-9646-b656f42a2913` | Terra/xhigh | Stage B attachment/GC pre-mortem | Findings resolved/caveated at Stage C |
| `01a080df-ff7f-7183-a3ce-65f308cafe01` | Terra/xhigh | Stage B lifecycle/resource pre-mortem | Findings resolved/caveated at Stage C |
| `01a080f6-daec-7132-8da1-379d64ebb032` | Sol/xhigh | Stage D sole implementation writer and sole bounded repair writer | Implementation complete; one Stage F repair batch |
| `01a081d0-5464-7000-92da-974e6ea32fa3` | Terra/xhigh | Stage F filesystem/Btrfs review | Initial BLOCKED; closure addendum PASS |
| `01a081d0-cfbb-7893-b8fd-452efe8a0334` | Terra/xhigh | Stage F SQLite VFS review | Initial BLOCKED; closure addendum PASS |
| `01a081d1-4454-7d03-bd32-d8975a8b70f3` | Terra/xhigh | Stage F attachment/GC review | PASS WITH CAVEATS |
| `01a081d9-8a7a-7cc1-adc9-a56548020f22` | Terra/xhigh | Stage F lifecycle/collateral review | Initial BLOCKED; closure addendum PASS |

The automatic approval reviewer was infrastructure, not a campaign worker, and
is excluded from this ledger.  The pending fresh Stage I Sol/xhigh auditor will
be appended to the external repair ledger; this report is sealed before that
audit so the auditor reviews the exact report it was given.

### Repaired pre-Astra candidate seal and remaining limits

The report-excluding repair inventory is the exact eleven-path file set above.
The candidate seal is the SHA-256 of those path-ordered `sha256sum` lines:

`c83b33ce557723605051fcd085fc6324a24ddd0f9a485c032fc607ead344d6b3`

No implementation code, schema, migration, test, or this implementation report
may change after this Stage H seal.  The implementation-report SHA-256 is
recorded externally after this append.

No physical machine power cut was performed.  Evidence establishes syscall
ordering, retryable intermediate states and deterministic persistence-model
outcomes, not behavior below a truthful local `fsync` contract.  Correctness
continues to assume supported Linux `openat2`, `renameat2`, `statx`, abstract
AF_UNIX, stable mount identity, atomic supported rename operations, truthful
local Btrfs/tmpfs and one shared SQLite instance.  Malicious same-UID rewriting,
descriptor theft, privileged mount/namespace manipulation, lying storage,
network filesystems and arbitrary memory corruption remain out of scope.  The
generic/local HTTP exact-token-adapter caveat remains unchanged.

Prohibited-scope verification is exact: no commit, staging, push, fetch, pull,
checkout, switch, ref mutation, branch creation/deletion/rename, provider or
credential access, Organisational Memory operation, Phase 7 work, product-policy
weakening, schema/migration revision, new provider, search, Android, daemon,
remote-client, tool, plugin, RAG, scheduling or campaign-UI work occurred.

### Stage H reconciliation and immutable handoff

Stage H resumed from the human-adjudicated archaeology checkpoint rather than
rerunning any completed campaign stage.  Before this report-only reconciliation,
the checkout was reverified as branch `main`, with `HEAD` and local `main` at
`89fb52979c67b8b1c822a7efb3c5a67946611bdf`, `origin/main` at
`c9efdb374e37be94bb9ab68abd45e8ed718c3437`, divergence ahead one and behind
zero, an empty index, exactly twelve modified tracked paths, and no untracked
repository paths.  The implementation report's pre-reconciliation SHA-256 was
`77e32c22206c6c9fb04f7a1c3fc3f022fdaf1e3ba513cf792efee4a54ebbbb73`.

The prior mixed-provenance Stage H text and interrupted linked-writer state were
treated as historical execution evidence and reconciled in place.  The original
Phase 6 campaign history remains intact.  This report's final sections are the
authoritative account of the reopened repair leg; earlier candidate-specific
BLOCKED, PASS and PASS WITH CAVEATS records remain preserved as the outcomes of
their exact candidates rather than being rewritten into current authority.

No implementation, schema, migration or test file changed during this Stage H
resume.  The same exact eleven report-excluding paths remain the repaired
candidate.  Their binary Git diff SHA-256 remains
`364ca190cb438f05d75155e565a841549f84abaf046290430f48b5ff2367324e`,
and their path-ordered `sha256sum` seal remains
`c83b33ce557723605051fcd085fc6324a24ddd0f9a485c032fc607ead344d6b3`.
Only this implementation report changed during reconciliation.  Its final
SHA-256 is necessarily recorded by the external Stage H completion record,
because a file cannot contain its own ordinary SHA-256 without changing it.

Stages A through G were not rerun.  No implementation or review worker was
spawned during Stage H.  Stage I's exactly-one fresh Sol/xhigh audit and Stage
J's immutable Astra package remain unexecuted.  The repaired implementation and
test candidate is now frozen for Stage I; this statement is not a final Phase 6
closure claim and does not substitute for the required external Astra audit.

# N1-N3 durability and explicit-legacy repair campaign

Date: 2026-09-09 Australia/Melbourne.

The previous external Astra review of repaired candidate seal
`c83b33ce557723605051fcd085fc6324a24ddd0f9a485c032fc607ead344d6b3`
returned **BLOCKED**.  Its exact report SHA-256 is
`bc3579765ea76cd0097a1e604cf41a17086c89244f20455116627ee41fee61f5`,
its evidence ZIP SHA-256 is
`77c7559f0e98da5e57cf5e0c0d504d422dcf66c1c12e140626c40ca2963884ca`,
and the immutable bundle it audited has SHA-256
`b78907ff647cb0fe4c266a4f88358f97121bb555107dd6b18ce5c223f03d61ee`.
Those original artifacts were located and verified by hash before this repair
campaign.  Their findings were evidence; Mick's subsequent directive supplied
the human authority for this bounded repair.

The campaign accepted N1 and N2 as Phase 6 implementation blockers under the
shared invariant that a failed mandatory durability obligation remains
represented until it successfully completes or authoritative recovery
reconciles it.  N1 and N2 remained separate state machines.  Mick also resolved
N3 product policy: `local_openai` is explicitly
`LEGACY_PHASE3_LOCAL_OPENAI`, a Phase-3 desktop-testing/compatibility mode with
`phase6_enabled = false`.  Planless plain-text local sends remain intentional;
Phase 6 attachment selection fails before dispatch; configured Phase 6 sends
cannot fall through to the legacy path; and no new provider or accounting
adapter was authorized or added.

## Starting identity and blocked Stage F checkpoint

The N1-N3 campaign began from branch `main`, with `HEAD` and local `main` at
`89fb52979c67b8b1c822a7efb3c5a67946611bdf`, `origin/main` at
`c9efdb374e37be94bb9ab68abd45e8ed718c3437`, ahead one and behind zero, an
empty index, exactly twelve modified tracked paths, and no untracked repository
paths.  Its starting candidate seal, source/test binary-diff SHA-256, and this
report's SHA-256 were respectively:

- `c83b33ce557723605051fcd085fc6324a24ddd0f9a485c032fc607ead344d6b3`;
- `364ca190cb438f05d75155e565a841549f84abaf046290430f48b5ff2367324e`;
- `a8c9ff8a876c3d9f594ed7cf6b35c239b3cb394261292cc0c5c227ed0262242c`.

The first N1-N3 implementation leg stopped after its only then-authorized Luna
repair batch because a targeted Terra rereview found two remaining T1/K0
implementation omissions.  The exact blocked checkpoint is
`STAGE_F_FINAL_SUPERVISOR_BLOCKED_CHECKPOINT.md`, SHA-256
`4b73fd1e1e95b377eaca72f00a8fc4e52c31d437e6c18f94943c8fa660b5973f`.
At that checkpoint the report-excluding seal was
`8b8aa4a8423b17415ca57f43180b2a88ab2e5b0c829eb9b14b41e5b11643a5a6`,
the source/test binary-diff SHA-256 was
`54a743eea8bff5da97a7ce1ef8a0d423961e0eda11f9cbbda7a58cdec1bb70a3`,
the index was empty, and the worktree contained exactly seventeen modified
tracked paths and no untracked repository paths.

## Accepted design and first implementation leg

Exactly one fresh Sol/xhigh architect produced the bounded N1/N2/N3 design,
SHA-256
`7aa9ed9f05b1828d83639c9721fd258e2e6e020acc6460e915f51f569b34d4f8`.
Four fresh independent Terra/xhigh Stage B pre-mortems all returned PASS WITH
CAVEATS:

| Specialty | Report SHA-256 |
|---|---|
| SQLite commit uncertainty/admission | `46d2ac801fe4fc849e40b8d688ea653dc2a20c0b811755e86cf60bfbb78d19d7` |
| bootstrap/filesystem durability | `26a8ca47f120f128d61d70b7265ff807705413477a87632e717f982ba4cb6182` |
| attachment lifecycle/recovery | `7367c149af3420f10454de78bbdf69b7683a02e418697a8a12f8ea45414049d3` |
| local OpenAI Phase 3-6 policy compatibility | `4f417bf4a18c3c980b94954743c2a2780b2597b115e81e80da7f623ef3e69ed5` |

The supervisor's Stage C design gate passed with no Sol reconciliation loop;
its report SHA-256 is
`391d7549427b5780d1ffb51fb24bde67b1299da5c829ccb8073f4793f7489eb5`.
One Luna was the sole repository writer.  Four fresh Stage F Terra reviewers
then returned, by specialty, N1 BLOCKED, N2 PASS WITH CAVEATS, lifecycle BLOCKED
only on N3 authentication-tuple equality, and N3 BLOCKED on the same equality.
The same Luna performed one bounded repair batch, report SHA-256
`e06eda3fbe78aff3a5526da12a0704d504ac5525454ef8c7f73ffce7a0f722bc`.
The N3 reviewer subsequently returned PASS.  The N1 reviewer instead found:

1. capture could succeed before database checkout, after which a checkout
   failure skipped rowless-capture cleanup while ordinary READY admission
   remained open; and
2. an abnormal existing-row T1 rollback could fail once, then be retried
   successfully by the outer handler before poison became authoritative.

The targeted N1 rereview SHA-256 is
`9a78337e4b5e9a3243e3a2e1afb1e69a32f49b993b7b1a29b5d15f982fa246f3`.
This was the exact Stage F block continued below.  N2 and N3 were not reopened.

## Final Stage F continuation and two bounded K0 fixes

Mick authorized exactly one additional bounded Luna repository-writer batch
for the two remaining N1 omissions.  Supervisor reconstruction found no
material conflict among the accepted reports.  The exact F1 implementation
map SHA-256 is
`c9bb2acca25bb8cfd48627a734211d3f31c1a84b425e65682879225b15845532`.

The already-counted Stage D Luna identity performed that one additional repair
batch; no second writer or architecture worker was introduced.  It changed
only `src/bots5/infrastructure/persistence/sqlite.py` and
`tests/test_phase6_context_attachments.py`:

1. after successful private capture followed by database checkout failure,
   the attributable capture is now discarded through the existing
   descriptor-relative unlink plus capture-directory barrier; if that cleanup
   fails, store and authority poison before a restart-required error is raised;
2. the abnormal existing-row T1 branch now uses the accepted rollback helper,
   so the first rollback exception makes poison authoritative before any outer
   handler can retry rollback or discard the captured evidence.

Three direct regressions cover clean checkout-failure cleanup and same-process
retry, checkout-failure cleanup uncertainty plus poison/restart reconciliation,
and irreversible poison after the first abnormal-row rollback failure.  The
Luna repair report SHA-256 is
`ff8dd97332622670ae077be61982f0d066b0cd607c76e2a459e123f3e70bbce0`.
The repaired `sqlite.py` and focused test file SHA-256 values are respectively
`41f39b3878b91f1897f731b5d6a80cd3d898a20f5a4197fece7255946a9ede3f`
and
`f51f25c4b549270764ae1ad39b6fa07626ff81485d33a63e2c5382c50701a63b`.

## Final deterministic validation

Before the expensive suite, the exact unsandboxed Python 3.14 `/tmp` tmpfs
environment passed the required host preflight: 3,645,640,704 free bytes,
3,854,033 free inodes, file and containing-directory fsync, permissions,
abstract AF_UNIX `SOCK_SEQPACKET` bind, and a real
`DataRootAuthority.acquire()` / store / attachment / READY / close smoke path.
Preflight log SHA-256:
`4b6209171eb184f563b4a6d683c9506738b810acf579a6e1f60e45f185443278`.
The earlier ENOSPC and sandbox-denied abstract-AF_UNIX invocations remained
discarded host diagnostics and were not repeated as expensive discovery.

Current-candidate deterministic evidence is:

| Evidence | Result | JUnit/artifact SHA-256 |
|---|---:|---|
| exact three final K0 regressions | 3 passed | `6af7edcd1ff2d7671ffd81d457df26675528f9e3593dc607e5ee8e063f470e31` |
| accepted N1/N2/N3 repository selection | 24 passed | `b0c1b9de8d6dd47db2692bbb1dd79105e0ade988227aed34ab87ca45fae423d0` |
| complete N1 T1-T9 pre/post matrix | 18 passed, 12 deselected | `5c37b764c4197eee72fbf4b934660367f6dce3a0f771451b74c534792a06d8f7` |
| original native Astra T2/T7 event-8 probes | 2 passed, 28 deselected | `e423950901f29256f3908b2065762eeeed6691a19d9750805b30f253454ff3a1` |
| external N1/N2 matrix including native probes | 30 passed | `73d904252492a89d6a8f55daac71e0e7d909c4de21ab97e3baf683de5f8d7d07` |
| strict-built VFS exact load/ownership | 2 passed | `9875f79d079f127248cb09c9a09e78c7739219bf9a04bfdd3b193773cee34d89` |
| committed migration bytes | 1 passed | `57a76af8b249d7e8d7278e33fa7c93fed7f59ce0dec8629d5ecec27dac10b7a2` |
| authoritative complete suite | 875 passed, 1 opt-in local skip, 0 failed | `dd8ee986136bf811813713e2c0270f7590dcf1dc2eaf32a225dd09362bd26bdd` |

The authoritative full suite ran for 2,636.86 seconds; raw log SHA-256:
`07d45324c965c509f03635d20a9ff7570f2d1d72a9c7fcc129723897aee6d23f`.
Its sole skip was the Stage G local acceptance node.  Focused shards overlap
each other and the complete suite and are not added to the unique repository
test total.  Strict C11 `-Wall -Wextra -Werror`, compileall, migration
immutability, `git diff --check`, empty-index and no-untracked checks all
passed.  The strict native binary SHA-256 is
`0a81b05fac80ec401572325cbeb61c348cbe9fe28c87836c24f56ed09cb92757`.

One initial outside-test `--collect-only` command omitted the repository root
from `PYTHONPATH` and collected no tests; one concurrent N1 shard produced no
terminal JUnit.  Neither is candidate evidence.  A directly supervised
replacement produced the recorded 18-pass matrix.  The complete Stage F3
record SHA-256 is
`4ce24d1e7a485f256a57e44ce8be9628f0042eb6eb93092d18d961eb09e62843`.

## Fresh final Stage F N1 review

Exactly one new fresh independent Terra/xhigh reviewer attempted to falsify
capture ownership after checkout failure, first-rollback poison,
outer-handler/finally interactions, same-process admission, restart recovery,
sibling T1/T2 K0 paths, and preservation of T1-T9.  It inspected source and raw
Stage F3 artifacts and independently ran production-path probes.  Its exact
disposition was **PASS**, with no caveat requiring mutation.  Report SHA-256:
`f68b5b40b65e7e1e77c1c90d175ac38669bceaf3e0503b5c1b8c9e5437f6b58f`.

N1 is therefore repaired through the authorized Stage F gate.  N2 remains
repaired/review-clean and N3 remains repaired/targeted-rereview PASS; focused
regressions for both remained green after the N1-only change.

## Stage G local Ollama compatibility acceptance

The exact preferred local endpoint and model were available.  The required
`tests.test_phase3_local_qwen::test_opt_in_local_qwen_acceptance_path` node made
exactly one generation request to
`http://192.168.50.223:11434/v1` using `qwen3.5:35b-a3b`; no request was
retried and no public fallback or credential was used.  Result: exit 0,
**1 passed**, 0 failed, 0 skipped in 105.87 seconds.  JUnit SHA-256:
`fc5e2d1203acf611d51a8bcfad2c3f3a474061076d33a94dc038d89a79103d8d`.

The same runtime reported `LEGACY_PHASE3_LOCAL_OPENAI`,
`phase6_enabled = false`, and the visible label `Phase 3 legacy (Phase 6
disabled)`.  The persisted attempt was complete with finish reason `stop`, the
returned model was `qwen3.5:35b-a3b`, a provider request ID was present, and
the assistant returned a non-empty short sentence.  Raw `context_plans` count
was zero; Phase 6 connection/model attribution and `snapshot_version` were
absent.  Ollama exposed no prompt, completion, reasoning or total token counts
and no cost; those values are recorded as null rather than inferred.  The live
request selected no attachment.  Deterministic Stage F3 evidence remains the
proof for pre-dispatch attachment rejection and provider/base/auth mismatch.
The complete Stage G record SHA-256 is
`3eaa31e3f6a0ece793c967f5d5ebfe8b7ed2df92364cf124edee9e2151531b61`.

## Current inventory, telemetry, seal, and remaining authority

The final report-excluding candidate inventory is exactly these sixteen
modified tracked paths:

```text
README.md
docs/LINUX_V0_1_DESIGN.md
src/bots5/bootstrap/desktop.py
src/bots5/core/application.py
src/bots5/core/ports.py
src/bots5/desktop/profile.py
src/bots5/desktop/session.py
src/bots5/infrastructure/attachments.py
src/bots5/infrastructure/data_root_authority.py
src/bots5/infrastructure/native/rooted_sqlite_vfs.c
src/bots5/infrastructure/persistence/migration_runner.py
src/bots5/infrastructure/persistence/sqlite.py
src/bots5/infrastructure/rooted_sqlite_vfs.py
tests/test_phase3_generation.py
tests/test_phase4.py
tests/test_phase6_context_attachments.py
```

This implementation report is the seventeenth modified tracked path.  The
index is empty and there are no untracked repository paths.  No schema,
migration, rooted-VFS architecture, attachment-authority ownership,
content-addressed storage, GC architecture, context/provenance architecture,
new provider, or accounting adapter was added or redesigned.

Historical source telemetry remains exactly **6 Luna / 57 Terra / 11 Sol**,
including its recorded reconstruction ambiguity.  The completed prior N1-N3
authorization added **1 Luna / 8 Terra / 1 Sol**, for the blocked-checkpoint
arithmetic total **7 Luna / 65 Terra / 12 Sol**.  This continuation then used
one additional bounded repair turn by that same already-counted Luna stable
worker identity and dispatched exactly one new fresh Terra/xhigh N1 reviewer.
Accordingly, pre-Stage-I stable-worker telemetry is **7 Luna / 66 Terra / 12
Sol**; the repair-turn ledger separately records one Luna writer batch so the
reused identity is not silently counted twice.  No supervisor activity is
counted as a model worker.

The exact final report-excluding implementation seal is the SHA-256 of the
path-sorted `sha256sum` lines for the sixteen paths above:

`34ce80391887b5991d0801516c0671edf1e3da2b5476ee89b9dda8095353afad`

The exact final source/test binary Git diff SHA-256 is:

`3de16b6f176363b7371e347eb9bc9584e225bccd8c14c427147a9ab0675a2384`

This report's final SHA-256 is recorded externally after this report-only
append because the file cannot contain its own ordinary digest without
changing it.  The report-excluding candidate is now immutable.  Stage I's
exactly one fresh Sol/xhigh closure audit remains a read-only independent gate;
Stage J packaging remains contingent on a non-blocking Stage I disposition.
Neither Stage I nor an Astra package constitutes Mick's substantive Phase 6
acceptance.  No external Astra audit has been dispatched.

No commit, amend, push, fetch, pull, merge, rebase, reset, checkout, switch,
stage/index mutation, tag, branch or ref mutation occurred.  Organisational
Memory and Phase 7 were untouched.  No public provider or public credential was
contacted, read, requested or inferred.

# Stage I close-failure repair and final T7 K1 evidence completion

Date: 2026-09-09 Australia/Melbourne.

This section records the narrow continuation from the later Stage I production
block through the final deterministic T7 K1 evidence repair. It is a technical
candidate record, not Mick's acceptance, a commit/publication record, or an
external Astra result.

## Opening identity and prior Stage I block

The continuation remained in
`/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1` on branch
`main`. `HEAD` and local `main` remained
`89fb52979c67b8b1c822a7efb3c5a67946611bdf`; the locally recorded
`origin/main` remained `c9efdb374e37be94bb9ab68abd45e8ed718c3437`, with
`origin/main...main = 0 1`. The index was empty, exactly seventeen tracked
paths were modified, no repository path was untracked, and
`git diff --check` exited 0.

The earlier fresh Stage I Sol/xhigh auditor had reproduced a production N1
blocker: after a successful attachment transaction, raw
`Connection.close()` uncertainty could escape without poisoning store and data
root authority. Its audit SHA-256 was
`e4fac959d72d395d2690dde5c64445a825cf6f5ef3f0198fb98bce18b86e1e38`.
The controlling fresh Sol repair architecture, SHA-256
`82bb2364590eb281739757b7c52e9b30c6bde7597ce8a7ca453b40b06e444004`,
required exactly-one-close classification, store/authority poison before
unwind, no fabricated check-in/release, no post-close T7 fence or filesystem
recovery, and restart-only reconciliation.

One bounded Luna production writer then routed T1/T2, T4, T7 and
attachment-bearing T5/T6 connection terminalisation through the shared
single-attempt `_close_attachment_connection` primitive, with matching tests.
Its report SHA-256 was
`6a82473d8ea9aecd52073f1c4cf13ded1fc822143f871036bdcaa2b828c36a45`.
No later continuation changed that production implementation.

## Two evidence-completion blocks and final narrow repair

The first supervisor evidence review blocked the production repair candidate
on incomplete mandatory regression proof: close-after-prior-poison, explicit
T2 related/unrelated admission and tree preservation, exact T1 capture absence,
T5/T6 subsequent command rejection, and direct T7 post-close no-fence/no-
recovery evidence. Its checkpoint SHA-256 was
`305f61f26e870c68d7ff0259460e41072e5c96c0eed59675a74756e036f4a0f9`.

A subsequent test-completion leg added the missing close-after-prior-poison,
T2, T1 and T5/T6 evidence. The next supervisor gate found exactly one remaining
deterministic omission: T7 K1 still inferred no later filesystem work from
state but did not directly instrument
`database_durability_fence()` and `_recover_deleting_blob()`. The resulting
test-completion baseline was:

- report-excluding seal: `4161d357611e64d7a0244c70f32f8870ef25b0d89f16461a7cb8c1e37ca94013`;
- source/test binary diff: `3c4caa585b86179982b71dbad2b2b100d50276c23cbe697bac356cb904af339f`;
- complete binary diff: `08811ab43f60645a4037ec56e75f772a5e768a84e2ea5c78336a4dc3244ae1a5`;
- this report's SHA-256: `74266a7fc561f60663d6678e696e4aafa986d4a760122c8f17eb9d02c32029ca`.

Exactly one final Luna test-only writer then changed only
`tests/test_phase6_context_attachments.py`, adding eighteen lines and removing
none relative to that baseline. The T7 K1 branch now wraps the actual
`authority.database_durability_fence` and
`store._recover_deleting_blob` instance methods, counts any invocation, and
directly asserts both counters are zero immediately after the selected
post-real `Connection.close()` failure. The earlier T7 K1 one-close, poison,
authority, admission, durable `deleting` row/`gc_id`, canonical-object,
empty-GC-namespace and restart assertions remain unchanged. No production
change was required. The writer report SHA-256 is
`c8ea7b2d614adffcc2e9fc2ad7fa7896b2cbff856fbc67cd62a74b9c031cc978`.

The supervisor's ten-point evidence gate answered YES to every question. The
exact strengthened T7 K1 and K0 host rerun passed 2/2. No existing assertion
was weakened or removed, and the two instrumented hooks are the real methods
that production `gc_attachments()` would call only after the classified close
returns normally.

## Fresh deterministic validation and Terra rereview

The initial host preflight correctly rejected `/tmp` as too close to prior
suite capacity at 1,287,430,144 free bytes. The broad suite instead used the
clean host tmpfs at `/dev/shm`, where preflight passed with 16,691,363,840 free
bytes, 4,094,986 free inodes, file and directory fsync, permissions, abstract
AF_UNIX bind, and a real attachment-authority/store/ingest/READY/close smoke.

Fresh current-candidate evidence is:

| Evidence | Result | JUnit SHA-256 |
|---|---:|---|
| complete current N1 focused selection | 25 passed | `69d9b739b01479b926e5987fa0f6950b57f1812ed9518fb462cd50db8a242247` |
| complete live-close matrix including close-after-prior-poison | 16 passed | `43f479e921426d19a6646e0001fe484e9a7e6f1122f5d8e9ea2bb0cc7d217b63` |
| exact T2 post-real and pre-release/process-exit cases | 2 passed | `1a8bc100063fa94ca62b5250e7ef55324b5ed930a990de3223d9a856cb7aa907` |
| prior accepted K0 selection | 7 passed | `31c04534846106e1c69c100caa1a24f258588684ba1af7a8d6b9c4871aa4e1c4` |
| complete external T1-T9 pre/post matrix | 18 passed, 12 deselected | `aa23d1aa6069e94e7796c21826aa4b4aeb003999803596164c7853a17330f3f9` |
| original native Astra T2/T7 event-8 probes | 2 passed, 28 deselected | `76693b80645a300affba3662fb43b67b6a842f3d080223106feec7a919c333ca` |
| complete external N1/N2 matrix | 30 passed | `23b9f223a474045f082858a2bd4298b2efc44af43fe0881f71c0336e2a4e58cd` |
| expanded accepted N1/N2/N3 repository selection | 40 passed | `b4c2a2c6aa3134b4adcd8e53dbd9cf4988cf7d939ddcf6e286e1dbb975bd8150` |
| strict-built VFS load/ownership | 2 passed | `a7fab46ed44798313f5abab0f32a822df037b94e74ec1b25a6828d4e6c37de9c` |
| committed migration bytes | 1 passed | `0b950e35bed4270ab592e43464a1b3489d712a47a403358325c505b7f9046be1` |
| authoritative complete suite | 891 passed, 1 opt-in local skip, 0 failures/errors | `5f5cbc6bac415f76f3782ca7d3f970e20f053edd75679ffaf659bb31dabef071` |

The authoritative suite recorded 892 tests and 3515.736 seconds. Focused
shards overlap and are not added to that unique suite total. Strict C11
`-Wall -Wextra -Werror`, exact VFS load/ownership, `py_compile`,
`compileall -q src`, migration immutability, empty migration diff,
`git diff --check`, empty index and zero-untracked checks passed. The strict
native binary SHA-256 remained
`0a81b05fac80ec401572325cbeb61c348cbe9fe28c87836c24f56ed09cb92757`.
The full supervisor evidence report SHA-256 is
`34dbacea915a47eb809547b1c80e2bdc823e3b958fa5ab120801c84298b111f4`.

Exactly one fresh Terra/xhigh lifecycle rereviewer then returned **PASS**. It
independently reran T7 K1, T7 K0, close-after-prior-poison, T2 post-real,
representative T1 K1/K0, T5 send/edit and T6 regeneration: nine terminal
passes. It independently verified both T7 K1 hook counts are zero on the real
production path and resealed the candidate unchanged. Its report SHA-256 is
`f98d3f2d9fa196f9aca01ba8ca420c5223eeab16950f9548a5dce2b66eb65fad`.

Stage G remained previously passed and was not rerun. No provider call was
made.

## Telemetry reconciliation

The supplied latest checkpoint must remain preserved exactly as reported:
**9 Luna / 66 Terra / 12 Sol**. Existing source reports also preserve the
historical base **6 Luna / 57 Terra / 11 Sol**, the prior N1-N3 addition
**1 Luna / 8 Terra / 1 Sol**, and therefore **7 Luna / 65 Terra / 12 Sol** at
that Stage F checkpoint. The next source checkpoint records one fresh Stage F4
Terra and one fresh Stage I Sol and explicitly computes **7 Luna / 66 Terra /
13 Sol**. Two later test-evidence Luna dispatches explain the supplied latest
checkpoint's Luna increase from 7 to 9; no later Terra or Sol had then been
dispatched.

The Sol discrepancy is therefore deterministically located: the supplied
latest **12 Sol** carried forward the pre-Stage-I report count and omitted the
fresh Stage I Sol auditor that the Stage I source checkpoint explicitly counts
as the thirteenth stable Sol identity. Historical reports are not rewritten.
Both arithmetic lines remain visible rather than silently normalised:

- supplied-checkpoint arithmetic before this continuation: **9 / 66 / 12**;
- source-report stable-worker arithmetic before this continuation:
  **9 / 66 / 13**.

This continuation has so far dispatched exactly **1 Luna / 1 Terra / 0 Sol**:
the sole test writer and the sole fresh lifecycle rereviewer. Consequently the
parallel current arithmetic is **10 / 67 / 12** from the supplied checkpoint,
or **10 / 67 / 13** from the source-report stable-worker ledger. Supervisor
activity is not counted. The required fresh closure Sol and contingent Stage J
Terra packager are not counted here because neither had yet been dispatched at
this report/seal checkpoint.

## Current candidate seal and boundaries

Before this report-only append, the exact report-excluding implementation seal
was:

`c4c7be71872ca348ceffbb194bb1930b7bbaabbb529a42ea146e1ccbd2054d2e`

The source/test binary diff SHA-256 was:

`2a1d992d33aaef6ed3223abaaefede21171965ced8da143efa5e5fb4786e0930`

The complete binary diff SHA-256 was
`38e40650726db63ed36fa1cd96b2a758ece8f65bb4696ef99a3e135df5ef57c5`
and this report's SHA-256 was
`74266a7fc561f60663d6678e696e4aafa986d4a760122c8f17eb9d02c32029ca`.
This report-only append necessarily changes the latter two values; their new
hashes are recorded externally in the report/seal checkpoint. The
report-excluding seal and source/test binary diff remain unchanged.

No production/test mutation occurred after the final Luna test edit. No
commit, amend, staging/index mutation, push, fetch, pull, merge, rebase, reset,
checkout, switch, tag, branch or ref mutation occurred. No Phase 7, public
provider or public credential work occurred. External Astra was not
dispatched.

The prior evidence-completion checkpoint already disclosed one prohibited
read-only Organisational Memory query. This continuation made one additional
read-only Organisational Memory query before the prohibition became visible
while the pasted brief was being read. Neither query updated Organisational
Memory, neither supplied campaign authority, and no further query occurred.
The no-query condition therefore cannot be truthfully claimed; this process
deviation is preserved for Mick and the fresh closure auditor rather than
concealed.

# EF2 concurrent admission linearization repair and Stage J gate

Date: 2026-09-10 Australia/Melbourne.

This section records the single-blocker continuation from the fresh Sol EF2
TOCTOU finding through independent Terra and Sol review.  It does not reopen
EF1 design, repair deferred EF3, authorize Stage G, constitute Mick's Phase 6
acceptance, or authorize external Astra dispatch.

## Opening identity and exact blocker reproduction

Before the first edit, the repository root was exactly
`/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1` on branch
`main`.  `HEAD` and local `main` were
`89fb52979c67b8b1c822a7efb3c5a67946611bdf`; `origin/main` was
`c9efdb374e37be94bb9ab68abd45e8ed718c3437`; and
`origin/main...HEAD` was `0 1`.  The index was empty, exactly seventeen
tracked paths were modified, no repository path was untracked, and
`git diff --check` exited 0.

The supplied checkpoint seals were independently reproduced exactly:

- report-excluding implementation seal:
  `dd7631e22ee1f24bab8c2948dae078c6b8fb2ce816d04eb71c68c528db9e7b44`;
- source/test binary diff:
  `3689aa66cb1e80fbab7aa8eed9f725f486613a32b446d528157b110bce5ba0bd`;
- complete binary diff:
  `983056ea624c40c2ab300927993fc61d8eead53f8729db5d30e08603bfade1cb`;
- implementation report:
  `7d413da5dab06e30a7ecbc6c29354a6fe39aae93ba9ff101db303332ddecc61d`.

The original Sol race was then reproduced against those exact live candidate
bytes with deterministic thread barriers and a real rooted-VFS store.  The
`unstage_attachment()` command passed the existing point admission and was
held before its pending-selection mutation.  A competing real
artifact-proof descriptor release was called exactly once, completed the real
close, was classified `UNKNOWN` with `fd=None`, and made authority
`POISONED`.  After releasing the first command, it returned an empty
selection, removed the pending attachment, and advanced the event sequence
from 2 to 3.  The store-local poison flag remained false and a subsequent
database checkout rejected.  This reproduced the reported invalid ordering:
poison was authoritatively visible before the protected in-memory mutation
and publication.

## Bounded architecture decision

The required architecture questions were resolved as follows.

1. `BotsApplication._command_scope()` owns public application command
   lifetime.  Before this repair it called
   `SQLiteAppStateStore.assert_admitting()`, which acquired and immediately
   released a data-root operation.  After this repair the store exposes
   `command_admission()` and the ultimate admission owner is the one
   `DataRootAuthority` for that store.
2. `DataRootAuthority.poison()` owns authoritative poison requests and state
   transitions.
3. Before repair, no synchronization spanned application mutation and event
   publication; the authority operation protected only the check.  The repair
   uses the authority's existing condition and `_active_operations` lifecycle
   for both application leases and poison finalization.
4. `unstage_attachment()` becomes logically committed when its pending-state
   mutation and awaited `pending_attachments_changed` publication have both
   completed.  Both now occur inside one continuous admission lease.
5. Event publication is part of the protected logical effect.  It cannot occur
   after a competing poison transition has become `POISONED`.
6. All public asynchronous application operations use `_tracked_command` and
   therefore the same central scope.  The synchronous `subscribe()` sibling
   explicitly uses the same effect scope.  Stage, unstage, cancellation,
   pending-generation tracking and client-visible events are covered.  The
   internal background generation loop is the one non-command mutator: each
   dispatched, metadata, delta and terminal persistence/tracked-state/event
   effect now takes a fresh bounded effect admission.
7. The older `DataRootAuthority.operation()` remains a scoped authority I/O
   operation, not a point-test masquerading as a command lease.  The new
   `application_operation()` is the bounded command/effect lifetime primitive.
8. This was introduced without a new generic concurrency framework, schema,
   migration, provider, filesystem or event architecture.
9. `poison()` participates in the same condition-protected active-operation
   count.  A competing poison request immediately sets the monotonic pending
   predicate, so every new admission rejects; `POISONED` is published when
   the already-admitted effects drain.  A failure detected by the sole current
   owner still poisons immediately and raises before any success event, which
   preserves EF1 and N1 failure semantics.
10. Application admission linearizes under the authority condition at the
    READY/no-pending check plus active-count increment.  An eventful command
    effect linearizes no later than successful publication inside the lease;
    a non-event effect linearizes at its final protected mutation.  Poison-wins
    linearizes under the same condition before admission.  Command-wins poison
    has a distinct monotonic request point and state-transition point: it
    closes new admission immediately, then transitions to `POISONED` under the
    condition when the winning lease decrements the active count to zero.
11. `EventBus.publish()` delivers to queues and has no application callback
    invocation.  Its separate async lock does not call store admission or
    poison handling.  Task identity is checked in addition to `ContextVar`
    presence, so copied context cannot grant a child the parent's lease.
12. Background provider iteration and waits are outside admission.  A short
    background persistence/publication effect holds admission across its
    event-bus await.  Some already-existing public commands, notably operator
    model refresh, attachment capture and cancellation, keep their command
    lease across their awaited or filesystem work because their revision
    snapshot and eventual mutation/publication form one command-wins effect.
    The lease holds no condition mutex, poison returns after setting pending,
    and all new work rejects immediately; it therefore adds no lock wait cycle.
    Event-bus backpressure is released by subscription/event-bus close and was
    tested under terminal close by both reviewers.
13. Terminal close sets application/authority closing state, refuses new
    commands, and waits on the same active count.  Already-admitted owners can
    finish their exact work during `CLOSING`; close-owner re-entry rejects.
14. Fresh-authority recovery and the only accepted return to `READY` are
    unchanged.
15. EF1 production semantics and recovery were not changed.  They were rerun
    on tmpfs and btrfs by the supervisor and independently by Sol.

The active application admission is task-or-thread owned and reentrant only
for that owner.  It is released in `finally` exactly once.  A child asyncio
task may copy the `ContextVar` value, but owner comparison rejects it.  No
authority condition lock crosses an await.  The active-count lease may span a
command or event await, but poison never waits synchronously for it: poison is
recorded monotonically, new admission closes immediately, and terminal close
uses an independently tested condition wait.

## Repair history, including failed gates

The first bounded implementation added the application lease, held every
public command and `subscribe()` through mutation/publication, and made
connection checkout plus fresh directory observation reject poison-pending
competitors.  It initially deferred a sole owner's own failure poison until
lease exit; the accepted N1 close-after-prior-poison regression exposed that
semantic regression.  The implementation was corrected so a sole current
owner's failure still transitions immediately and unwinds without publishing
a success event.

The first fresh Terra review returned **BLOCKED** despite the then-green EF2
tests.  Its production probe showed that an unadmitted competitor could still
execute `database_durability_fence()` during `READY + poison_pending`, because
that method used only `assert_live()`.  Terra also identified missing focused
evidence for background generation effects.  The bounded repair put runtime
READY/CLOSING durability fences inside `DataRootAuthority.operation()` and
wrapped every background persistence/tracked-state/publication effect in the
same application effect admission, while leaving provider iteration outside.

The first affected rerun after that edit failed all seven selected tests before
their EF2 scenarios: the runtime fence guard had also rejected the legitimate
pre-READY startup handoff in `ACQUIRED`.  The guard was narrowed to runtime
READY/admitted-CLOSING use; the existing ACQUIRED startup handoff remains
explicit and independently tested.  The corrected seven-case selection then
passed.  Terra reran its former fence attack, verified competing rejection,
admitted-owner completion and ACQUIRED startup, executed all eight focused EF2
tests, and ran a real rooted-VFS backpressure/close probe.  Its final bounded
disposition was **PASS WITH CAVEATS**; its only caveats were the expected
uncommitted candidate, Stage G not rerun, and EF3 out of scope.

## Deterministic EF2 evidence

The exact original synchronization probe after repair recorded:

- admitted command: authority `READY`, pending selection unchanged, event
  sequence 2;
- after the one ambiguous real close and before release: authority still
  `READY`, pending selection unchanged, event sequence still 2;
- descriptor claim `UNKNOWN`, `fd=None`, close count exactly one;
- after command completion: result empty, pending selection empty, event
  sequence 3, authority `POISONED`;
- later database checkout rejected.

The repository races prove both orderings directly.  Command-wins holds
authority at `READY + poison_pending` through state mutation and even real
event-bus backpressure, then publishes `POISONED` at lease release.
Poison-wins reaches `POISONED` first and rejects unstage, stage, cancel,
subscribe and direct database work with pending, cancellation, subscriptions
and event sequence unchanged.  Six repeated deterministic command-wins runs
use explicit barriers, not scheduling probability.

Background-generation evidence separately proves provider admission false at
stream entry/wait/yield.  If its delta effect wins, persistence, tracked state
and the delta event complete before final poison, while the next completion
effect rejects.  If poison wins, message content, durable attempt state,
tracked state and event sequence do not change.  Context-inherited child tasks
reject both while poison is pending behind the real owner and after final
`POISONED`.

## Final validation

All final-candidate evidence used `.venv314`, local fake backends, `/dev/shm`
where appropriate, provider variables removed from child environments, and a
fresh strict rooted VFS.  Focused shards overlap and are not added to the
unique full-suite count.

| Evidence | Result | SHA-256 |
|---|---:|---|
| corrected EF2 race/fence/background/cleanup selection | 7 passed | `1cbb4d9919ffa291f7796e5e8131d7ecc865f74ffac4c0b4ca9eece6e8f59231` |
| exact original Sol race inversion transcript | PASS | `96655d86d18ca709e856afff948d00466a757c5a28b69905eb4a97bf22961913` |
| combined EF1/EF2/N1 selection | 35 passed | `55fd962a1a5440886d26978b2bb4607577535b4386286d56b9654f2fa3174578` |
| affected Phase 3 generation file | 80 passed | `b6cc6894eafd3001ca65c75c0afec0efe6d69f8917384bb1c428e1b87c6fa74a` |
| strengthened T7 K1/K0 | 2 passed | `c6749a8e726b04440f8b5d55d008b4d2bee619641c510b8bbff6591766444dbb` |
| complete live-close matrix | 16 passed | `33b0fc0a499c6ac03941a15cdb1268d9cb5ca577f2a5df909549376f3e6631b0` |
| exact T2 close regressions | 2 passed | `c25e92d3ea8625cb25efd1edd6d585f7b72035bb9b86adf7edfbb784f386d093` |
| prior K0 regressions | 7 passed | `03647b77bc9328b7f4d93ac794472be5393397ac1f63dfd1c7bb5b45ca809ee5` |
| external T1-T9 pre/post matrix | 18 passed | `8a9e958444780dba70a448d571c2ae15ab1451dba8580edaae68e569104991c8` |
| native T2/T7 probes | 2 passed | `d25e09fa3a3068a986367d958746a103b18904b900911624c9624cd346555f64` |
| N2 topology matrix | 10 passed | `11ca401df311b08e41cf86b014273b795bdd10e3ff13504f65109789344e93d1` |
| accepted N1/N2/N3 selection | 40 passed | `890fce99d608e0d3253b20b96ac7ef9b5e8609f4e2939d881dacfe1e2be69225` |
| context provenance/immutability probe | PASS | `155cdda9e319f8bb0a04cfe87033045bd652897870ea8cd268d695cc659fda5e` |
| strict VFS load/ownership | 2 passed | `630b9dd4441ff511f70d737e43c7b84805b858608b4636c1e5b7e58a09090b2f` |
| migration immutability | 1 passed | `3d3494ebaf89a18dad4c8e6b98bb84932457c8ed7c1e32d2f8cbcac01408b344` |
| complete Phase 6 file | 561 passed | `6ae372443b10aec68cf384c3bbf415d49267578c629d8f62cf128957ef59cb0c` |
| full authoritative repository suite | 901 passed, 1 expected Stage G skip | `828579570b937dde1d1b6fd0d5c73e116ee872bf49d3801d229c3f5d7e3f6e02` |

The complete Phase 6 file recorded 561 tests, zero failures/errors/skips, and
1,925.705 seconds.  The full repository suite recorded 902 tests, zero
failures/errors, one expected opt-in Stage G skip, and 2,838.919 seconds.
Strict C11 `-Wall -Wextra -Werror` rebuild, bytecode compile into `/tmp`,
strict load/ownership, migration immutability, `git diff --check`, empty index
and zero-untracked checks passed.  The fresh strict VFS SHA-256 remained
`0a81b05fac80ec401572325cbeb61c348cbe9fe28c87836c24f56ed09cb92757`.

One independent context probe initially omitted the repository root from
`PYTHONPATH` and failed at harness import before candidate execution.  Its
corrected invocation passed two frozen fake requests, canonical/wire digest
checks, five raw foreign-key-off immutability attacks, restart `READY`, and
referenced-object GC preservation.  This discarded harness error is not
candidate evidence.

## Fresh independent reviews

Exactly one fresh Terra worker was used.  Its initial production fence finding
was repaired and the same worker performed the rereview.  Final disposition:
**PASS WITH CAVEATS**.  Report SHA-256:
`08e048078bcb94bd9a1f27ce9bd22935c70e195ff848f9882b9cf2cb64cf5195`.

Exactly one fresh Sol worker was then used.  A model-capacity interruption
occurred after its first production race probe; the same worker identity was
resumed, not replaced.  Sol independently executed six deterministic original
race repetitions, poison-wins and sibling attacks, both background orderings,
ContextVar inheritance, exception cleanup, fence ownership/startup, real
rooted-VFS close/backpressure/re-entry, T7 K1/K0 and both EF1 filesystems.
It did not rely on repository tests for disposition.  Final disposition:
**PASS**.  Report SHA-256:
`7608bb215f070d2cbbafc6cb7021fa08a9fa3ac522d91ef6a231659817bd7a56`.

## Telemetry and process caveats

Historical worker arithmetic is preserved without normalization:

- supplied-checkpoint arithmetic: **10 Luna / 68 Terra / 13 Sol**;
- source-report stable-worker ledger: **10 Luna / 68 Terra / 14 Sol**;
- previous EF1/EF2 continuation addition: **0 Luna / 1 Terra / 1 Sol**;
- this EF2 concurrency continuation: **0 Luna / 1 Terra / 1 Sol**.

Supervisor/root activity is not counted as a worker.  The Terra rereview and
Sol capacity continuation reused their original stable identities.

Exactly two prohibited read-only OMC/Organisational Memory queries occurred in
prior continuations.  There were zero updates, neither query supplied campaign
authority, and this continuation performed no OMC, Organisational Memory or
local-memory query/update.  The historical thirteen-case Terra XML omission
of strengthened T7 K1 remains supplemental only and is not used as current
closure evidence.

This continuation has one additional disclosed process violation.  A
supervisor environment-listing diagnostic exposed the value of an existing
public-provider credential in command output.  The value was not used, no
provider or network call followed, and every subsequent validation/reviewer
command explicitly removed provider variables without printing their values.
The exposed credential must be treated as compromised and rotated outside
this campaign.  Consequently, the statement that no credential was accessed
would be false; the accurate statement is that no credential was used and no
provider call occurred.

EF3 remains deferred and non-blocking: runtime teardown can retain a completed
opening-task exception graph across a later await.  No EF3 mechanism was
modified.  Stage G was not rerun; the sole full-suite skip is its opt-in local
acceptance test, and no new Stage G result is inferred.  External Astra was
not dispatched.

## Final boundary

The bounded EF2 concurrency blocker is technically inverted with both
linearized orderings, sibling/background centrality, monotonic poison,
deadlock/reentrancy cleanup, EF1 preservation, complete validation, Terra
review and Sol falsification.  This authorizes only creation of a new immutable
Stage J evidence package.  It does not claim Phase 6 accepted or closed.
Mick retains substantive acceptance, Phase 6 closure, commit, push, Stage G
and external-Astra authority.
