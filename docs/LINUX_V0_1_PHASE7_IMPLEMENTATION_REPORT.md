# B.O.T.S. Linux v0.1 Phase 7 implementation report

Date: 2026-09-12 Australia/Melbourne

## Status

Two independently audited Phase 7 candidates are immutable **BLOCKED** history.
The original external closure candidate has manifest SHA-256
`87e71b5c46796142498dad8f1f1cd6b742a78b1f3e6c995fd0a5d03bfe07707c`
and tracked-patch SHA-256
`f6ff0f709d97a08531e1ffb91cf91116260d42d0a94d60dc78b91bb5f2b98e4f`.
The subsequent repaired candidate has manifest SHA-256
`8fcc0e4f36658ba6b59e593fe9ce271c789175d445f96da5884b205d71bab557`
and tracked-patch SHA-256
`21cbe391ed6aa58d6e3a14f4636794cd73b4c367e2fedc13745ced36df6d402a`.

The second external audit established a distinct type-correct authoritative
monotonicity defect, B1-RC-01: a live `source_revision` regression could reuse a
previously consumed revision and launder an incomplete FTS projection back to
VALID. A later bounded repair enforces the exact authoritative sequence at the
existing Phase 7 mutation boundary and adds a live high-water defence that
poisons the existing `DataRootAuthority` on observed semantic regression. Its
focused, migration, Phase 7, Phase 1–6 preservation, mutant, and fixed-seed
benchmark gates pass. Exact final seal and fresh independent dispositions are
recorded outside this self-referential candidate report in the campaign evidence
package. Phases 1–6 remain closed and landed. This report is not commit, push,
release, external closure, or substantive acceptance authority.

The complete repository suite has deliberately not been run. It remains reserved
for an external closure PASS followed by Mick's pre-commit gate.

## External closure BLOCKED and bounded repair waves

The independent external report is preserved byte-for-byte after its documented
newline canonicalisation at
`work/campaign-evidence/phase7/reviews/EXTERNAL_PHASE7_CLOSURE_BLOCKED_87e71b5c.md`,
SHA-256
`6c8bd838d5cf49086c819974420ed22a8ecd7cfc829fdbae25ff035e478128d1`.
It established exactly three B1 implementation defects; it is not disputed or
rewritten as success.

- **B1-01:** ordinary search could serve a forged returned FTS payload while
  revision/checkpoint metadata and structural mappings remained valid. Search
  now selects the stored FTS title/body/filename for each bounded candidate row
  and compares the complete indexed projection with current authoritative truth
  in the same read snapshot before materialisation. The `limit + 1` lookahead is
  validated too. Any mismatch returns typed `SearchIndexInvalid`, durably marks
  the observed derived snapshot INVALID through the existing generation/
  checkpoint CAS, never silently suppresses one row, and leaves rebuild
  reachable. Ordinary search performs at most 101 such projections and does not
  invoke whole-index diagnostics.
- **B1-02:** malformed derived singleton values could leak raw conversion
  exceptions. Bounded singleton decoding now requires the exact singleton,
  strict integer counters within SQLite's signed range, exact condition/schema/
  tokenizer/detail representations, and a coherent checkpoint. Malformation is
  typed derived INVALID, does not poison `DataRootAuthority`, and explicit
  rebuild deletes/recreates or normalises the derived singleton.
- **B1-03:** malformed authoritative `source_revision` could leak raw conversion
  failure or be coerced by SQLite arithmetic during a later mutation. The
  authoritative singleton is strictly decoded before each of the seven Phase 7
  mutation arms and after trigger consumption. Corruption raises typed
  `StateError`, poisons through the existing Phase 6 authority mechanism, rolls
  back the attempted business mutation, and prevents later forward admission.

The same storage-class family is therefore intentionally split at its authority
boundary: `search_source_state.source_revision` is AUTHORITATIVE;
`search_index_state.condition`, `checkpoint_revision`, `generation`,
`schema_version`, `tokenizer_version`, `detail`, and rebuild coordination are
DERIVED. No migration, authority redesign, product-policy change, second
connection authority, or whole-index ordinary-query scan was introduced.

Focused B1 coverage passed **13/13**. The final-byte Phase 7 subsystem matrix
passed **99/99** in 252.21 seconds, and the representative Phase 1–6 selection
passed **21/21** in 60.21 seconds. The first post-repair matrix passed 98 cases
but exposed an over-broad rebuild exception conversion; that implementation
defect was narrowed, its targeted 8/8 rerun passed, and both transcripts remain
historical evidence. The accepted expanded mutant run used a clean **37/37**
baseline and killed **43/43** mutants, including the three external-B1 mutants,
with zero survivors, setup failures, collection failures, or timeouts. Earlier
partial/harness-failure mutant runs are preserved and excluded.

