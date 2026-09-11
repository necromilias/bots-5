# Unified authority/effect participation inventory

Status: **landed Phase 6 authority inventory** for commit
`20847c7a49e26679d0d3dfe99798a2c211bec436`.

This inventory is the finite sibling-path audit for the unified-grant architecture. It was developed
against the Phase 6 repair candidate based on `89fb52979c67b8b1c822a7efb3c5a67946611bdf` and landed,
after final external closure review and complete-suite validation, in `20847c7a49e26679d0d3dfe99798a2c211bec436`.
See `LINUX_V0_1_PHASE6_CLOSURE_REPORT.md` for final acceptance and landing evidence.

`DataRootAuthority` is the only root admission and invalidation coordinator. Store `_poisoned`,
application close state, helper `_closed` fields, native UNKNOWN generations and migration journal states
are diagnostics, resource evidence, or local lifecycle gates; none independently grants forward work.

## Invalidation-route dispositions (22/22)

| Route | Disposition in landed Phase 6 |
|---|---|
| I01 explicit `poison()` | Implemented through `_request_invalidation`; target/cause are monotonic, admission closes immediately, origin grant is revoked, and publication drains forward grants, issued effects and resources. |
| I02 fresh validation/enumeration | Implemented inside callee-owned `operation()`; missing identity, changed identity and enumeration failure call the universal coordinator. |
| I03 close after failed fresh observation | Implemented: `_close_claim` records UNKNOWN and requests FAILED_CLOSED; the sibling exception handler repeats the terminal request without bypassing the coordinator. |
| I04 close after successful fresh enumeration | Implemented by the same UNKNOWN/FAILED_CLOSED path; result is withheld. F1 tests observe `READY + pending` while an unrelated grant finishes. |
| I05 pre-enumeration fresh-view failure | Implemented: absent retained claim/baseline and native fresh-open failure are classified before returning. Invalid area remains an ordinary API error. |
| I06 general scoped/retained close | Implemented centrally in `_close_claim`: clear fd once, record RELEASED or UNKNOWN, retain UNKNOWN and request FAILED_CLOSED. |
| I07 attachment descriptor uncertainty | Implemented: `_AttachmentFS` reports the descriptor outcome to authority; store poison remains diagnostic. |
| I08 attachment COMMIT uncertainty | Preserved and integrated: store classifier invokes authority invalidation; rollback is cleanup-only and cannot restore the grant. |
| I09 attachment ROLLBACK uncertainty | Preserved and integrated through the same grant-revoking poison path. |
| I10 attachment connection-close uncertainty | Preserved explicit classifier; rooted DBAPI close also reports generic uncertainty and releases its grant resource exactly once. |
| I11 capture abort/cleanup uncertainty | Preserved EF1 split: known cleanup permits retry; uncertain cleanup revokes authority. EF1 mutant proves oracle sensitivity. |
| I12 live lifecycle inconsistency | Preserved classifiers now revoke the discovering logical grant, preventing caught-exception re-entry. |
| I13 generic database uncertainty | Implemented in rooted DBAPI commit, rollback and physical close, with native handoff in every execute/commit/rollback/close path. Each rooted connection retains every live child cursor and drains those statements before native connection close and DB-resource lease release. Every public runtime store family owns/joins a grant. |
| I14 native live/temporary/auxiliary uncertainty | Implemented with per-VFS atomic global and thread-attributed generations. The faulting call hands off before success; polling catches out-of-band UNKNOWN. |
| I15 VFS registration cleanup uncertainty | Implemented through the universal FAILED_CLOSED request and a synthetic retained UNKNOWN ledger entry. |
| I16 acquisition/migration/startup failure | Implemented under one phase-restricted startup grant. Failure revokes it; READY requires a forward grant, no pending target, no resources and no issued effects. |
| I17 terminal physical/VFS/logical close | Implemented by the shared close driver. Healthy forward verification and invalidated release-only teardown are separate. Every bearer is attempted and merged. |
| I18 local helper invalidation | Justified release-only bearer bookkeeping. It prevents helper reuse but does not decide aggregate admission. |
| I19 migration-private VFS close | Implemented: every private VFS is authority-registered under a unique resource key and merges full close inventory. |
| I20 fork | Justified special case: child PID/owner provenance is invalid; at-fork teardown sets child FAILED_CLOSED without changing the parent. |
| I21 direct fence/integrity failure | Runtime fence owns/joins a grant and classifies fsync failure. Canonical `_AttachmentFS.read_verified` is the callee boundary for authority-owned digest/size facts: absent, unsafe or mismatched stored bytes revoke the discovering grant before E09/E13 can translate the exception. Persisted text-representation claims are checked against the shared semantic classifier; malformed external input remains a normal error. |
| I22 logical-claim acquisition failure | Implemented: bind/collision is under the startup grant, and provisional socket-close uncertainty creates retained `provisional:logical-root=UNKNOWN`, requests FAILED_CLOSED and pins the authority. |

