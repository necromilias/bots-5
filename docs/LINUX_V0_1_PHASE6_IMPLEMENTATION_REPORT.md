# Linux v0.1 Phase 6 implementation report

## Current candidate status and authority boundary

This is the cumulative, unstaged, uncommitted Phase 6 implementation report.
The historical blocked candidates and their evidence are retained below. The
previous recovery candidate attempted to implement the human-accepted
C3-corrected authority design, passed deterministic validation and bounded
local acceptance, but its Sol/xhigh final closure audit returned **BLOCKED**
after reproducing three accepted-contract failures. Human authorization then
permitted one final bounded FCR repair of exactly those findings. The new FCR
candidate passed its deterministic and independent targeted-review gates. The
one new fresh Sol/xhigh final audit returned **PASS WITH CAVEATS** and closed
F1, F2, and F3. Phase 6 has technical closure within the recorded bounded
caveats, but remains unstaged, uncommitted, unpublished, and outside the Git
authorization boundary.

This candidate is in the canonical Phase 1 checkout. The supplied baseline is
`c9efdb374e37be94bb9ab68abd45e8ed718c3437`, parent
`7c609f227bf34887208272b42af77d2c8a15789f`. No fetch, ref mutation, commit,
stage, push, OrgMem query/write, public-provider call, public-network call,
managed/public-credential read, or Phase 7+ work was performed. The only live
calls were the explicitly authorized local Ollama acceptance at
`192.168.50.223`. Phase 1-5 behavior, the campaign engine, and the
`Provider.complete()` seam remain preserved.

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
that this candidate does not yet meet those requirements.

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