The unchanged fixed-seed benchmark completed in 81.299 seconds against the exact
repaired production/test/mutant bytes recorded in its launch identity. It used
25,046 documents and 1,309,144 searchable bytes. P50 milliseconds were: global
138.170, in-chat 64.996, filtered 68.705, two-page 260.761, active-only 81.605,
attachment filename 63.991, and verified attachment text 63.738. Deterministic
rebuild p50 was 3,939.884 ms with three identical content hashes; peak sampled
temporary storage was 7,303,920 bytes. All seven p50 query shapes were lower than
the post-V8 observation despite returned-row validation. These are synthetic
observations, not an operator workload, SLO, or proof that every future run is
faster. The accepted FTS5 trigger did not fire.

Fresh review of the exact V9 review seal SHA-256
`7c0687890e068d93e80622ef1bb1848fb7a9cb3656a99212ded7597c3ee9ea0b`
and tracked patch SHA-256
`21cbe391ed6aa58d6e3a14f4636794cd73b4c367e2fedc13745ced36df6d402a`
returned three PASS dispositions with no blocker or stop condition:

- implementation review report SHA-256
  `5dbb45ffbb553eae0263b588eb946aff8e02500bc3a95a0780dc52579125b50c`;
- falsification report SHA-256
  `acbe20018da7b6314f593df447e98df0c5a1972cf54380420f4fcb69adc835c2`;
- evidence reconciliation report SHA-256
  `8459cfa4b0ebbc3e40d166f1e880f76a2b3fda0bee7918462f6cfb0de8f55bd0`.

The independent external re-closure report is separately preserved at
`work/campaign-evidence/phase7/reviews/EXTERNAL_PHASE7_RECLOSURE_BLOCKED_8fcc0e4f.md`,
SHA-256
`2aaa565f78325c3c2aa8929c7d8f6525424bdd8b5bdeeeb718cc3b61d09c328d`.
The repaired candidate stayed byte-stable during that audit. Its manifest,
tracked patch, and report SHA-256
`fcc283cca2e747a498566d777f88481ec1af4279dce164a0f7e999f08f245bcc`
remain historical evidence and are not disputed or rewritten as success.

The re-closure audit found exactly B1-RC-01, which is a distinct semantic defect
from the earlier malformed-storage-type defect B1-03. A naturally accepted
integer regression from 1 to 0 left authority READY; the next legal mutation
reused revision 1 already acknowledged by derived coordination, so the new
authoritative object was missing while source and checkpoint both read 1 and
search again advertised VALID.

The bounded monotonicity repair uses the existing Phase 7 arming and Phase 6
poison paths:

- schema triggers reject direct insert/delete of the source singleton and call
  the rooted connection's transition guard for every update;
- an armed participating transaction binds the exact observed old revision and
  permits only its unit successor, while later trigger firings in the same
  transaction may only retain that already-incremented value;
- the store verifies trigger consumption and the exact committed successor;
- a commit-aware in-memory high-water is advanced only after a known successful
  authoritative commit, under the existing writer serialization;
- every live authoritative source read must equal that established high-water;
  a mismatch is typed authoritative `StateError`, poisons the real
  `DataRootAuthority`, and prevents further forward admission;
- valid derived checkpoint lag remains ordinary STALE derived state and does not
  poison authority; receipt coordination remains an optimization, not authority.

Direct arbitrary transitions `5 -> 4`, `5 -> 5`, and `5 -> 7` are rejected at
the mutation boundary. A test-only UDF bypass on a disposable admitted database
injects already-persisted `1 -> 0` and `10 -> 9` states to prove the independent
live defence detects regression even when the value remains above the derived
checkpoint. Normal `5 -> 6 -> 7`, concurrent serialized participants, receipt
ordering/catch-up, and restart/open all retain their accepted behavior. No
durable second revision ledger, authority redesign, whole-index query scan, new
product object, or migration-history change was introduced.

## Baseline gate

The pre-mutation gate passed:

- repository: `/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1`;
- branch: `main`;
- HEAD/local `main`/local `origin/main` tracking ref:
  `da3264442e6069861dbeaad972d5bc7ba85bdf31`;
- local divergence: 0 ahead, 0 behind;
- index empty and tracked worktree clean;
- untracked files confined to `work/campaign-evidence/**`;
- `git diff --check`: exit 0;
- Phase 6 implementation confirmed at
  `20847c7a49e26679d0d3dfe99798a2c211bec436`;