## Effect-class dispositions (22/22)

| Effect | Disposition in landed Phase 6 |
|---|---|
| E01 public async commands | All tracked application commands enter `command_admission`; the production fallback was removed. Outer ownership spans mutation and awaited publication. |
| E02 selection/cancellation mutation | Owned by E01. Per-grant revocation is revalidated before nested entry; F1 and F3 inspect the in-memory value and event sequence. |
| E03 generation tracking/task start | Command work uses E01; each background persistence block uses a fresh independent E05 grant. Copied parent context cannot authorize the child. |
| E04 event publication/delivery | Production EventBus is bound to store admission. Sequence allocation and every awaited delivery remain inside the grant; direct private delivery requires an issued effect. |
| E05 background branches | Dispatch, metadata, delta, completion/non-stop, provider failure, stream-end cancel, missing terminal, timeout, cancellation and exception blocks each acquire fresh independent local grants. Provider wait owns none. |
| E06 direct generic store work | Every public SQLite method is callee-decorated. Rooted connection creation rejects before real DBAPI creation without a grant, and use revalidates forward status. |
| E07 inherited Phase 5 work | Every listed Phase 5 reader/writer is decorated at the mixin definition, including nested setters and post-write reads. Trigger/CAS guards remain unchanged. |
| E08 attachment ingest/dedup | Existing transition now joins the logical grant and transition gate; self-failure revokes forward work while allowing owned unwind. |
| E09 attachment reads | Public metadata/history/byte methods acquire grants; filesystem helper and DB resources revalidate that grant. |
| E10 attachment deletion | Existing transition lifetime is preserved and joins the same logical grant. |
| E11 GC | Existing transition covers enumeration, durable intent, fence, filesystem mutation and final row deletion; a tainted grant cannot advance it. |
| E12 filesystem leaves | `_AttachmentFS` requires current forward authority for live leaves; public `inventory` now callee-acquires/joins. Close bookkeeping remains cleanup-only. |
| E13 generation provenance persistence | Both start paths retain transition ownership across context/source/message/attempt rows, attachment associations, CAS, transaction settlement and return reads. Persisted representation metadata is revalidated at the persistence boundary. |
| E14 database durability fence | Runtime calls acquire/join; startup calls use only the startup owner; failures invalidate. |
| E15 startup/reconciliation | One startup grant spans acquire, migration, open, recovery, fence and READY publication. |
| E16 interrupted-generation reconciliation | Public store composite method is callee-decorated; nested finalizations join it. Constructor binds EventBus first and then invokes the composite. |
| E17 shutdown fallback reconciliation | The close driver now acquires one independent composite grant across row selection and finalization. Rejection leaves durable RUNNING state for fresh authority; only in-memory bearer cleanup follows. |
| E18 terminal close | Healthy close obtains a narrow post-drain teardown grant. Invalidated close opens no DB/recovery work and only releases known bearers. Repeated callers share one result. |
| E19 acquisition/migration/restore | Startup grant and registered private VFS ledgers cover all pre-READY effects. Broad caught failure cannot mint a replacement grant. |
| E20 configuration/credentials | Every public `ProviderConfiguration` method acquires/joins command admission, including Secret Service put/delete and status reads. |
| E21 subscription/projection | Bound subscribe is an admitted mutation. Historical consumption needs no forward grant; consumer-triggered commands must independently admit. |
| E22 release-only cancellation/bookkeeping | Task/subscription cancellation, scalar close results, rooted child-cursor drain, map/set removal and bearer release remain allowed cleanup. Child settlement precedes parent DB-resource release and cannot open DB sessions, reconcile, fence or mutate business state. |

