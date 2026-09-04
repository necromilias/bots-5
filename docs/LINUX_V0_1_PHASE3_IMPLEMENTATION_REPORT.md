# B.O.T.S. Linux v0.1 Phase 3 implementation report

Date: 2026-09-05

Classification: **IMPLEMENTED, DETERMINISTICALLY VALIDATED, MANUAL LIVE-QWEN ACCEPTANCE EVIDENCE RECORDED, ROUNDS 4-20 IN-SCOPE BLOCKERS REPAIRED; FRESH SOL/XHIGH ROUND 21 AUDIT PASS; HUMAN-CLOSED AND ACCEPTED; RECOVERY-ARTIFACT FINDING REMAINS A SEPARATE HUMAN-ADJUDICATION BOUNDARY**

## Authority and repository state

- Checkout: `/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1`
- Branch: `main`
- Approved baseline: `1660ac57af5c845fb6f11b9eb99a4fae797a1706`
- Current base remains that baseline; no refs were altered.
- Phase 1 and Phase 2 remain closed.
- Phase 3 is human-closed but remains unstaged and uncommitted pending separate landing approval. No paid provider call or OpenRouter call was made. The
  separate operator live-Qwen acceptance used an explicitly corrected local Ollama endpoint; its
  evidence is recorded below. No commit, push, merge, or OrgMem change was made.

The projectless directory supplied for this task was an empty Codex output directory rather than the
repository. Implementation was made in the existing clean checkout above, which matched the approved
baseline exactly before editing.

## Exact implementation inventory

### Core and domain

- `src/bots5/core/generation.py`: adds typed dispatch, metadata, and streaming terminal events while
  retaining the existing `GenerationBackend` shape.
- `src/bots5/core/urls.py`: provides the shared canonical HTTP base-URL spelling used by providers,
  the B.O.T.S. backend, and Phase 3 persistence validation.
- `src/bots5/core/application.py`: freezes provider/backend/model/endpoint/auth-name request metadata,
  persists metadata before observable progress, adds `cancel_generation`, rejects late deltas after a
  cancellation request, and records remote uncertainty.
- `src/bots5/domain/models.py`: adds nullable normalized outcome metadata to `GenerationAttempt`.
- `src/bots5/desktop/window.py`: adds the operator Stop action without adding provider/model settings UI.

### Provider and backend

- `src/bots5/providers/base.py`: adds the streaming provider event seam; `Provider.complete()` remains.
- `src/bots5/providers/openai_compatible.py`: parses ordinary OpenAI-compatible SSE, sends exactly
  `stream=true`, omits `stream_options.include_usage`, normalizes optional usage/cost, and closes the
  HTTP context on task cancellation.
- `src/bots5/providers/openrouter.py`: adds the same streaming seam and normalization for the stable
  `openrouter` provider identity; this path was not invoked.
- `src/bots5/infrastructure/generation/openai_compatible.py`: B.O.T.S.-owned
  `openai_compatible_http` backend adapter.
- `src/bots5/bootstrap/desktop.py`: keeps fake as default and adds explicit
  `--backend local_openai --base-url URL --model MODEL [--api-key-env NAME]` startup selection.

### Persistence

- `src/bots5/infrastructure/persistence/migrations/versions/0005_generation_outcomes.py`: additive
  nullable migration over `generation_attempts`, including current-schema outcome/provenance
  validation and the strengthened attempt trigger.
- `src/bots5/infrastructure/persistence/phase3_validation.py`: shared Phase 3 snapshot and outcome
  invariants used at attempt creation/finalization/update, row reads, migration, and authoritative
  startup validation; historical Phase 1/2 snapshot semantics remain permissive, including the
  legacy fake snapshot shape.
- `src/bots5/infrastructure/persistence/migration_runner.py`: advances schema head to `0005` while
  retaining support for pre-Phase-3 revisions.
- `src/bots5/infrastructure/persistence/schema.py` and `sqlite.py`: map, validate, persist, and read
  nullable provider/outcome metadata, preserve null historical values, and reconcile running work
  according to its durable dispatch boundary.

### Tests and documentation

- `tests/test_phase3_generation.py`: deterministic streaming telemetry, no alternate request shape,
  absent-telemetry nulls, cancellation, partial preservation, late-delta protection, metadata ordering,
  cost validation, canonical hostname/URL-boundary validation, empty-stream failure, authoritative
  snapshot/outcome rejection and acceptance, historical compatibility, current-schema provenance
  tamper and provider-identity-erasure rejection, rejected terminal-metadata fallback, dispatch-uncertainty preservation, legacy
  `Provider.complete()` payload preservation, legacy snapshot migration, live row-read provenance,
  and malformed current-schema rejection.
- `tests/test_phase3_local_qwen.py`: opt-in local acceptance path; skipped unless
  `BOTS5_PHASE3_QWEN_BASE_URL` and `BOTS5_PHASE3_QWEN_MODEL` are explicitly supplied. The test is
  the only marked exception to the ordinary no-live-network fixture and requires a completed
  `finish_reason=stop` result.
- `tests/conftest.py` and `pyproject.toml`: isolate the explicitly marked local-Qwen acceptance test
  from the ordinary socket blocker and register its marker.
- `tests/test_phase1_desktop.py`, `tests/test_phase1_persistence.py`, `tests/test_phase2_core.py`, and
  `tests/test_phase2_persistence.py`: updated Phase 1/2 regression expectations for schema head and
  restart uncertainty.
- `README.md`, `docs/ARCHITECTURE.md`, `docs/EXECUTION.md`, and `docs/ROADMAP.md`: record the bounded
  Phase 3 scope and opt-in operation.

## Migration and schema changes

`0005_generation_outcomes` adds these nullable columns to the existing `generation_attempts` table:

`provider_id`, `returned_model`, `request_id`, `finish_reason`, `prompt_tokens`,
`completion_tokens`, `reasoning_tokens`, `total_tokens`, `known_cost_usd`, and
`remote_outcome_unknown`.

No outcome table was introduced. Pre-Phase-3 rows remain readable with null values. New real attempts
use provider IDs `local_openai` or `openrouter` and backend ID `openai_compatible_http`. The snapshot
stores the normalized base URL and optional authentication environment-variable name, never the
resolved secret. Although the additive uncertainty column remains nullable for historical rows, new
Phase 3 attempts must explicitly persist `false` before dispatch or `true` once dispatch may have
occurred.

The migration replaces the attempt lifecycle update trigger so identity remains immutable and terminal
outcome metadata cannot be changed after finalization. Its current-schema validator checks Phase 3
snapshot/provider/backend/model/prompt consistency and normalized outcome fields before startup can
make the store authoritative. Exact `finish_reason=stop` is required for Phase 3 `COMPLETE`; valid
non-stop incomplete, failed, and aborted outcomes remain representable. Existing Phase 1/2 integrity
guards remain in place.

## Validation results

Baseline validation before editing: **163 passed**.

Targeted Phase 3 and Phase 1/2 regression checks passed; the opt-in Qwen test remained skipped.

Full deterministic suite after implementation:

```text
180 passed, 1 skipped
```

The one skip is the deliberately opt-in local-Qwen acceptance test. The current collection contains
181 tests. The independent partition results were 16 Phase 3 tests passed, 51 Phase 1/2 tests passed,
and 113 legacy campaign/provider/runner tests passed. `git diff --check` passed and compileall passed.
The full suite included the legacy campaign/provider/runner tests; no campaign manifest or
`Provider.complete()` contract was converted to streaming.

Documentation-pass rerun on 2026-09-04 produced the same zero-spend results: the full suite was
`180 passed, 1 skipped`; the Phase 3 focused suite was `16 passed, 1 skipped`; the Phase 1/2
regression suite was `51 passed`; and the legacy preservation suite was `113 passed`. All three
legacy manifests returned exit `0` with `OK` results. `compileall` and `git diff --check` both
returned exit `0`. `OPENROUTER_API_KEY` was unset for every command; ordinary tests had live sockets
blocked by the autouse fixture, the marked local-Qwen test skipped without explicit endpoint/model
variables, and manifest validation performed no run or provider dispatch. No provider/API/network
request was made by this closure-validation pass.

Bounded-repair validation on 2026-09-04 completed after the Round 4 repairs:

```text
full deterministic suite: 198 passed, 1 skipped (199 collected)
Phase 3 focused suite: 34 passed, 1 skipped (35 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The Phase 3 focused suite now directly covers authoritative-store rejection of empty, malformed,
and incomplete real-provider snapshots; valid snapshot acceptance; historical compatibility; exact
`stop` completion; rejection of non-stop, missing, and malformed complete finishes; incomplete and
aborted remote-unknown persistence; valid current-schema open; and startup rejection of contradictory
provider/backend/model/prompt provenance. The matrix ran with `OPENROUTER_API_KEY` and all opt-in
Qwen variables unset. The marked Qwen test skipped, ordinary tests retained live socket blocking,
manifest validation was offline, and no provider/API/network request was made.

## Bounded repair after Round 4

Round 4 reproduced three accepted-scope persistence-authority defects: a new real-provider attempt
could be inserted with an empty request snapshot; a real-provider attempt could be finalized as
`COMPLETE` with a non-stop or missing finish reason; and current-schema startup did not validate
duplicated Phase 3 provenance against the canonical snapshot. These were implementation defects, not
contradictions in the approved Phase 3 design.

The bounded repair adds no persistence model or provider policy. The existing store now rejects
malformed or incomplete Phase 3 snapshots at start, finalization, update, and read boundaries while
retaining historical Phase 1/2 compatibility. The exact current user message is checked against the
snapshot prompt. Phase 3 `COMPLETE` requires exact `finish_reason=stop`; incomplete, failed, and
aborted outcomes retain their accepted semantics, including partial output and remote uncertainty.
Current-schema open validates provider/backend/model/prompt/base URL and outcome metadata before
refreshing the strengthened SQLite attempt triggers. No secret value is added to the snapshot.

Focused repair coverage now includes 36 Phase 3 tests (with the one opt-in local-Qwen test skipped),
including public-store rejection/acceptance, exact-stop completion, non-stop incomplete persistence,
aborted remote-unknown persistence, historical compatibility, valid current-schema open, and
tampered provider/backend/model/prompt startup rejection. The final full matrix and independent
post-repair audit are recorded below after they run; final human closure remains pending.

## Bounded repair after Round 5

Fresh Sol round 5 found two further genuine persistence-authority defects in the trigger path. A
Phase 3 snapshot with an unknown credential-shaped key such as `access_token` was accepted, and
direct SQLite writes could commit a malformed Phase 3 base URL, forbidden `api_key`, or empty
`request_id` before a later startup/read rejection. These findings contradicted the locked non-secret
snapshot rule and the requirement that current-schema outcome/provenance constraints hold at the
authoritative boundary; neither required architecture change, dependency, scope expansion, or policy
reversal.

The bounded repair closes the Phase 3 snapshot schema over the accepted request fields plus optional
`api_key_env`, and registers shared snapshot/outcome validators with the existing connection-local
SQLite guard. The 0005 attempt triggers now invoke those validators for inserts and updates, so
secret-shaped unknown fields, malformed URLs, and invalid auxiliary outcome values are rejected before
commit. Historical Phase 1/2 snapshots remain validated under their earlier permissive semantics.

Post-repair validation passed with the full suite at `200 passed, 1 skipped` (201 collected), the
Phase 3 focused suite at `36 passed, 1 skipped` (37 collected), the Phase 1/2 suite at `51 passed`,
and the legacy preservation suite at `113 passed`; compileall, all three offline manifests, and
`git diff --check` returned exit `0`. All provider/OpenRouter/Qwen variables were unset, the marked
Qwen test skipped, and no provider/API/network request was made. A new independent Sol/xhigh audit
is required next; final human closure is still pending.

## Manual operator live-Qwen acceptance evidence

This section records operator evidence supplied after the deterministic validation and independent
Sol/xhigh review. It is not a replacement for either evidence layer, and it is not the final human
closure decision.

1. Initial attempts using the wrong local endpoint failed as expected and were preserved as failed
   attempts. These are recorded as operator endpoint/configuration failures, not implementation
   defects; no repository evidence currently contradicts that classification.
2. After the local Ollama endpoint was corrected, B.O.T.S. completed a real local Qwen generation.
   The selected provider/backend identities were `local_openai` and `openai_compatible_http`.
3. The successful Qwen attempt reached `COMPLETE` with terminal `finish_reason=stop`.
4. A subsequent longer real Qwen generation was manually stopped mid-stream. The message/attempt
   reached `ABORTED`, and the already committed partial output remained visible and preserved.
5. B.O.T.S. was closed and reopened. Both the completed Qwen response and the aborted partial
   response persisted across restart.
6. No automatic retry occurred. No paid-provider or OpenRouter request occurred.

The evidence supplied for this documentation pass does not include exact endpoint text, model
identifier, timestamps, attempt IDs, or a telemetry payload, so this report does not invent those
details. The manual acceptance is operator-observed evidence of the local completion, cancellation,
partial-preservation, and restart paths; it does not claim a throughput/backpressure benchmark.

## Bounded repair after Round 6

Fresh Sol round 6 found four issues. Three were accepted Phase 3 implementation defects: surrounding
whitespace was accepted in the authoritative normalized base URL; an empty streamed terminal finish
reason could strand a generation as running after the persistence rejection; and Phase 3 outcome
validation accepted contradictory combinations such as failed/aborted with `stop`, running with a
finish reason, or complete with `remote_outcome_unknown=true`. These were repaired without changing
the provider IDs, backend ID, cancellation policy, retry policy, context scope, or streaming design.

The fourth finding reproduced a pre-existing recovery-artifact interaction during a sequential
Phase 2-to-Phase 3 upgrade: a schema-0001 recovery point can be retained after a later upgrade to
0004, causing the candidate 0005 upgrade to reject the stale recovery point. It is outside the three
authorized persistence-authority repairs and may change the closed Phase 1/2 recovery semantics, so
it was not modified. It remains a human-adjudication item and prevents a final closure claim here.

The bounded repair adds URL whitespace validation, rejects empty streamed finish reasons in both
OpenAI-compatible provider normalizers, converts malformed terminal completion events into durable
`FAILED` outcomes with a null finish reason, and strengthens shared Phase 3 outcome-state validation.
New deterministic tests cover the public store, current-schema startup, provider SSE, durable failure,
and restart/readability paths.

Post-repair validation on 2026-09-04 passed with the full suite at `208 passed, 1 skipped` (209
collected), the Phase 3 focused suite at `44 passed, 1 skipped` (45 collected), the Phase 1/2 suite
at `51 passed`, and the legacy preservation suite at `113 passed`. Compileall, all three offline
manifests, and `git diff --check` returned exit `0`. All provider/OpenRouter/Qwen variables were
unset, the marked Qwen test skipped, and no provider/API/network request was made.

## Bounded repair after Round 7

Fresh Sol round 7 found two further accepted-scope defects and one documentation seal defect. A
duplicate JSON key could be collapsed by parsing before validation, allowing a secret-bearing first
`api_key_env` value to disappear behind a later null value. Also, an out-of-range non-negative usage
integer could pass provider normalization, overflow SQLite during metadata persistence, and strand
the generation before terminal failure. The report's tracked-diff hash was stale after the Round 6
repair and was corrected as documentation-only evidence.

The bounded repair rejects duplicate snapshot keys recursively before Phase 3 validation and limits
provider usage counters to SQLite's signed 64-bit integer range. Out-of-range provider telemetry is
represented as unknown/null, preserving the honest-telemetry policy and allowing the generation to
terminalize normally. Current-schema startup uses the same duplicate-key validator. No provider
identity, backend identity, cancellation, retry, context, or streaming policy changed.

Round 7 also independently reconfirmed the retained Phase 2 recovery-point interaction during a
sequential 0004-to-0005 upgrade. It remains outside the authorized repair set and requires human
adjudication before any recovery-runner change.

Post-Round-7-repair focused validation passed at `47 passed, 1 skipped` (48 collected). The final
zero-spend rerun after the code and documentation updates passed as follows:

```text
full deterministic suite: 211 passed, 1 skipped (212 collected)
Phase 3 focused suite: 47 passed, 1 skipped (48 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The variables `OPENROUTER_API_KEY`, `BOTS5_PHASE3_QWEN_BASE_URL`, `BOTS5_PHASE3_QWEN_MODEL`, and
`BOTS5_PHASE3_QWEN_API_KEY_ENV` were unset. The marked Qwen test skipped, ordinary tests retained
live socket blocking, and no provider/API/network request occurred. A fresh independent audit after
these Round 7 repairs is required; final human closure remains pending.

## Bounded repair after Round 8

Fresh Sol round 8 returned `BLOCKED` with three further accepted-scope defects. The authoritative
outcome validator allowed a known non-stop finish with `remote_outcome_unknown=true` and allowed
failure metadata on running or complete attempts. Provider/backend construction could accept a
non-canonical base URL whose effective HTTP dispatch spelling differed from the immutable snapshot.
Finally, an ordinary streamed `finish_reason=stop` with no non-whitespace output was persisted as a
successful empty completion even though the existing provider completion semantics reject an empty
model response. These were implementation defects, not contradictions in the approved Phase 3
design.

The bounded repair extends the existing shared outcome validator, current-schema startup check, and
SQLite trigger to reject known-finish/remote-unknown and running/complete/failure-metadata
contradictions. It adds one small shared canonical URL helper used by both OpenAI-compatible
providers, the owned backend, and Phase 3 snapshot validation; configured variants are normalized
before dispatch and persisted only in their canonical spelling. The owned streaming backend now
turns an empty `stop` response into a durable `FAILED` outcome while preserving the existing
ordinary `stream=true` request and event ordering. No persistence model, provider identity, backend
identity, cancellation, retry, context, or streaming architecture changed.

Round 8 also independently reconfirmed the retained Phase 2 recovery-point interaction during a
sequential 0004-to-0005 upgrade. It remains outside the authorized repair set and requires human
adjudication before any recovery-runner change; no recovery code was modified.

Post-Round-8-repair validation passed as follows:

```text
full deterministic suite: 218 passed, 1 skipped (219 collected)
Phase 3 focused suite: 54 passed, 1 skipped (55 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The variables `OPENROUTER_API_KEY`, `BOTS5_PHASE3_QWEN_BASE_URL`, `BOTS5_PHASE3_QWEN_MODEL`, and
`BOTS5_PHASE3_QWEN_API_KEY_ENV` were unset. The marked Qwen test skipped, ordinary tests retained
live socket blocking, manifest validation was offline, and no provider/API/network request occurred.
A fresh independent Round 9 closure audit then returned `BLOCKED` on two further in-scope defects;
those defects and their bounded repairs are recorded below. Final human closure remains pending.

## Bounded repair after Round 9

Fresh Sol Round 9 returned `BLOCKED` with two further in-scope implementation defects. The shared
HTTP base-URL helper allowed malformed hostnames such as `exa mple.invalid` and retained Unicode
hostnames in the snapshot even though HTTPX used an encoded or IDNA-normalized spelling for effective
dispatch. This violated the canonical non-secret endpoint requirement and could make persisted
provenance disagree with the request actually sent.

The same audit also reproduced a terminal finalization failure path: a backend emitted a known
`finish_reason=length` together with `remote_outcome_unknown=true`; the authoritative validator
correctly rejected that combination, but the application fallback attempted `FAILED` finalization
while retaining `finish_reason=length`, which the validator also rejected. The attempt remained
`RUNNING` and the message `STREAMING` with committed partial output.

The bounded repair tightens the existing shared URL helper with hostname-label validation, IDNA
canonicalization, and IP address validation, so persistence and both OpenAI-compatible providers use
the same effective host spelling. The application fallback now clears terminal finish metadata before
persisting a failed outcome. No provider ID, backend ID, cancellation, retry, context, or streaming
architecture changed; the retained Phase 2 recovery-artifact interaction remains untouched and
requires human adjudication.

Post-Round-9-repair validation passed as follows:

```text
full deterministic suite: 220 passed, 1 skipped (221 collected)
Phase 3 focused suite: 56 passed, 1 skipped (57 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The test matrix explicitly unset the OpenRouter key and all local-Qwen opt-in variables. Ordinary
tests retained socket blocking, the marked Qwen test skipped, manifests were validated offline, and
no provider/API/network request was made. A fresh independent Round 10 Sol/xhigh closure audit is
required; final human closure remains pending.

## Bounded repair after Round 10

Fresh Sol Round 10 returned `BLOCKED` with two further in-scope persistence defects and one stale
documentation count. After a dispatch marker had durably set `remote_outcome_unknown=true`, the
public store and SQLite trigger allowed that uncertainty to be cleared while the attempt remained
`RUNNING` or when it was finalized as `ABORTED`; reopening the resulting database accepted the
contradiction. The same audit found that malformed path percent escapes such as `%ZZ` were accepted
as canonical Phase 3 base URLs by the store, startup validation, and provider path. It also found
that the report's current 220/56 test counts did not match the exact sealed candidate's 222/58 counts.

The bounded repair adds an existing-boundary transition validator and its connection-local SQLite
function so a dispatch-uncertain running attempt cannot be changed back to known and an aborted
attempt retains uncertainty. A known terminal completion, incomplete result, or provider failure may
still resolve the current outcome to `remote_outcome_unknown=false`, preserving the accepted
distinction between in-flight uncertainty and a known terminal result. The URL helper now rejects
malformed percent escapes before canonicalization. The report counts and audit history were refreshed.

No provider ID, backend ID, cancellation, retry, context, or streaming architecture changed. The
retained Phase 2 recovery-artifact interaction remains untouched and requires human adjudication.

Post-Round-10-repair validation passed as follows:

```text
full deterministic suite: 225 passed, 1 skipped (226 collected)
Phase 3 focused suite: 61 passed, 1 skipped (62 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The test matrix explicitly unset the OpenRouter key and all local-Qwen opt-in variables. Ordinary
tests retained socket blocking, the marked Qwen test skipped, manifests were validated offline, and
no provider/API/network request was made. A fresh independent Round 11 Sol/xhigh closure audit is
required; final human closure remains pending.

## Bounded repair after Round 11

Fresh independent Sol/xhigh Round 11 (worker task `01a06c31-fbda-72d1-ab8e-d82523a46ac6`) returned
`BLOCKED` on three in-scope implementation defects. The audit reproduced a deterministic backend
emitting a dispatched partial response followed by `GenerationCompleted(finish_reason="stop",
prompt_tokens=-1)`: terminal validation rejected the negative counter, the generic failure fallback
retained it, and the fallback was rejected again, leaving durable `RUNNING`/`STREAMING` state.
It also finalized a dispatched attempt as `INCOMPLETE` with no finish reason while clearing
`remote_outcome_unknown`, which committed and reopened with false uncertainty. Finally, it compared
the baseline provider payloads and found that the new shared payload builder had changed both
non-streaming providers by omitting an empty system message from `Provider.complete()`.

These findings were genuine contradictions against the accepted Phase 3 authority and were repaired
without changing the persistence model, provider IDs, backend identity, streaming policy, cancellation
truth, retry policy, context scope, or legacy campaign seam:

- The application failure fallback now clears all optional returned-model, request-ID, finish, usage,
  and cost metadata before persisting a durable `FAILED` attempt, while retaining partial message text
  and dispatch uncertainty.
- The existing transition validator, public store paths, SQLite connection-local guard, and `0005`
  update trigger now receive the candidate finish reason and reject clearing dispatch uncertainty for
  an `INCOMPLETE` result with no known finish. Known terminal outcomes remain permitted to resolve
  uncertainty, and `RUNNING`/`ABORTED` preservation remains unchanged.
- Both providers preserve the baseline `Provider.complete()` payload, including an empty system
  message. Only the new streaming path requests the Phase 3 exact-user-message shape.
- Focused regressions cover invalid terminal telemetry fallback with restart, uncertainty resolution
  without a finish, and both providers' empty-system non-streaming payloads.

Post-Round-11-repair validation passed as follows:

```text
full deterministic suite: 229 passed, 1 skipped (230 collected)
Phase 3 focused suite: 65 passed, 1 skipped (66 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen opt-in variables. The marked
local-Qwen test skipped, ordinary tests retained live socket blocking, manifest validation was
offline, and no provider/API/network request was made. Round 11's read-only audit reported one
ignored `src/bots5/core/__pycache__/urls.cpython-312.pyc` cache refresh from its offline URL probe;
no candidate source, index, commit, or ref changed. A fresh independent Round 12 Sol/xhigh closure
audit is required; final human closure remains pending.

## Bounded repair after Round 12

Fresh independent Sol/xhigh Round 12 (worker task `01a06c49-90ba-7791-82af-8297961c2a5c`) returned
`BLOCKED` on four in-scope compatibility and authority defects. The default fake application had
started serializing three nullable Phase 3 fields into the historical six-field fake request
snapshot. The Phase 3 classifier also treated a legacy-only non-null `base_url` as a real Phase 3
record, causing migration rejection of an otherwise valid Phase 1/2 database. A live open store
could read a Phase 3 snapshot whose prompt no longer matched the referenced user message. Finally,
`SQLiteAppStateStore.open()` accepted an Alembic `0005_generation_outcomes` stamp when the additive
outcome columns were absent.

These findings were genuine contradictions against the accepted compatibility and authoritative
startup requirements and were repaired without changing the Phase 3 architecture or legacy
campaign/provider seam:

- Request snapshots now omit `None` optional fields, restoring the baseline fake six-field snapshot
  while retaining configured Phase 3 provenance.
- A non-null snapshot `base_url` alone no longer classifies a record as Phase 3; the real Phase 3
  provider/backend identity still does. Historical permissive snapshot validation remains intact.
- Attempt row reads now join the referenced user message and validate the Phase 3 snapshot prompt
  against its exact current content, rejecting tampered live state rather than returning it.
- Current-schema startup now requires all ten additive Phase 3 outcome columns whenever the database
  is stamped `0005_generation_outcomes`, while earlier schema revisions remain readable/migratable.
- Focused regressions cover fake snapshot compatibility, historical `base_url` migration, live row-read
  prompt provenance, and malformed current-schema stamps.

Post-Round-12-repair validation passed as follows:

```text
full deterministic suite: 233 passed, 1 skipped (234 collected)
Phase 3 focused suite: 69 passed, 1 skipped (70 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen opt-in variables. The marked
local-Qwen test skipped, ordinary tests retained live socket blocking, manifest validation was
offline, and no provider/API/network request was made. The retained Phase 2 recovery-artifact
interaction remains outside this repair authority and requires human adjudication. A fresh
independent Round 13 Sol/xhigh closure audit returned `BLOCKED`; its bounded repair and the
post-repair validation are recorded below. Final human closure remains pending.

## Bounded repair after Round 13

Fresh independent Sol/xhigh Round 13 (worker task `01a06c5e-373f-77d0-87fd-400340867125`) returned
`BLOCKED` on two in-scope persistence-authority defects. The new Phase 3 validation path was also
being applied to historical snapshots during the 0005 migration: a baseline-readable trailing-slash
`base_url`, duplicate legacy field, or credential-shaped legacy field could prevent an otherwise valid
Phase 1/2 database from upgrading. In addition, a new real Phase 3 attempt could retain
`remote_outcome_unknown=NULL` while running or after becoming aborted; that value survived the public
store, SQLite trigger, read, and current-schema reopen boundaries.

These were repaired within the existing Phase 3 persistence/domain boundaries:

- Historical snapshots are parsed and checked using their earlier permissive semantics. Duplicate-key
  and forbidden-secret-key rejection is applied only after the record is identified as Phase 3, while
  the legacy URL check continues to accept the baseline trailing-slash form. Phase 3 snapshots still
  receive strict duplicate/secret/schema/canonical-URL validation.
- New Phase 3 attempts must carry explicit boolean remote-outcome truth (`false` before dispatch or
  `true` once dispatch may have occurred). The additive database column remains nullable so historical
  Phase 1/2 rows remain readable. The 0005 insert/update triggers reject null uncertainty for Phase 3
  rows, and the public store applies the same rule.
- Focused regressions cover historical migration/readability with the legacy snapshot shapes and
  authoritative public-store plus SQLite-trigger rejection of null Phase 3 uncertainty.

Post-Round-13-repair validation passed as follows:

```text
full deterministic suite: 234 passed, 1 skipped (235 collected)
Phase 3 focused suite: 70 passed, 1 skipped (71 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen variables. The marked local-Qwen
test skipped, ordinary tests retained live socket blocking, and the three manifests performed offline
validation only. No provider/API/network request was made. The retained Phase 2 recovery-artifact
interaction remains outside this repair authority and requires human adjudication. A fresh
independent Round 14 Sol/xhigh closure audit is required; final human closure remains pending.

## Bounded repair after Round 14

Fresh independent Sol/xhigh Round 14 (worker task `01a06c76-6ce0-74b0-82b9-fccbb48543cf`) returned
`BLOCKED` on one remaining in-scope historical-compatibility defect. Pre-Phase-3 `0001` databases
with snapshot-only `provider_id="legacy-provider"` or an opaque legacy `base_url` were accepted by
the baseline validator but were still classified or validated as Phase 3 during the 0005 migration,
so upgrade failed and the database was restored to `0001_desktop_state`.

The bounded repair removes snapshot-content-based Phase 3 classification and removes new URL
validation from the historical path. Phase 3 strict validation continues to use the persisted
provider/backend identity, so real Phase 3 records retain mandatory provider, backend, model, prompt,
canonical endpoint, duplicate-key, and non-secret invariants. Historical snapshots retain their prior
unknown-field, duplicate-key, credential-shaped-field, and opaque-URL readability semantics. No
provider, backend, cancellation, retry, streaming, context, or later-phase policy changed.

The migration/readability regression now covers the baseline trailing-slash URL, snapshot-only legacy
provider field, opaque legacy URL, duplicate legacy field, and credential-shaped legacy field. The
Round 13 explicit Phase 3 remote-outcome truth repair remains present and covered.

Post-Round-14-repair validation passed as follows:

```text
full deterministic suite: 236 passed, 1 skipped (237 collected)
Phase 3 focused suite: 72 passed, 1 skipped (73 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen variables. The marked local-Qwen
test skipped, ordinary tests retained live socket blocking, and the three manifests performed offline
validation only. No provider/API/network request was made. The retained Phase 2 recovery-artifact
interaction remains outside this repair authority and requires human adjudication. A fresh
independent Round 15 Sol/xhigh closure audit is required; final human closure remains pending.

## Bounded repair after Round 15

Fresh independent Sol/xhigh Round 15 (worker task `01a06c88-bbb1-7262-a354-a99dc009d686`) returned
`BLOCKED` on two in-scope Phase 3 defects. Current-schema startup checked only that all ten additive
outcome columns existed, so a database stamped `0005_generation_outcomes` with a non-nullable
`provider_id` was accepted and later broke the default fake path. Separately, a streamed terminal
`finish_reason="length"` followed by contradictory `finish_reason="stop"` was overwritten to a false
successful `COMPLETE/stop` result that survived restart.

The bounded repairs preserve the accepted architecture and policy:

- Authoritative startup now checks the `PRAGMA table_info` nullability flag for every additive Phase 3
  outcome column and rejects a stamped current schema unless all ten remain nullable.
- The existing B.O.T.S.-owned OpenAI-compatible streaming adapter now treats differing non-null
  terminal finish reasons as a malformed provider response. The application’s existing durable failure
  fallback records `FAILED` with a null finish reason, preserves committed partial output, and retains
  remote uncertainty; an unambiguous exact `stop` is still the only completion path.
- Focused regressions cover non-nullable stamped schemas and contradictory streamed finish signals,
  including restart/readability of the durable failed partial result.

Post-Round-15-repair validation passed as follows:

```text
full deterministic suite: 238 passed, 1 skipped (239 collected)
Phase 3 focused suite: 74 passed, 1 skipped (75 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen variables. The marked local-Qwen
test skipped, ordinary tests retained live socket blocking, and the three manifests performed offline
validation only. No provider/API/network request was made. The retained Phase 2 recovery-artifact
interaction remains outside this repair authority and requires human adjudication. A fresh
independent Round 16 Sol/xhigh closure audit is required; final human closure remains pending.

## Bounded repair after Round 16

Fresh independent Sol/xhigh Round 16 (worker task `01a06ca1-8ae3-7910-bbde-342d8635a675`) returned
`BLOCKED` on two in-scope runtime/data-contract defects and one closure-evidence defect. A valid
historical Phase 1/2 row whose unrestricted legacy `backend_id` happened to equal
`openai_compatible_http` was classified as Phase 3 during the 0005 upgrade and rejected for missing
provider provenance. Separately, explicit cancellation persisted `ABORTED` but then remained blocked
waiting to publish `generation_aborted` into a full bounded EventBus subscriber queue. The report’s
tracked-diff seal also contained the pre-R15 hash rather than the current candidate hash.

The bounded repairs preserve the accepted architecture and policy:

- Migration, current-schema, and persisted-row validation now classify historical versus Phase 3 state
  from persisted provider identity. New writes still treat the Phase 3 backend marker as strict and
  reject a missing provider or mandatory snapshot provenance, so the compatibility exception applies
  only to provider-null persisted historical rows.
- The existing generation task now signals immediately after terminal state is durably finalized.
  `cancel_generation()` waits for that durability signal and may return while the already-ordered
  terminal event remains subject to the existing EventBus backpressure. The event is still delivered
  when the subscriber drains; no event coalescing or second notification architecture was added.
- The report seal was refreshed to the current candidate hash, and focused regressions cover the
  historical backend collision and cancellation under a full queue.

Post-Round-16-repair validation passed as follows:

```text
full deterministic suite: 240 passed, 1 skipped (241 collected)
Phase 3 focused suite: 76 passed, 1 skipped (77 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen variables. The marked local-Qwen
test skipped, ordinary tests retained live socket blocking, and the three manifests performed offline
validation only. No provider/API/network request was made. The retained Phase 2 recovery-artifact
interaction remains outside this repair authority and requires human adjudication. A fresh
independent Round 17 Sol/xhigh closure audit is required; final human closure remains pending.

## Bounded repair after Round 17

Fresh independent Sol/xhigh Round 17 (worker task `01a06cc0-5b93-7ac1-abb5-a1bd299a9cbb`) returned
`BLOCKED` on one in-scope cancellation race. If completion had already been durably finalized while
its terminal event was waiting on a full bounded EventBus queue, `cancel_generation()` could still
cancel the task and lose `generation_completed`, leaving durable `COMPLETE/stop` without its
corresponding observable terminal event.

The bounded repair keeps the existing ordered/backpressured EventBus and terminal-state policy:

- `cancel_generation()` now checks the terminal-durability signal before cancelling an active task. A
  request that reaches durable terminal state first is allowed to retain its terminal event
  publication; cancellation still cancels work that has not reached terminal durability.
- The focused regression covers a full queue, durable `COMPLETE/stop`, late cancellation, and ordered
  terminal-event delivery. No event coalescing, retry, or second notification architecture was added.

Post-Round-17-repair validation passed as follows:

```text
full deterministic suite: 241 passed, 1 skipped (242 collected)
Phase 3 focused suite: 77 passed, 1 skipped (78 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The matrix explicitly unset `OPENROUTER_API_KEY` and all local-Qwen variables. The marked local-Qwen
test skipped, ordinary tests retained live socket blocking, and the three manifests performed offline
validation only. No provider/API/network request was made. The retained Phase 2 recovery-artifact
interaction remains outside this repair authority and requires human adjudication. A fresh
independent Round 18 Sol/xhigh closure audit is required; final human closure remains pending.

## Bounded repair after Round 18

Fresh independent Sol/xhigh Round 18 (worker task `01a06cd4-58a4-75d1-975e-670f7ad79085`) returned
`BLOCKED` on one remaining in-scope historical-compatibility defect. A valid pre-Phase-3 row whose
legacy `backend_id` happened to equal `openai_compatible_http` could be migrated and read using its
provider-null historical semantics, but application restart reconciliation still failed. The
reconciliation path reached the existing finalization boundary, while the current-schema update
trigger still classified the colliding backend marker as Phase 3 and the connection-local snapshot
validator was only registered at its eight-argument historical shape. The row consequently remained
`RUNNING`/`STREAMING` instead of being durably reconciled to an honest aborted state.

This was an implementation defect, not a contradiction in the approved Phase 3 design. The bounded
repair keeps the existing SQLite trigger and connection-local validator seam:

- the snapshot validator now has a nine-argument form that accepts an explicit persisted-record
  classification, while the eight-argument form remains available for earlier triggers;
- the 0005 insert trigger continues to classify a backend-marker/provider-null new write as strict
  Phase 3 and rejects missing provenance, while the 0005 update trigger classifies a persisted
  historical row from provider identity rather than the colliding backend string;
- update-time snapshot, outcome, and remote-uncertainty validation use that same persisted-row
  classification, so restart reconciliation preserves historical compatibility without weakening
  new Phase 3 insertion rules;
- the focused restart regression constructs a pre-Phase-3 backend-marker collision, upgrades and
  reopens it, runs application reconciliation, and verifies durable `ABORTED` state, preserved
  partial output, and `remote_outcome_unknown=true`.

No provider ID, backend ID, cancellation truth, retry policy, context scope, streaming architecture,
legacy `Provider.complete()` behavior, or later-phase functionality changed. The retained Phase 2
recovery-artifact interaction remains outside this repair authority and was not modified.

Post-Round-18-repair validation passed as follows:

```text
full deterministic suite: 242 passed, 1 skipped (243 collected)
Phase 3 focused suite: 78 passed, 1 skipped (79 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The local-Qwen acceptance test remained skipped because no endpoint/model variables were supplied.
`OPENROUTER_API_KEY`, `BOTS5_PHASE3_QWEN_BASE_URL`, `BOTS5_PHASE3_QWEN_MODEL`, and
`BOTS5_PHASE3_QWEN_API_KEY_ENV` were unset for the deterministic pass. Ordinary tests retained live
socket blocking, manifest validation was offline, and no provider/API/network request was made.
The full Phase 1/2 and legacy partitions were run independently in addition to the aggregate suite.
A fresh independent Round 19 Sol/xhigh closure audit was required; its result and bounded repair are
recorded below. Final human closure remains pending.

## Bounded repair after Round 19

Fresh independent Sol/xhigh Round 19 (worker task `01a06cf2-ab1a-7a72-9344-7ef32ab9d6a7`) returned
`BLOCKED` on one in-scope current-schema provenance defect. A valid Phase 3 attempt could be created,
then have only its persisted `provider_id` erased through out-of-band database mutation. Startup
validation classified the row as historical because its backend marker was also the permitted legacy
collision, accepted the row, and exposed it without rejecting the snapshot's remaining
`provider_id="local_openai"` identity. This could launder a real Phase 3 record into historical
semantics and contradicted authoritative current-schema duplicated-provenance validation.

The bounded repair retains the Round 18 historical compatibility exception while making classification
evidence-based at every existing persistence boundary:

- strict Phase 3 classification now includes an exact Phase 3 provider or backend identity already
  present in the immutable snapshot, in addition to a persisted provider identity;
- 0005 migration/startup validation parses the snapshot only to identify that exact Phase 3 identity,
  so the approved provider-null historical backend collision with legacy `{}` snapshot remains
  readable and restart-reconcilable;
- current persisted-row reads and public finalization/update validation use the same snapshot-aware
  classification, rejecting provider erasure or other Phase 3 identity loss before authoritative use;
- the 0005 update trigger applies the snapshot-aware marker with SQL `COALESCE`, preventing a missing
  legacy JSON field from becoming an unspecified validator mode, while strict new backend-marker
  inserts remain unchanged;
- the focused regression directly tampers a valid current-schema Phase 3 row to erase only
  `provider_id` and verifies `SQLiteAppStateStore.open()` rejects it.

The initial Round 19 audit prompt carried a stale report-file hash after the report was resealed. Sol
classified that as non-blocking, verified the executable tracked-diff seal, and found no source,
index, ref, or candidate mutation. This documentation record uses the current re-sealed candidate
values below. No provider ID, backend ID, cancellation truth, retry policy, context scope, streaming
architecture, legacy `Provider.complete()` behavior, or later-phase functionality changed. The
retained Phase 2 recovery-artifact interaction remains outside this repair authority and was not
modified.

Post-Round-19-repair validation passed as follows:

```text
full deterministic suite: 243 passed, 1 skipped (244 collected)
Phase 3 focused suite: 79 passed, 1 skipped (80 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The local-Qwen acceptance test remained skipped because no endpoint/model variables were supplied.
`OPENROUTER_API_KEY`, `BOTS5_PHASE3_QWEN_BASE_URL`, `BOTS5_PHASE3_QWEN_MODEL`, and
`BOTS5_PHASE3_QWEN_API_KEY_ENV` were unset for the deterministic pass. Ordinary tests retained live
socket blocking, manifest validation was offline, and no provider/API/network request was made.
A fresh independent Round 20 Sol/xhigh closure audit was required; its result and bounded repair are
recorded below. Final human closure remains pending.

## Bounded repair after Round 20

Fresh independent Sol/xhigh Round 20 (worker task `01a06d09-8b49-7a71-95fc-4f90964fd3dc`) returned
`BLOCKED` on one in-scope current-schema type-validation defect. A database stamped
`0005_generation_outcomes` with all ten additive outcome columns present and nullable, but with
`prompt_tokens` declared as `TEXT`, was accepted by the normal upgrade/open path. A later valid
telemetry update then failed at the SQLite lifecycle trigger because text affinity changed the value
before integer validation. Startup therefore failed to reject a malformed current schema before the
store became authoritative.

The bounded repair extends the existing `PRAGMA table_info(generation_attempts)` check in
`SQLiteAppStateStore.open()` with the exact declared SQLite types from the accepted 0005 migration:
`VARCHAR(64)`, `TEXT`, `VARCHAR(128)`, `INTEGER`, and `BOOLEAN` as applicable. It adds a focused
regression with a nullable `TEXT` `prompt_tokens` column and verifies that current-schema startup
rejects it. Missing columns and non-nullable columns remain separately covered. No migration model,
provider identity, backend identity, cancellation, retry, context, streaming architecture, legacy
seam, or later-phase functionality changed. The retained Phase 2 recovery-artifact interaction
remains outside this repair authority and was not modified.

Round 20 also recorded two non-blocking documentation observations: stale wording in an architecture
description of `Provider.complete()` and streaming, and a stale report-file hash in the audit prompt
that was corrected by this candidate reseal. Neither affected executable behavior or repository
integrity.

Post-Round-20-repair validation passed as follows:

```text
full deterministic suite: 244 passed, 1 skipped (245 collected)
Phase 3 focused suite: 80 passed, 1 skipped (81 collected)
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The local-Qwen acceptance test remained skipped because no endpoint/model variables were supplied.
`OPENROUTER_API_KEY`, `BOTS5_PHASE3_QWEN_BASE_URL`, `BOTS5_PHASE3_QWEN_MODEL`, and
`BOTS5_PHASE3_QWEN_API_KEY_ENV` were unset for the deterministic pass. Ordinary tests retained live
socket blocking, manifest validation was offline, and no provider/API/network request was made.
Fresh independent Round 21 Sol/xhigh closure audit returned `PASS`; its final evidence and the
post-audit deterministic rerun are recorded below. Final human closure remains pending.

## Round 21 independent closure audit and final deterministic evidence

Fresh independent GPT-5.6 Sol/xhigh Round 21 (worker task
`01a06d19-7269-7693-8c89-b0e5202ede4a`) returned `PASS` after a read-only audit of the exact
candidate. It found no concrete Phase 3 closure blocker. Direct probes independently confirmed
the ten additive outcome columns' declared types and nullability, structural endpoint rejection,
HTTP stream closure on cancellation, durable `ABORTED` with `remote_outcome_unknown=true`, and
partial-output preservation. The worker also confirmed the accepted historical compatibility
exception and repository integrity without making provider, API, network, OpenRouter, or live-model
calls.

Round 21 recorded two non-blocking findings: the audit prompt carried a stale implementation-report
hash, which was corrected by this report update and final reseal; and
`docs/ARCHITECTURE.md:59-62` retains older wording that presents `Provider.complete()` as the only
service seam and the local provider as non-streaming. That wording does not describe the current
Phase 3 runtime and was not changed because it is outside the bounded persistence repair scope.
The retained Phase 2 recovery-artifact interaction was independently reproduced and remains a
separate human-adjudication item; the database safely remained at `0004_integrity_guard_function`
after the rejected upgrade.

The final zero-spend deterministic rerun after the Round 21 PASS and this report update produced:

```text
full deterministic suite: 244 passed, 1 skipped (245 collected)
Phase 3 focused suite: 80 passed, 1 skipped (81 collected)
high-risk Phase 3 lifecycle/persistence regressions: 12 passed
Phase 1/2 regression suite: 51 passed
legacy preservation suite: 113 passed
compileall: exit 0
three offline legacy manifests: exit 0, each returned OK
git diff --check: exit 0
```

The deterministic commands unset `OPENROUTER_API_KEY` and all local-Qwen variables. Ordinary tests
retained live-socket blocking; the marked local-Qwen test skipped; manifest validation was offline;
and no provider/API/network request occurred. The final report hash was computed after this update.

## Human closure decision

Human closure was approved on 2026-09-05. Linux v0.1 Phase 3 is accepted and closed on the evidence
recorded in this report: manual local-Qwen acceptance, deterministic validation, legacy-preservation
validation, and fresh independent Sol/xhigh Round 21 `PASS` with no concrete Phase 3 closure blockers.

The retained Phase 2 recovery-artifact interaction remains a separate known human-adjudication
boundary. This closure does not reclassify or repair that interaction. Phase 4 and later scope remain
unimplemented and separately gated.

A final documentation-only closure-pass rerun reproduced the same zero-spend results: full
`244 passed, 1 skipped`; Phase 3 `80 passed, 1 skipped`; Phase 1/2 `51 passed`; legacy preservation
`113 passed`; all three offline manifests `OK`; and compileall/diff-check exit `0`.

## Independent adversarial audit record

Twenty-one fresh GPT-5.6 Sol workers were invoked at xhigh reasoning for Rounds 1-21 against the current
candidate. Each worker was read-only and used the exact checkout, baseline, candidate diff, untracked
files, source, tests, and repository state as evidence. No worker made a provider/model/API/network
call or changed candidate source, the index, a commit, or a ref. Round 11 refreshed one ignored
Python bytecode cache during an offline probe; that audit-time filesystem mutation is recorded above.

| Round | Sol disposition | Blockers found | Repairs and validation | Resulting candidate state |
|---|---|---|---|---|
| 1 | BLOCKED | Non-stop finishes were marked remotely unknown; pre-dispatch aborts were marked unknown; invalid costs could be stored and then unreadable; OpenRouter accepted secret-bearing base URLs; the Qwen acceptance test was socket-blocked and accepted failed/incomplete outcomes. | Corrected certainty, added application/SQLite cost validation, hardened provider/backend URL validation, isolated the marked Qwen test, and required complete/stop. Focused result: 82 passed, 1 skipped. | Unstaged/uncommitted; baseline HEAD unchanged. |
| 2 | BLOCKED | Empty `?`/`#` URL delimiters remained accepted; metadata could arrive after its related delta and be lost on interruption; restart/shutdown overwrote durable pre-dispatch false; cleanly consumed cancellation became incomplete. | Moved metadata persistence ahead of deltas, preserved durable dispatch truth through reconciliation, mapped clean cancellation to aborted, rejected empty delimiters, added regression tests, and updated semantics docs. Full result: 176 passed, 1 skipped; focused repair selection: 7 passed, 1 skipped. | Unstaged/uncommitted; baseline HEAD unchanged. |
| 3 | PASS | None. | Sol independently reproduced 180 passed, 1 skipped; 16 Phase 3, 51 Phase 1/2, and 113 legacy tests; compileall and diff check passed; independent probes covered cancellation, stream close, certainty, reconciliation, raw-DML, migration retry, and fake-default behavior. | Deterministic and independent-review evidence complete before the separately supplied live-Qwen operator evidence; remains unstaged/uncommitted with final human closure pending. |
| 4 | BLOCKED | The authoritative store accepted an empty request snapshot for a new `local_openai` attempt; the authoritative store accepted `COMPLETE` with `finish_reason=length`; current-schema startup accepted a provider ID inconsistent with the immutable snapshot. The audit also found the report's tracked diff seal stale at audit time. | The three runtime defects were repaired within the accepted persistence/domain boundaries: shared mandatory snapshot validation, exact-stop outcome validation, current-schema Phase 3 row validation, and strengthened current-schema triggers. The documentation seal was refreshed. Final deterministic validation and a new independent audit are pending. | Candidate remains unstaged/uncommitted; final human closure remains pending. |
| 5 | BLOCKED | The shared validator allowed an unknown credential-shaped snapshot key such as `access_token`; direct trigger-authorized writes could commit a malformed Phase 3 base URL, forbidden `api_key`, or empty `request_id` before later startup/read rejection. | Closed the Phase 3 snapshot schema, exposed the shared snapshot/outcome validators through the existing SQLite connection-local guard, and made the 0005 insert/update triggers enforce them. Post-repair validation: full 200 passed/1 skipped; Phase 3 36 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. A fresh Round 6 audit is pending. | Candidate remains unstaged/uncommitted; final human closure remains pending. |
| 6 | BLOCKED | Three in-scope defects were reproduced: whitespace-bearing base URLs crossed the trigger/startup boundary; an empty streamed finish reason stranded running work; and contradictory Phase 3 outcome combinations were accepted. A separate sequential 0004→0005 upgrade reproduced rejection of an older retained recovery point. | Rejected surrounding URL whitespace; rejected empty provider stream finish reasons; converted malformed completion events to durable failed outcomes; and strengthened running/complete/incomplete/failed/aborted outcome consistency. Full result: 208 passed/1 skipped; Phase 3 44 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. The recovery-artifact interaction was not changed and requires human adjudication. | Three Round 6 repairs are present; candidate remains unstaged/uncommitted and not closed. |
| 7 | BLOCKED | Duplicate JSON keys could hide a credential-bearing snapshot value after parsing; out-of-range usage telemetry could overflow SQLite and strand a generation; the report's tracked-diff seal was stale. The retained recovery-pair upgrade interaction was reconfirmed as outside the three-repair authority. | Reject duplicate snapshot keys recursively; bound provider usage counters to SQLite's signed 64-bit range so invalid telemetry remains null; refresh the report seal. Post-repair result: full 211 passed/1 skipped; Phase 3 47 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Candidate remains unstaged/uncommitted; recovery interaction remains for human adjudication; fresh Round 8 audit required. |
| 8 | BLOCKED | The authoritative outcome validator accepted `INCOMPLETE` with a known `finish_reason=length` plus `remote_outcome_unknown=true`, and accepted failure metadata on `RUNNING` or `COMPLETE` attempts. A non-canonical base URL could be persisted while the HTTP client dispatched a different effective URL. An empty streamed `stop` response could become durable `COMPLETE` with empty output. The retained recovery-pair upgrade interaction was independently reconfirmed outside the repair authority. | Extended the existing outcome validator/trigger/startup validation; added the shared canonical URL helper and normalized provider/backend dispatch inputs; converted empty streamed `stop` to durable `FAILED`; added focused adversarial tests. Post-repair result: full 218 passed/1 skipped; Phase 3 54 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Candidate remains unstaged/uncommitted; recovery interaction remains for human adjudication; fresh Round 9 audit required. |
| 9 | BLOCKED | The shared URL helper accepted malformed host spelling such as `http://exa mple.invalid/v1`, which the HTTP client dispatched with an encoded space, and accepted Unicode host spelling while HTTPX dispatched its IDNA form. A rejected `finish_reason=length` plus `remote_outcome_unknown=true` terminal event could then be followed by a failed fallback retaining the invalid finish reason, leaving durable `RUNNING`/`STREAMING` work with partial output. The retained recovery-pair interaction was also reconfirmed outside the repair authority. | Strictly validate/canonicalize hostname labels, IDNA, IPv4, and IPv6 through the existing URL helper used by persistence and providers; clear terminal finish metadata when the application converts a rejected terminal event into durable `FAILED`; add store/current-schema/provider and fallback regression tests. Post-repair result: full 220 passed/1 skipped; Phase 3 56 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Candidate remains unstaged/uncommitted; recovery interaction remains for human adjudication; fresh Round 10 audit required. |
| 10 | BLOCKED | Durable dispatch uncertainty could be erased on a running or aborted Phase 3 attempt; malformed percent escapes such as `%ZZ` were accepted by the canonical URL and persistence boundaries; the report recorded stale 220/56 test counts instead of the exact 222/58 counts present at audit time. The retained recovery-pair interaction remained outside the repair authority. | Added a shared store/SQLite transition guard that preserves dispatch uncertainty for running/aborted outcomes while allowing known terminal outcomes to resolve it; rejected malformed percent escapes in the shared URL helper; refreshed the report counts and repair history. Post-repair result: full 225 passed/1 skipped; Phase 3 61 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Candidate remains unstaged/uncommitted; recovery interaction remains for human adjudication; fresh Round 11 audit required. |
| 11 | BLOCKED | Invalid terminal telemetry could make the failure fallback fail and strand `RUNNING`/`STREAMING` work; `INCOMPLETE` without a finish could clear dispatch uncertainty; the shared payload builder changed baseline `Provider.complete()` behavior for an empty system message. The retained recovery-pair interaction remained outside the repair authority. | Cleared optional terminal metadata in the durable failure fallback; made uncertainty resolution finish-aware across public store, SQLite guard, and the `0005` update trigger; restored baseline non-streaming payloads while retaining exact-user streaming; added focused regressions. Post-repair result: full 229 passed/1 skipped; Phase 3 65 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 12 audit pending. |
| 12 | BLOCKED | The default fake snapshot gained nullable Phase 3 fields; a legacy-only `base_url` snapshot was misclassified and rejected during migration; live attempt reads returned a snapshot prompt contradicting the referenced user message; and a `0005` stamp without outcome columns opened successfully. The retained recovery-pair interaction remained outside the repair authority. | Restored the fake six-field snapshot shape; classified Phase 3 only from real provider/backend identity; added prompt validation at the attempt row-read boundary; and required all additive outcome columns for current-schema startup. Added focused compatibility, migration, live-read, and malformed-schema tests. Post-repair result: full 233 passed/1 skipped; Phase 3 69 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 13 audit pending. |
| 13 | BLOCKED | Historical migration still rejected a baseline-readable trailing-slash `base_url` and applied Phase 3 duplicate/secret strictness to historical snapshot fields. New Phase 3 running/aborted attempts could retain `remote_outcome_unknown=NULL` across public-store, trigger, read, and reopen boundaries. The retained recovery-pair interaction remained outside the repair authority. | Restored historical permissive parsing/URL behavior while retaining strict Phase 3 duplicate/secret/canonical validation; required explicit Phase 3 remote-outcome truth and enforced it in the public store and 0005 insert/update triggers; added migration/readability and null-uncertainty regressions. Post-repair result: full 234 passed/1 skipped; Phase 3 70 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 14 audit pending. |
| 14 | BLOCKED | Pre-Phase-3 snapshots with snapshot-only `provider_id="legacy-provider"` or opaque `base_url="legacy opaque value"` remained baseline-readable but failed 0005 migration because the snapshot provider field triggered Phase 3 classification and the historical URL still received new validation. The retained recovery-pair interaction remained outside the repair authority. | Classified Phase 3 only from persisted provider/backend identity and removed new URL validation from the historical path; added parameterized migration/readability coverage for snapshot-only provider, opaque URL, duplicate, credential-shaped, and trailing-slash legacy fields. Post-repair result: full 236 passed/1 skipped; Phase 3 72 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 15 audit pending. |
| 15 | BLOCKED | A stamped current schema with all ten outcome columns but non-nullable `provider_id` was accepted at startup and then broke the default fake path. A contradictory streamed `length` followed by `stop` was overwritten into durable `COMPLETE/stop`, surviving restart. The retained recovery-pair interaction remained outside the repair authority. | Startup now enforces nullability for every additive outcome column; the B.O.T.S.-owned streaming adapter now fails closed on conflicting non-null finish reasons, allowing the existing durable `FAILED` fallback to preserve partial output and uncertainty. Added startup-schema and conflicting-finish regressions. Post-repair result: full 238 passed/1 skipped; Phase 3 74 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 16 audit pending. |
| 16 | BLOCKED | A valid historical row with legacy `backend_id='openai_compatible_http'` was rejected by 0005 because the backend marker alone imposed Phase 3 provenance; explicit cancellation could durably persist `ABORTED` and still block on terminal-event delivery into a full bounded queue; the report’s final tracked-diff seal was stale. The retained recovery-pair interaction remained outside the repair authority. | Migration/current-schema/persisted-row classification now uses provider identity for historical compatibility while new writes retain strict Phase 3 backend-marker checks; cancellation waits for terminal durability and returns without bypassing ordered/backpressured terminal delivery; the tracked-diff seal was refreshed. Added collision and full-queue cancellation regressions. Post-repair result: full 240 passed/1 skipped; Phase 3 76 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 17 audit pending. |
| 17 | BLOCKED | If completion was already durable while `generation_completed` was backpressured, a late `cancel_generation()` could cancel the task and drop the terminal event, leaving durable `COMPLETE/stop` without corresponding progress. The retained recovery-pair interaction remained outside the repair authority. | Cancellation now avoids cancelling a task whose terminal state is already durable, preserving the existing ordered/backpressured publication; added a late-cancellation/full-queue regression. Post-repair result: full 241 passed/1 skipped; Phase 3 77 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; fresh Round 18 audit pending. |
| 18 | BLOCKED | A provider-null historical row whose legacy `backend_id` collided with `openai_compatible_http` migrated and read but failed application restart reconciliation because the update trigger and eight-argument snapshot validator still classified the update as Phase 3. The retained recovery-pair interaction remained outside the repair authority. | Added explicit persisted-row classification to the existing snapshot validator seam, retained strict backend-marker classification for new inserts, and made 0005 update snapshot/outcome/uncertainty checks use provider identity. Added upgrade/reopen/reconciliation coverage. Post-repair result: full 242 passed/1 skipped; Phase 3 78 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; recovery interaction remains for human adjudication; fresh Round 19 audit pending. |
| 19 | BLOCKED | Erasing only the persisted `provider_id` from a valid Phase 3 row allowed current-schema startup to classify the row as historical when its backend marker collided with the legacy compatibility exception, despite the immutable snapshot retaining exact Phase 3 provider identity. The report-file hash supplied at audit dispatch was stale, but this was non-blocking; the tracked diff and repository state were intact. | Made persisted-row classification snapshot-aware for exact Phase 3 provider/backend identities; retained the provider-null legacy collision exception for genuinely historical snapshot shapes; added SQL `COALESCE` for deterministic update-trigger classification and an authoritative startup-erasure regression. Post-repair result: full 243 passed/1 skipped; Phase 3 79 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; recovery interaction remains for human adjudication; fresh Round 20 audit pending. |
| 20 | BLOCKED | A stamped current schema with all ten outcome columns present and nullable but `prompt_tokens` declared as `TEXT` opened successfully, then failed a valid telemetry update at the SQLite trigger after text affinity changed the value. Round 20 also noted stale architecture wording as non-blocking. | Current-schema startup now checks exact declared types for all ten additive outcome columns using the existing PRAGMA validation; added malformed-`prompt_tokens` startup rejection coverage. Post-repair result: full 244 passed/1 skipped; Phase 3 80 passed/1 skipped; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Unstaged/uncommitted; baseline HEAD unchanged; recovery interaction remains for human adjudication; fresh Round 21 audit required. |
| 21 | PASS | None. Sol independently verified the authoritative snapshot/outcome boundaries, exact schema type/nullability checks, historical compatibility, cancellation/stream closure, partial-output durability, legacy behavior, and repository integrity. It recorded only stale report-input hash and stale architecture wording as non-blocking findings; the retained recovery artifact remains a separate human-adjudication item. | Final deterministic rerun: full 244 passed/1 skipped; Phase 3 80 passed/1 skipped; high-risk lifecycle/persistence regressions 12 passed; Phase 1/2 51 passed; legacy 113 passed; compileall/manifests/diff check all exit 0. | Candidate remains unstaged/uncommitted; baseline HEAD unchanged; manual local-Qwen acceptance evidence is recorded; human closure was subsequently approved. |

Round 3 recorded three non-blocking findings and they were intentionally not expanded into repairs:
the persistence validator accepts a fabricated empty `?`/`#` snapshot even though approved provider and
backend construction reject it; one architecture sentence still describes `Provider.complete()` as the
only seam while preserving it alongside streaming; and the previously recorded test count was stale.
The report count is corrected here. No throughput/backpressure evidence exists, so no architecture
change was made.

Round 4 was a fresh GPT-5.6 Sol/xhigh read-only closure audit after the manual-Qwen documentation
pass (worker task `01a06b7e-78a5-74e3-9f2d-1b02954b04a1`). It returned `BLOCKED`. The first three
findings were implementation contradictions against the locked Phase 3 authority and were repaired
within scope; the fourth was documentation-only and is corrected in this report. Fresh Round 5
(worker task `01a06ba7-c38b-7b10-96dd-a23a577fcd07`) independently confirmed those three repairs but
found two further trigger-boundary defects; those were repaired within scope and the deterministic
matrix rerun passed. Fresh Round 6 (worker task `01a06bb9-f769-7d72-8431-dd1f4799001d`) returned
`BLOCKED`: its three persistence-authority findings were repaired within scope, while its separate
recovery-artifact finding was intentionally not changed and is recorded for human adjudication. A
fresh Round 7 (worker task `01a06bcf-ca51-74a2-a549-9c0f52560d6a`) returned `BLOCKED`: its duplicate
snapshot and numeric-telemetry findings were repaired within scope, while the same recovery-artifact
finding remains for human adjudication and its stale report-seal finding was corrected. A fresh
Round 8 returned `BLOCKED` on the three defects recorded above; those defects were repaired within
scope and the deterministic matrix was rerun. Fresh Round 9 (worker task
`01a06c02-45f2-75a3-b9ac-6f036c11d94c`) returned `BLOCKED` on the malformed/non-canonical hostname
boundary and the rejected-terminal-metadata fallback; both were repaired within scope and the matrix
was rerun. Fresh Round 10 (worker task `01a06c19-ea1f-7640-9f9b-9202617f9f1a`) returned `BLOCKED`
on remote-uncertainty erasure, malformed percent escapes, and stale counts; the first two were
repaired within scope and the documentation was refreshed. Fresh Round 13 (worker task
`01a06c5e-373f-77d0-87fd-400340867125`) returned `BLOCKED` on historical-snapshot overvalidation
and nullable Phase 3 dispatch truth; both were repaired within scope and the deterministic matrix
was rerun. Fresh Round 14 (worker task `01a06c76-6ce0-74b0-82b9-fccbb48543cf`) returned `BLOCKED`
on snapshot-only legacy provider and opaque URL overvalidation; the bounded classification/validation
repair was applied and the deterministic matrix was rerun. Fresh Round 15 (worker task
`01a06c88-bbb1-7262-a354-a99dc009d686`)
returned `BLOCKED` on stamped-schema nullability and conflicting streamed finish signals; both were
repaired within scope and the deterministic matrix was rerun. Fresh Round 16 (worker task
`01a06ca1-8ae3-7910-bbde-342d8635a675`) returned `BLOCKED` on the historical backend-identity
collision, full-queue cancellation liveness, and a stale tracked-diff seal; the first two were
repaired within scope, the seal was refreshed, and the deterministic matrix was rerun. A fresh
Round 17 (worker task `01a06cc0-5b93-7ac1-abb5-a1bd299a9cbb`) returned `BLOCKED` on the late
completion-cancellation race; it was repaired within scope and the deterministic matrix was rerun.
Fresh Round 18 (worker task `01a06cd4-58a4-75d1-975e-670f7ad79085`) returned `BLOCKED` on the
historical backend-identity collision during restart reconciliation; the bounded trigger/UDF
classification repair was applied and the deterministic matrix was rerun. A fresh Round 19 audit is
pending. That fresh Round 19 audit (worker task `01a06cf2-ab1a-7a72-9344-7ef32ab9d6a7`) returned
`BLOCKED` on provider-identity erasure at current-schema startup; the snapshot-aware classification
repair was applied and the deterministic matrix was rerun. A fresh Round 20 audit is pending. No
round establishes human closure. That fresh Round 20 audit (worker task
`01a06d09-8b49-7a71-95fc-4f90964fd3dc`) returned `BLOCKED` on malformed current-schema outcome
types; exact PRAGMA type validation was applied and the deterministic matrix was rerun. A fresh
Round 21 (worker task `01a06d19-7269-7693-8c89-b0e5202ede4a`) returned `PASS` after a fresh
read-only audit. It found no concrete Phase 3 closure blocker; its non-blocking findings and the
retained recovery-artifact boundary are recorded above. The final deterministic matrix was rerun
after the PASS and this report update. Human closure was subsequently approved on 2026-09-05.

## Live local-Qwen readiness/status

The opt-in path is structurally ready, and the supplied manual desktop acceptance has now passed for
the local `local_openai` provider over the `openai_compatible_http` backend. It demonstrated a real
Qwen completion, explicit mid-stream cancellation to `ABORTED`, partial-output preservation, and
restart persistence. The marked automated acceptance test remains separately opt-in; this report
does not claim that pytest test ran as part of the manual operator exercise.

To run the marked acceptance later, set only the explicitly authorized local endpoint and model,
optionally set an environment-variable name for local auth, and run
`tests/test_phase3_local_qwen.py`. The marked test uses `local_openai`, does not construct OpenRouter,
and requires a completed `finish_reason=stop` result with non-empty output.

Example:

```bash
env -u OPENROUTER_API_KEY \
  BOTS5_PHASE3_QWEN_BASE_URL=http://127.0.0.1:8000/v1 \
  BOTS5_PHASE3_QWEN_MODEL=Qwen3-8B \
  PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_phase3_local_qwen.py
```

No throughput or backpressure evidence exists yet. The implementation deliberately retains the
existing per-delta durable-write and ordered/backpressured event strategy; measured Qwen throttling,
if any, must be separately adjudicated.

## Known limitations and non-goals

- No provider/model settings UI or discovery.
- No multi-turn context: the real request contains only the exact current user message.
- No tools, attachments, search, RAG, summarisation, campaigns UI, Android, daemon/remote client,
  Git integration, or Phase 4 concurrency/workspace work.
- No automatic retries, including for aborted or remotely uncertain work.
- Provider usage/cost is null when absent; Phase 3 does not ask for an alternate request shape.
- The local HTTP adapter uses the existing OpenAI-compatible SSE convention and does not claim support
  for provider-specific non-SSE or bespoke streaming protocols.
- The supplied manual evidence does not include an exact endpoint/model identifier, cancellation
  timing, usage/cost payload, or a streaming throughput measurement.
- A sequential upgrade from a database carrying the older fixed-name Phase 2 pre-migration recovery
  point to the Phase 3 0005 head was reproduced as rejecting that retained recovery artifact. This
  candidate does not alter the closed Phase 1/2 recovery semantics; the interaction requires separate
  human adjudication before it can be treated as a closure result.
- The provider and authoritative store now cap usage counters at SQLite's signed 64-bit maximum;
  provider-supplied counters outside that representable range are treated as unknown telemetry.

## Deviations from approved proposal

No material deviation was made from the locked decisions. The implementation adds small supporting
surfaces required to make those decisions observable and testable: a typed dispatch marker so durable
remote uncertainty is recorded before progress, a desktop Stop action wired to the explicit core
cancellation command, and a marked opt-in test exception for the separately authorized local endpoint.
It does not add batching, event coalescing, a second notification architecture, or any later-phase
capability.

## Exact diff for independent adversarial review

The authoritative candidate diff is the unstaged working-tree diff against the approved baseline:

```bash
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-ext-diff --binary \
  1660ac57af5c845fb6f11b9eb99a4fae797a1706
```

The seven untracked candidate files, which are not included by `git diff`, are the following exact
additional diff inputs:

```bash
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  src/bots5/core/urls.py || true
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  docs/LINUX_V0_1_PHASE3_IMPLEMENTATION_REPORT.md || true
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  src/bots5/infrastructure/generation/openai_compatible.py || true
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  src/bots5/infrastructure/persistence/migrations/versions/0005_generation_outcomes.py || true
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  src/bots5/infrastructure/persistence/phase3_validation.py || true
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  tests/test_phase3_generation.py || true
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 diff --no-index --binary /dev/null \
  tests/test_phase3_local_qwen.py || true
```

The candidate path inventory is available with:

```bash
git -C /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1 status --short
```

The exact current inventory is intentionally left unstaged so an independent adversarial reviewer can
inspect the complete tracked and untracked diff, including the new migration, backend, and tests,
before any commit gate.

Post-Round-21 final candidate seal: 25 modified tracked files, seven untracked candidate files,
zero staged files, `HEAD` and `origin/main` both remain
`1660ac57af5c845fb6f11b9eb99a4fae797a1706`. The current tracked binary-diff SHA-256 is
`df1e2bed3cb2a795004380c3a17555063d46cd0057953bc6f7d770084f194769`.