- Phase 6 closure/current-state documents confirmed closed/landed;
- migrations 0001–0009 present and no Phase 7 implementation already landed.

No fetch or network operation was used. `origin/main` means the existing local
tracking ref.

## Implemented scope

The candidate implements the accepted Phase 7 scope:

- authoritative `chats.archived_at` and archive/unarchive behavior;
- additive migration `0010_phase7_search_navigation`;
- authoritative singleton `search_source_state.source_revision`;
- derived index state, document-key mapping, and FTS5 `unicode61` index;
- global and in-chat literal search;
- document, role, terminal-state, immutable attempt-attribution, time, active-only,
  and Include Archived filters;
- FTS relevance ordering with authoritative recency and stable identity tie-breaks;
- all surviving message revisions and regeneration siblings by default;
- explicit active/historical annotations and exact message-ID navigation;
- temporary historical-leaf projection without mutating the authoritative head;
- typed GONE behavior when an identity or location disappears;
- one attachment document per attachment identity, filename plus Phase 6-verified
  UTF-8 text, multiple resolved locations, and hidden unreferenced attachments;
- explicit VALID, STALE, REBUILDING, INVALID, and UNAVAILABLE behavior;
- contiguous optional in-memory receipts and mechanically visible lost-receipt gaps;
- synchronous authority-owned deterministic rebuild with atomic rows/checkpoint;
- bounded desktop search/filter/status/rebuild/archive/historical-focus controls;
- opt-in fixed-seed performance evidence.

The candidate adds no semantic search, embeddings, vector store, RAG, OCR,
document extraction, hidden summarization, provider/network dependency, durable
per-object search journal, detached search worker, or Phase 8 behavior.

## Authority and failure semantics

Search-visible business writes are armed inside the existing
`DataRootAuthority` transition. Phase 7 triggers reject unarmed direct DML and
consume exactly one source-revision increment per authoritative transaction,
regardless of the number of affected rows. The business mutation and revision
commit together.

B1-RC-01 hardens that same boundary rather than adding another authority. The
source-singleton update trigger now consults the existing connection-local guard
and accepts only the armed transition from the bound old revision to its exact
unit successor. Direct update/insert/delete cannot become legitimate sequence
evolution. The store holds its source-revision lock across known commit and
high-water publication; every later authoritative read requires exact agreement
with that established live high-water. Already-persisted decrement or reuse is
authoritative corruption: the existing poison path raises typed `StateError`,
makes authority non-ready, and closes later admission. Rollback clears only the
pending publication. Cleanup of already-owned resources remains available.

FTS rows, mappings, checkpoint, generation, ranking, snippets, active-state
annotation, and locations are derived. Receipts are in-memory optimization only;
out-of-order receipts are retained and only contiguous revisions advance. A lost
receipt or restart leaves source revision greater than checkpoint and search
refuses service until explicit rebuild.

Rebuild first durably marks REBUILDING, obtains existing writer serialization,
recomputes current authoritative projections, validates at the expensive boundary,
and atomically commits mappings, FTS rows, and matching checkpoint. Interruption
cannot appear VALID. Routine query validation performs only bounded state/version,
cursor, and authoritative join checks.

Clean derived failure does not roll back a known successful business mutation.
Unknown commit, rollback, physical-close, rooted-native, file, or verified
attachment outcome continues through the landed Phase 6 fail-closed authority
paths. Every new public store method is included in the existing callee-operation
inventory; no second authority exists.

Application rebuild retains its executor future through cancellation, settles
the completion event before returning, and only then re-raises deferred caller
cancellation. The event child starts in an explicit empty context so it acquires
its own executor-owned grant instead of copying the tracked caller's grant; the
original tracked command remains active and awaits both effects. Deterministic
real-authority oracles cover ordinary completion and repeated cancellation.

## Migration identity

- revision: `0010_phase7_search_navigation`;
- down revision: `0009_phase6_context_attachments`;
- migration-wrapper SHA-256:
  `bab383da0bf73478c769bb48d2c6b273158cb843091ece8fd31d11416cfd481e`;
- the wrapper byte is unchanged, but its imported uncommitted Phase 7 schema
  changed in this bounded repair: `phase7_schema.py` moved from SHA-256
  `22d2e8cd4e11ad865403c488c9d5881287a74560869e6f0337fc7ade525a985b`
  to `1678956968ff0f24d356b5744a2e9e57d84a2738bb4b04d738ba7b7dbec2b57c`;
  therefore effective 0010 semantics changed and the complete bounded migration
  matrix was rerun;