## Obligation dispositions (27/27)

`Executable` names the direct oracle. `Preserved` refers to still-applicable tests in
`test_phase6_context_attachments.py` and the Phase 3–5 files. Final broad-suite acceptance is recorded in
`LINUX_V0_1_PHASE6_CLOSURE_REPORT.md`.

| # | Disposition and evidence |
|---:|---|
| 1 | Executable: `test_ef2_race_a_command_wins_serialization` and repeated race D observe mutation/delivery before final POISONED. |
| 2 | Executable: `test_ef2_race_b_poison_wins_serialization_and_centrality` observes unchanged application, event and DB surfaces. |
| 3 | Executable: F1 `stage` parameter observes UNKNOWN immediately, `READY + pending`, stage mutation/delivery, then FAILED_CLOSED. |
| 4 | Executable: F1 `unstage` parameter makes the same intermediate observations around removal and payload. |
| 5 | Executable: F1 `create_chat` parameter proves durable row and delivered event before terminal publication. |
| 6 | Executable: both `before_insert` and `before_commit` F2 barriers prove a direct durable row and terminal publication only after transaction/resource settlement. |
| 7 | Executable: pre-creator F2 test checks private bypass rejection, zero real `sqlite3.connect`, stable native open count, and rejected SELECT/P5 setter after invalidation. |
| 8 | Executable/source combination: rooted commit/rollback/close classifiers are common to every SQLAlchemy family; raw DBAPI and production SQLAlchemy cleanup-order tests prove cursor-first and connection-first settlement, multiple-child ownership, revoked cleanup and UNKNOWN handoff before parent lease release. Preserved K0, T1–T9, N1 and native transaction faults distinguish known abort from uncertain outcomes. |
| 9 | Executable: F3 single-owner and two-owner cases catch a real fresh-view close failure, then reject application, store, event, fence and checked-out-connection effects with unchanged mutation/event snapshots. |
| 10 | Executable: healthy nested app/store/event/DB test proves one grant identity and one release; preserved attachment nesting covers the transition gate. |
| 11 | Executable: copied child task, `to_thread` and post-scope context reject; preserved fork/PID tests cover process provenance and parent survival. |
| 12 | Executable/preserved: grant exception cleanup, generation cancellation tests, publisher cancellation with a real blocked putter, and close-task scalarization leave no live owner/putter. |
| 13 | Executable: capacity-one queue is observed full with an actual `_putters` waiter; command-wins drain and cancellation both settle. |
| 14 | Executable/source combination: two-owner F3 and EF2 races preserve unrelated grants; monotonic target ranking prevents downgrade; issued effects/resources participate in drain. |
| 15 | Executable/preserved: admitted-owner fence completion, late fence rejection, native/startup fences and T7 no-post-close fence paths. |
| 16 | Preserved executable background-generation test plus Phase 3 generation matrix covers every named branch; source inventory confirms one independent scope per local-effect block and none across provider wait. |
| 17 | Source/executable combination: constructor uses the decorated composite reconciliation method; close fallback now owns a single composite independent scope and otherwise leaves durable state for restart. Existing restart and close tests exercise both. |
| 18 | Preserved executable EF1 on tmpfs/Btrfs, N1/N2/N3, T1–T9, lifecycle/recovery/GC and post-close no-fence/no-callback matrices. |
| 19 | Preserved executable native open/delete/auxiliary/private-close fault matrices plus thread-generation handoff and authority-ledger source assertions. |
| 20 | Preserved executable private-VFS close/reacquisition tests; source inventory shows every migration constructor supplies authority and a unique resource label. |
| 21 | Executable/preserved: identity failure, new logical/open classification, both close siblings and closing race. F1 explicitly covers the successful-enumeration close sibling; source confirms failed-enumeration uses the same request. |
| 22 | Preserved executable shared/faulted/held-owner/UNKNOWN close matrices and same-owner rejection; source audit confirms invalidated release-only store teardown. |
| 23 | Preserved executable healthy close and physical-barrier tests; `_healthy_teardown_operation` is phase-, owner-, pending-, forward-, resource- and checkout-restricted. |
| 24 | Preserved executable all-revision migration, crash/restore, staging/deleting recovery, ready validation, second restart and schema immutability matrices. |
| 25 | Executable/source: complete store/mixin/configuration wrapper inventory, private engine/pre-creator rejection, expired connection and private delivery rejection. I21 corrupt-required-payload and persisted-representation oracles prove callee invalidation and reject subsequent forward durable work. |
| 26 | Preserved executable raw-FK-off trigger guards, exact schema/trigger/index definitions, frozen context/wire evidence and no-attachment generation tests. |
| 27 | Executable: event is produced, authority invalidates, historical payload is consumed afterward; private delivery has no issued grant and copied child commands reject. |