- migrations 0001–0009: byte-unchanged from HEAD by exact Git diff and executable
  hash oracle;
- FTS5/unicode61 preflight occurs before migration/journal/source mutation;
- a genuine interrupted 0009 journal completes at immutable target 0009 before a
  distinct 0010 migration begins;
- accepted startup constraint: the current composition preflights FTS5 before
  canonical database discovery, so this candidate does not claim that core use of
  an already-migrated database remains reachable when the runtime lacks FTS5. This
  remains within the locked allowance for existing startup-architecture constraints.

## Targeted validation

The B1-RC-01 repair added ten exact focused cases covering live `1 -> 0`
laundering, rollback above a lagging checkpoint, same-value and forward-jump
direct writes, armed non-unit transitions, singleton replacement/deletion,
sequential legal transitions, restart, poison/no-forward-admission, and the
derived-lag non-poisoning control; all **10/10** passed. The current-byte Phase 7
subsystem matrix passed **109/109** in 316.19 seconds. The representative Phase
1–6 preservation matrix passed **21/21** in 66.92 seconds. Because effective
0010 schema semantics changed, the bounded migration matrix passed **15/15** in
40.92 seconds, including fresh and every supported 0001–0009 start, FTS5
unavailable/no-touch, immutable earlier migrations, genuine interrupted-0009
journal handling, and validation/promotion fault coverage.

The first current-wave Phase 7 run passed 107 and failed two synthetic unknown-
connection settlement oracles because new commit bookkeeping assumed the test
double exposed SQLAlchemy `.info`. The same assumption then failed one of 21
Phase 1–6 preservation cases. This was classified as a Phase 7 persistence-
compatibility implementation defect, repaired with non-authoritative optional
bookkeeping for connections that cannot carry pending source state, and proved
by a focused 4/4 rerun before the final matrices. Both failed transcripts remain
historical evidence. No complete repository suite was run.

The single broad post-V8 Phase 7 subsystem command collected and passed
**86/86** cases, exit 0, in 233.23 seconds. Its accepted host transcript is
retained in campaign evidence:

```text
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python -m pytest -o addopts= -p no:cacheprovider -q tests/test_phase7_core_contracts.py tests/test_phase7_desktop.py tests/test_phase7_migration_authority_faults.py tests/test_phase7_search_navigation.py tests/test_phase7_v4_repairs.py
```

Composition:

- core/domain/application contracts: 6;
- desktop UX: 7;
- migration/authority/fault/concurrency: 27;
- search/archive/lineage/navigation/attachment/corruption/real-authority: 24;
- V4-V8 authority/recovery/archive/filter/version/cancellation regressions: 22.

Additional preservation evidence:

- one combined post-V8 preservation rerun of current-head persistence and
  conversation transactions (6), representative Phase 1 core/desktop (9), and
  representative Phase 6 authority/migration/attachment (6): 21/21 passed in
  66.96 seconds, with raw host transcript retained;
- the exact V8 real-authority rebuild/event, repeated-cancellation, foreign-version,
  and malformed-version-type repair slice: 7/7 passed in 21.73 seconds, with raw
  host transcript retained;
- strict rooted native VFS rebuild: exit 0, SHA-256
  `238fd2eb770ddf28fa4c30d971140a5a3562d008fa0b3661ee8a580d2e0ab840`;
- `compileall -q src tests`: exit 0;
- performance harness `py_compile` and JSON parse: exit 0;
- `git diff --check`: exit 0;
- exact diff over migrations 0001–0009: empty;
- production isolation scan: no provider/network/semantic/OCR/Phase 8 path and no
  `search_source_changes` journal.

Fresh independent post-V8 closure evidence:

- implementation review: PASS, including 7/7 exact repairs, a 14/14
  cross-domain sample, repeated cancellation during backpressured event delivery,
  worker-error precedence, and concurrent close settlement; report SHA-256
  `5357f14a025d448f041c067ca0a0728c8905b94a635daaf03ef2a231de85b82c`;
- falsification: PASS, including a 52/52 targeted matrix, 8/8 malformed-state
  rejection probe, repeated worker/event cancellation, pending-poison settlement,
  and 4/4 selected repair-boundary mutants killed; report SHA-256
  `eb3102f691d536a7c6f2aa21203c5fdc99b8e737b9d55f834bad6cea856ff2ae`;
- evidence reconciliation: PASS over all 35 sealed paths, exact path union,
  refs/index, raw accepted transcripts, mutants, performance identities, prior
  migration immutability, and historical-failure chronology; report SHA-256
  `f927ed23af9993d985b12d993ff17dcc42c45a636b622664fab336a5c6c4ea45`.

The required-area-to-oracle mapping is preserved in
`work/campaign-evidence/phase7/REQUIRED_AREA_MATRIX.md`.

## Oracle sensitivity

The current expanded disposable-copy mutant runner used a clean **39/39**
baseline and killed **46/46** variants with no survivor. Its three B1-RC-01
mutants respectively bypass live returned-source regression detection, downgrade
authoritative regression without poison, and admit a non-unit source transition;
the intended laundering, authority-poison/no-admission, and exact-transition
assertions killed them. The final runner SHA-256 is
`b64140050a99aa135ca5c54bafdd795f4cdc58c2779c82ddc554fd0c30903416`;
the final matrix-summary SHA-256 is
`33f81d3edc018a3448bb76f26fa15b2993ed54b4210d99115cda9cf93b97791a`.

An initial isolated monotonicity run conservatively reported the downgrade
mutant as surviving because its expected signature named a lowercase enum
rendering; the authority assertion had in fact failed. Two legacy mutant anchors
in the first complete run also produced setup/incidental failures after the new
guard. Those harness defects were corrected, rerun in isolation, and then
superseded by the uninterrupted 46/46 accepted run. All earlier transcripts are
retained and are not counted as kills.

The final disposable-copy mutant runner used a clean 34-test baseline and killed
**40/40** material bad variants by the intended assertion or exact intended
wrong-outcome evidence, with zero survivors, setup failures, collection errors, or
timeouts in the accepted run. It covers source-revision single consumption and
chat identity/recency trigger coverage, stale refusal, receipt contiguity, literal
FTS escaping, canonical cursor form, typed Unicode rejection, archive exclusion,
historical annotation, scoped/hidden attachment locations, GONE without
substitution, exact historical leaf, interrupted rebuild, authoritative FTS
payload diagnostics, durable invalidation after logical query corruption, and
live read commit/rollback classification. The V4 additions prove post-commit
derived failure suppression, source transaction settlement classification across
create/archive/generation/regeneration, typed missing-state recovery, generation
ABA prevention, exact filter containers/values, and headless archive lineage
preservation. The V5 additions prove restart reachability with a missing derived
singleton and reject a prior-process cursor even if durable generation integers
are reused. The V6 additions prove foreign schema/tokenizer versions refuse
service and rebuild to accepted metadata, while archive/default and active-only
visibility loss return typed GONE during exact navigation. The V7 additions prove
restart tolerates well-typed repairable foreign derived versions and make the
fake-store worker oracle sensitive to cancellation detachment. The V8 additions
close the real-authority/event gap, reject malformed stored version types, and kill
both copied-parent-grant event publication and detached-cancellation variants
through real-authority oracles. A final three-mutant rerun against the strengthened
repeated-cancellation test bytes also killed 3/3.

- runner SHA-256:
  `c8def9638c774bd97d491cdfcc07743856abb17767a733091c7e0097de109d9c`;
- final summary SHA-256:
  `b2a3e66fff2bb63691cb9adf68f170c88e584640083cc1a9bb7f0855e1fb5cab`.

The proposed live-close bypass leaked an owned rooted resource and timed out, so it
was excluded rather than counted as a kill. The ordinary close path remains covered
by a focused public-path regression. Earlier invalid baselines, survivors, and the
close timeout remain preserved as history.

## Fixed-seed synthetic performance evidence

These figures are synthetic and are **not** the operator's real workload or an
operator SLO. Runtime: CPython 3.14.7, SQLite 3.53.4, Ryzen 7 3800X, btrfs, fixed
seed `0xb0750007`, `PYTHONHASHSEED=0`.

The B1-RC-01 rerun on exact repaired production and benchmark bytes completed in
91.516 seconds. Its corpus has 25,046 search documents and 1,309,144 searchable
UTF-8 bytes. Current p50 milliseconds are: ordinary incremental receipt 84.381,
global 153.892, in-chat 74.725, filtered 78.948, two-page 293.274, active-only
91.678, attachment filename 73.391, and attachment text 72.646. The 25,000-row
business transaction took 569.415 ms and its derived receipt 11,528.181 ms.
Three rebuilds took 4,414.658, 4,440.989, and 4,439.254 ms, with identical
content hash
`e1cef8aefcbc13c60a6e2d1e30da8d20350a6405ef3f2a229fd9f975c6e55325`;
peak sampled temporary storage was 7,299,928 bytes. Final status was VALID at
source/checkpoint 47/47 and generation 3.