## Release-critical mutant sensitivity

`tests/mutants/run_authority_effect_mutants.py` copies `src` into a fresh temporary tree per variant and
never rewrites the candidate. Required result:

| Mutant | Required oracle result |
|---|---|
| F1 immediate terminal assignment | Killed; all three F1 command parameters fail. |
| F2 remove direct-store decorator grant | Killed; both direct-writer barriers fail. |
| F2 acquire only after real DBAPI creation | Killed; the pre-creator oracle fails. |
| F3 ambient/global-only re-entry | Killed; single- and two-owner revocation oracles fail. |
| EF1 remove cleanup-failure poison | Killed; EF1 tmpfs oracle fails while the positive path remains unchanged in the live candidate. |
| Duplicate selected-resource close with swallowed error | Killed; unconditional selected-connection invocation accounting reports the second call. |
| Detached EventBus delivery | Killed; the real full-queue owner/delivery oracle fails. |
| Invalidated cleanup opens a fresh DB session | Killed; the zero-creator cleanup oracle fails. |
| Suppressed native UNKNOWN handoff | Killed; the thread-attributed pre-return authority oracle fails. |
| Removed I21 required-payload invalidation | Killed; corrupt authority-owned bytes remain a local read error and the later durable-write rejection oracle fails. |
| I21/E13 representation invalidation removed | Killed; detected persisted representation contradictions fail to invoke the common coordinator. |
| I21/E13 NUL eligibility weakened | Killed; NUL-bearing canonical bytes incorrectly become text-eligible. |
| B1 convenience cursor bypass | Killed; a connection convenience path returning a raw cursor is detected. |
| B1 cursor revalidation bypass | Killed; post-revocation statement/stepping work reaches SQLite. |
| B1 parent release before child drain | Killed; the connection-first oracle observes the retained cursor still live when the mutated parent releases. |
| B1 child-cursor tracking removed | Killed; the direct ownership-ledger assertion observes that the parent does not retain its live child. |

## Explicit sibling-path search result

The audit searched direct terminal assignments, raw engine/DBAPI creation, caller-only wrappers,
stale/copied context, cleanup forward admission, detached publication, native UNKNOWN propagation,
phase-capability escape, parallel poison flags and resource lifetimes longer than grants. Concrete gaps
found during the campaign were repaired and retained as regression/mutant targets, including provisional
logical-socket close outcome accounting, composite shutdown fallback ownership, canonical attachment
payload/representation invalidation, DBAPI convenience-cursor participation, and child-cursor lifetime
settlement before parent resource release.

Final targeted external rereview found no concrete current blocker in the repaired DBAPI/resource-lifetime
family. The exact candidate then passed the reserved complete Phase 6 suite and complete repository suite
without failure before landing. No unresolved sibling escape is recorded in the landed Phase 6 inventory;
future changes must preserve or explicitly supersede these obligations.