Compared with the immediately prior repaired-candidate observation, wall time
rose 12.57%; bulk business time 11.36%; bulk receipt time 12.63%; incremental
p50 11.45%; query p50 values rose between 11.38% and 14.97%; and rebuild p50
rose 12.67%. These are separate fixed-seed observations, not isolated causal
measurements: the changed source-write path plausibly contributes, while normal
run variance is not estimated. No accepted SLO or backend-replacement trigger
fired. Raw JSON SHA-256 is
`221c2eeaed712a99588d7da6b3757a98f2d8e9d2861b6de9bee4f9847b436780`;
rendered Markdown SHA-256 is
`7cc87f655ccd7bb578b6a59740631ddc6566ee6fde57af1b0e28a96b0cea78fe`.

The corpus and measurements below are earlier Phase 7 performance history and
remain preserved; they are not substituted for the current B1-RC-01 rerun.

Corpus:

- small public-application corpus: 6 chats, 36 messages, 18 attempts,
  4 attachments, 4 message references, 4 attempt references, 46 search documents,
  9,144 searchable UTF-8 bytes;
- large corpus after one disclosed authority-owned synthetic transaction:
  25,006 chats, 36 messages, 4 attachments, 25,046 search documents,
  1,309,144 searchable UTF-8 bytes;
- corrected regeneration-sibling count: 0 at both stages.

Latency, p50/p95/max milliseconds:

- ordinary derived receipt, 46 samples: `84.380 / 87.854 / 89.310`;
- 25,000-row authoritative bulk transaction: `536.201`;
- one 25,000-key derived catch-up receipt: `11,143.509`;
- global query over 25,000 matching titles: `144.522 / 145.552 / 145.621`;
- in-chat: `71.561 / 72.499 / 72.501`;
- role/model filtered: `72.334 / 73.071 / 73.720`;
- two sequential result pages: `277.428 / 285.238 / 285.722`;
- active-branch-only: `83.573 / 86.715 / 87.038`;
- attachment filename: `71.492 / 73.583 / 73.993`;
- verified attachment text: `69.868 / 71.342 / 71.898`.

The active-branch figure covers six active chains exactly six messages deep,
36 active messages total, 25,000 additional headless chats, and result limit 50;
it is not a worst-case ancestry-depth claim.

Three rebuilds took `4200.521`, `4314.693`, and `4376.664` ms. All produced
row/content SHA-256
`50c4e75d92439254d9a371a80071fb04dbfcbb4c863e9cc011864313486d92ae`.
Peak sampled temporary storage was 7,311,928 bytes.

After the bulk receipt, derived pages were 7,270,400 of 13,516,800 allocated
bytes, ratio `0.537879`; derived/(authoritative + shared) was `1.163934`. After
three rebuilds, derived pages were 7,245,824 of 13,955,072 bytes, ratio
`0.519225`; derived/(authoritative + shared) was `1.079976`.

The exact post-V8 raw JSON SHA-256 is
`ff9e63cc79f54c7ab17cdff7d84b3164e7abebc14d3694783b8b3459423d8009`;
its Markdown rendering SHA-256 is
`e36fdd863f9f5e7bc289a308964d8cfe6f615f6c8a70b50a7dc088f3cfc1c584`.
Both current corpus records correctly report zero regeneration siblings. The
earlier mislabeled raw run and its T-03 correction overlay remain preserved as
historical evidence; they are not the final candidate measurement.

No accepted threshold was supplied and this evidence does not fire authority to
replace FTS5.

## Campaign findings and preserved failures

The campaign ledger classifies every finding before repair. Material repaired
implementation defects included archive transition arming, bounded incremental
row-id collision resolution, exact Phase 7 DDL validation, durable INVALID state
after diagnostic/query corruption, and typed structural/logical corruption. The
first corruption repair displaced receipt draining; an exact four-case test failed,
the block was restored, and the corrected run passed. These are retained as
historical failures rather than rewritten as initial success.

The immutable V2 implementation review and falsification both returned BLOCKED.
Their reproduced defects were forged FTS payloads accepted by diagnostics,
attachment locations escaping query scope, uncoordinated chat identity/recency,
noncanonical/overflowing cursors, invalid-Unicode exception leakage, live read
settlement bypassing fail-closed classifiers, and cursor expiry being displayed as
index staleness. The bounded repair wave added exact public-path oracles and passed
48/48 core plus 7/7 desktop cases; the supervisor independently reran 16/16 repaired
cases before the 60-case subsystem matrix. Both BLOCKED reports and the superseded
V2 seal remain unchanged as historical evidence.

Test/evidence defects include stale pre-Phase-7 head assertions, a literal-query
negative-control error, deterministic executor-observer ordering, superseded
review seals, the post-V2 attachment/forged-key/overflow/Qt assertion corrections,
and performance harness E-01/T-01/T-02/T-03. The failed
900-second benchmark produced no accepted figures. The replacement run exited 0
in 80.424 seconds and preserved the reporting correction separately. The exact
post-V2 benchmark then exited 0 in 88.517 seconds with the corrected metric built
in. A cursor canonicalization mutant exposed one remaining stale-checkpoint oracle
ambiguity; the suffix check was moved to a matching fresh snapshot and passed.
The first post-V5 benchmark invocation correctly refused to produce evidence
without `PYTHONHASHSEED=0`; the corrected deterministic invocation exited 0 in
90.644 seconds. The first post-V8 benchmark invocation likewise correctly refused
because its explicit `--run` opt-in was omitted; no figures were accepted. The
corrected fixed-seed invocation exited 0 in 88.811 seconds.

The immutable V3 implementation review and falsification also returned BLOCKED.
They independently reproduced raw authoritative source settlement and derived
failure escaping after known business commit; the falsifier additionally found
missing derived singleton recovery and malformed filter containers. A bounded V4
repair audit then reproduced revoked derived-admission escape and cursor-generation
ABA, while the V4 oracle worker exposed archive misuse of the conversation-head
revision on headless chats. All were classified before repair. The first expanded
V4 recovery run contained one test-only inverted tie-break expectation; it was
corrected without production change, and the exact recovery oracle passed.

The immutable V4 implementation review and falsification independently returned
BLOCKED on deletion of only the derived index-state singleton: restart failed raw
schema validation before explicit rebuild was reachable. V5 retains strict
migration validation while treating exactly an absent derived singleton as
repairable startup state, exposing typed INVALID/refusal and restoring explicit
rebuild. It also binds cursors to a process-local epoch so a prior-process cursor
cannot survive durable generation-number reuse. Focused restart and mutant
oracles pass. The sandboxed post-V5 acceptance and preservation attempts failed
before rooted candidate execution with the known abstract AF_UNIX `EPERM`; both
raw failures are preserved, and exact host reruns passed 78/78 and 21/21.

The immutable V5 implementation review then proved foreign derived
schema/tokenizer versions were still reported VALID and served, while rebuild
leaked raw validation failure. The immutable V5 falsification proved default
results archived after query and active-only results made historical still
navigated despite leaving their query domains. V6 performs the locked bounded
version comparisons, normalizes derived metadata during rebuild, preserves the
two mutable visibility predicates in results, and re-resolves both against
authoritative truth at navigation. The exact repairs passed 3/3; all four added
mutants were killed both alone and in the final 36-variant matrix.

No separate Phase 6 defect, authority redesign need, product-policy conflict,
provider/network dependency, migration-history modification, Phase 8 dependency,
or other human-adjudication stop condition was established.

## Worker and review dispositions

Bounded discovery, persistence implementation, core contract, desktop, migration
fault/concurrency, search/navigation falsification, mutant, performance, and V2
repair workers produced raw reports under `work/campaign-evidence/phase7/`.

The first two closure-review runs were interrupted because their immutable seal was
superseded by later corruption-path repairs. The completed V2 implementation review
and falsification both returned BLOCKED and triggered the bounded repair wave; they
provide historical defect evidence, not closure. The V3 implementation review and
falsification also returned BLOCKED and triggered the V4 repair wave. The V4
implementation review and falsification returned BLOCKED on restart after loss of
derived singleton state and triggered V5. The earlier V4 repair-audit gaps and the
V4 closure blocker are repaired. The exact V5 implementation review and
falsification returned BLOCKED on schema/tokenizer validity/rebuild and
query-visibility navigation; V6 repairs both, with raw 81-case acceptance and
36-mutant evidence. The V5 evidence reconciliation additionally found the absent
accepted host transcript and stale static summary; both evidence defects are
repaired. The V6 implementation review then reproduced rebuild cancellation
detaching the authority-owned executor effect and losing its completion event;
the V6 falsifier reproduced restart failure on repairable foreign derived
versions. V7 keeps the rebuild grant active until the worker and event settle,
defers cancellation until then, and limits startup tolerance to repairable
derived-version mismatch while retaining strict structural validation. Exact
barrier-controlled oracles and both dedicated mutants pass. Fresh post-V7
implementation review and falsification then independently returned BLOCKED.
Both reproduced event-publication context copying: even an ordinary real-authority
rebuild committed VALID, then returned `StateError` and omitted the completion
event. Falsification also proved startup admitted malformed non-integer/non-text
version values. V8 starts event publication in a clean context while the original
tracked command awaits worker and event settlement, rejects malformed version
representations while tolerating well-typed foreign derived versions, and adds
real-authority ordinary/repeated-cancellation plus malformed-storage oracles. The
exact repair slice, 86-case matrix, preservation matrix, 40-mutant matrix, and
fixed-seed benchmark pass. The exact post-V8 seal was independently reviewed:
implementation review, falsification, and evidence reconciliation all returned
PASS with no blocker. The implementation and falsification reports include fresh
dynamic probes and did not treat prior green evidence as closure by itself.

The subsequent external re-closure BLOCKED report is immutable and established
B1-RC-01; the prior V9 PASS reviews do not close that later finding. A bounded
read-only audit by three specialists found the repair feasible inside the existing
Phase 7 transition guard and Phase 6 poison path, with no stop condition. A fresh
post-repair inspection of the implementation returned PASS and found no blocker;
its inability to execute rooted tests under the worker's AF_UNIX/native runtime
was an environment precondition, not used as dynamic evidence. The campaign's
required fresh implementation, falsification, and evidence-reconciliation
dispositions apply only to the exact final seal recorded outside this report.

## Accepted caveats and deferred items

- Performance results are fixed-seed synthetic measurements, not operator-workload
  claims or SLOs.
- The live high-water is defence in depth, not a durable second ledger. Primary
  prevention is the exact armed update trigger on every admitted connection.
  Arbitrary offline file replacement outside valid live operation remains outside
  this bounded anti-tamper repair; startup coherently establishes its observation
  from a structurally valid persisted singleton.
- The earlier raw performance JSON requires its explicit T-03 correction overlay;
  the final post-V8 run reports the corrected field directly. All raw bytes remain
  preserved.
- Python 3.14 emits upstream pytest-asyncio/qasync deprecation warnings in tests.
- The strengthened twice-cancelled real-authority test was run directly in the
  final 7/7 exact slice. There is no dedicated mutant whose only defect is failure
  on the second cancellation; independent backpressured-event falsification
  issued repeated cancellations across both worker and event barriers.
- A concurrent application close may make an already-admitted rebuild return the
  typed closed-EventBus error after its worker settles; independent review found
  no detached effect, false success, or post-revocation admission.
- The complete repository suite is intentionally deferred to Mick's external
  closure/pre-commit gate.
- Phase 8 provenance/inspection UX and every explicitly excluded future capability
  remain out of scope.

## Candidate paths

The candidate comprises the following 35 source, migration, test, and
current-state documentation paths. Final per-file hashes and patch/manifest seals
are retained in the campaign evidence package rather than embedded in this
self-referential candidate document:

```text
README.md
docs/ARCHITECTURE.md
docs/DEVELOPMENT.md
docs/LINUX_V0_1_DESIGN.md
docs/LINUX_V0_1_PHASE7_IMPLEMENTATION_REPORT.md
docs/ROADMAP.md
docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md
src/bots5/core/application.py
src/bots5/core/errors.py
src/bots5/core/ports.py
src/bots5/desktop/theme.py
src/bots5/desktop/widgets.py
src/bots5/desktop/window.py
src/bots5/domain/models.py
src/bots5/domain/search.py
src/bots5/infrastructure/persistence/migration_runner.py
src/bots5/infrastructure/persistence/migrations/versions/0010_phase7_search_navigation.py
src/bots5/infrastructure/persistence/phase6_schema.py
src/bots5/infrastructure/persistence/phase7_schema.py
src/bots5/infrastructure/persistence/phase7_validation.py
src/bots5/infrastructure/persistence/schema.py
src/bots5/infrastructure/persistence/search.py
src/bots5/infrastructure/persistence/sqlite.py
src/bots5/infrastructure/persistence/transition_guard.py
tests/mutants/run_phase7_search_mutants.py
tests/performance/run_phase7_search_benchmark.py
tests/test_phase1_persistence.py
tests/test_phase2_persistence.py
tests/test_phase3_generation.py
tests/test_phase5_provider_model.py
tests/test_phase7_core_contracts.py
tests/test_phase7_desktop.py
tests/test_phase7_migration_authority_faults.py
tests/test_phase7_search_navigation.py
tests/test_phase7_v4_repairs.py
```

Evidence and immutable seals are stored outside the candidate path list under
`work/campaign-evidence/phase7/`.

## Authority retained by Mick

Mick retains substantive acceptance, external closure review, the complete
pre-commit suite gate, commit approval, and push approval. This campaign performs
none of those actions.
