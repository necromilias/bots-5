# Linux v0.1 Phase 5 implementation report

## Candidate status

This is the bounded Phase 5 candidate for the human-adjudicated provider/model usability slice. The
candidate was implemented from `main` at `7c609f227bf34887208272b42af77d2c8a15789f`; the locally
recorded `origin/main` is the same SHA. The candidate is unstaged and uncommitted. No fetch, ref
mutation, push, merge, rebase, OrgMem write, or live public-provider call was performed. The
bounded live local-Ollama acceptance is recorded below.

The deterministic candidate seal, computed as the SHA-256 of the per-file `sha256sum` lines for all
modified and untracked non-ignored candidate paths excluding this report itself, in the exact path
order listed in the File inventory below, is updated after the final repair matrix and recorded here
before the independent closure audit:

`1c0e61ac4e7a9061a5223f822765a70d6933b716812d96600ba00f70660c4a37`

## Architecture

Phase 5 adds a closed built-in provider router. The durable identity layers remain separate:

* backend type: `fake` or `openai_compatible_http`;
* provider profile/adapter behavior: `generic` or `openrouter`;
* revisioned configured connection with durable UUIDv7 identity;
* durable model catalogue entry belonging to exactly one connection and preserving the exact
  case-sensitive provider model ID.

The fake connection/model are the only seeded runtime state. New chats inherit the application
default; pre-Phase-5 chats without a durable selection receive no inferred model and therefore remain
selection-required. Historical attempts retain their original provider attribution and snapshots.
`provider_id` remains the Phase 3 compatibility field and is not used as the Phase 5 connection ID.
The legacy campaign engine and `Provider.complete()` path remain intact.
The core depends on the `SecretStore` port and sanitized `SecretStoreError` contract; concrete
environment and Linux Secret Service adapters are composed by bootstrap and are not imported or
constructed by core configuration authority.

## Migration and recovery

Migration `0007_phase5_provider_model_configuration` upgrades every supported Phase 1–4 revision and
creates provider connections, model catalogue, capability facts/overrides/observations, application,
model, and chat-model generation settings, durable chat selections, and nullable indexed attempt
connection/model attribution. It seeds only the built-in fake connection/model, its application
default, and the minimum trusted fake capability facts.

Migration `0008_catalogue_refresh_outcomes` adds one durable current refresh-state row per provider
connection. It records `never`, `succeeded`, or `failed` independently of the existing
`catalogue_revision`, which continues to advance for each explicit refresh attempt and for accepted
catalogue-identity invalidation. Failed refreshes store only a closed B.O.T.S.-owned failure class and
message (`transport`, `timeout`, `provider_http`, `protocol`, or `unknown`); successful empty
catalogues clear the prior failure state. Existing `0007` candidate databases upgrade additively,
and missing refresh rows are rejected at current-schema startup. The connection list projects this
state after re-query/restart, so a failed refresh cannot appear as a successful empty catalogue.

The migration is retry-safe and rejects unsupported newer revisions. Phase 3 historical snapshots are
not rewritten. The existing fixed recovery path remains the first candidate when it is absent or
already verifies against the source. If it belongs to a different valid source state, a new
digest-and-UUIDv7-suffixed recovery path is created and verified; existing recovery files are never
overwritten or deleted. A failed metadata promotion leaves the newly promoted SQLite artifact and
prior evidence intact, and the next invocation can reconstruct its missing metadata without
replacing the artifact.

The Phase 5 SQLite boundary guards attempt connection/model attribution, referenced catalogue
identity, provider-connection identity, direct writes of provider/settings values, secret-shaped
keys in catalogue metadata and capability provenance using SQLite JSON-tree triggers, bounded
capability provenance fields, and one identity per model/key/source/source-revision evidence row.
They also reject Phase 5 attribution on legacy attempts, reject disabled or marker-preserving inert
guard triggers at startup, validate Phase 5 primary keys, foreign keys, and declared CHECK
constraints, exercise the raw-DML guards in savepoints, and retain lower-precedence evidence without
allowing contradictory duplicate identities or malformed capability truth. Capability-fact timestamps
and capability-override reason lengths are guarded on insert and every relevant update column, and
startup revalidates both boundaries. New Phase 5 snapshots are checked against the
connection and catalogue truth at dispatch; historical snapshots retain their frozen revisions and
remain readable after later connection edits.

## Secrets and dependencies

`keyring>=25,<26` and `secretstorage>=3,<4` were added to project dependencies and installed into the
project development environment (`keyring==25.7.0`, `secretstorage==3.5.0`). `SecretServiceStore`
explicitly instantiates `keyring.backends.SecretService.Keyring`; it does not use keyring's global
backend selection or fallback chain. `EnvironmentSecretStore` is a separate explicit read-only
environment source. Missing, locked, unavailable, and operation-error states are sanitized and
visible.

Credential values never enter SQLite, request snapshots, attempt attribution, logs, diagnostics,
exceptions, repr output, or exports. Provider transport failures, including unexpected non-HTTP
exceptions, are sanitized against the configured credential before they can become a durable error
or event; discovery failure diagnostics receive the same boundary. Nested secret-shaped fields are
rejected in stored bounded metadata and capability provenance as well as in snapshots. The Settings surface accepts a
credential only for an explicit Secret Service save action, clears the transient UI field before
dispatch, and displays status and reference only. Deterministic tests use fake Secret Service
implementations and never inspect real stored credentials.

## Catalogue, discovery, and capabilities

Manual catalogue entries preserve exact provider IDs and survive discovery failure. A manual entry
confirmed by discovery retains one durable ID and changes provenance to `manual_confirmed`. Successful
refreshes make returned entries available and mark omitted discovered entries unavailable; failed
refreshes preserve cached entries and mark discovered entries stale. Endpoint/catalogue revisions
make old provider-metadata and endpoint capability facts non-effective while retaining their evidence;
lower-priority evidence remains available for deterministic fallback. Disabled or retired connections
are represented as disconnected/unavailable. The same model string on
different connections remains distinct. Refreshes are operator-triggered only and use both catalogue
and connection revision CAS, so stale async refreshes cannot overwrite newer state. Endpoint/profile/
backend and credential-source/reference changes advance the catalogue revision and stale discovered
entries. No startup, periodic, or silent provider discovery exists.

The bounded typed capability vocabulary is the accepted eleven-key set. Each capability resolves as
manual override, confirmed endpoint behavior, provider metadata, trusted registry, heuristic, then
unknown. Supported, unsupported, and unknown are distinct; source revision and bounded provenance are
preserved. Provider metadata discovery records only bounded context/output-limit fields. Runtime
contradictions have an observation seam and do not auto-promote into durable capability truth. Numeric
limits are authoritative only when their resolved capability state is supported; unsupported/unknown
values and manual facts outside the override authority are rejected. Settings can author a numeric
value for supported limit overrides while non-limit capabilities remain state-only.

## Settings and timeout

Generation settings are temperature, max output tokens, reasoning `none`, and optional timeout. The
inheritance chain is application defaults, exact model-on-connection defaults, then exact chat/model
overrides. Absent values inherit; explicit `none` reasoning remains distinct from unset. Switching a
chat model resolves that model's own chat override and does not leak settings from another model.
Tune and Settings preserve hidden exact-scope timeout/reasoning values when unrelated visible fields are
saved; explicit inheritance still clears only the requested chat/model scope.

Preparation rejects unsupported or unknown required settings and never clamps configured output
limits. It resolves one immutable request configuration object used both for dispatch and the Phase 5
snapshot. Timeout is unset by default. When configured, the application applies a B.O.T.S.-owned
deadline around streaming iteration; deadline expiry terminalizes immediately even when a backend
resists cancellation, while the child is detached and its result consumed later. Partial output is
durable, timeout is a distinct failure type, dispatch uncertainty is preserved, and no retry occurs.
Legacy campaign timeout/provider semantics are unchanged.

## Request snapshots and routing

Phase 5 snapshots use closed version `2` and contain attempt/chat/message identity, backend/profile and
legacy provider attribution, connection identity/name/revision, canonical endpoint, credential
source/reference/status only, model-entry identity, exact provider model, catalogue revision, prompt,
effective settings and provenance, consulted capabilities and provenance, relevant manual overrides,
and omitted-setting reasons. Unknown fields, duplicate keys, malformed capability/settings data, and
credential-shaped fields are rejected. Historical Phase 1–4 snapshot forms remain readable.

The built-in router prepares a request with frozen connection revision, endpoint, credential status,
model, capabilities, and settings, then routes only from that frozen request. Editing a connection or
changing a chat selection cannot reroute an active attempt. HTTP routing supports generic
OpenAI-compatible and configured OpenRouter profiles, but no public endpoint was contacted.

## Desktop

The existing Draft 1 shell now has a compact durable model selector grouped by connection and showing
available, stale, unavailable, or disconnected state. Tune edits the current chat/model settings and
shows inherited provenance with reset-to-inherited behavior. Settings provides connection create/edit,
enable/disable/retirement, endpoint/profile and credential source/reference/status, strict Secret
Service save/delete, explicit Refresh and Save & Refresh, manual models, application/model defaults,
and manual capability overrides. Saving a connection and discovery are explicit in the combined
`Save & Refresh` action; unrelated configuration saves do not discover. The existing inspector is extended
only with concise frozen connection/model attribution, effective settings, and capability provenance.

The selector keeps a `selection_required` chat visibly unselected until an operator chooses a model.
Core commands and persisted events are used for cross-window convergence. Selection/configuration state
is re-queried from the shared store; open Settings and Tune dialogs are refreshed as well, and form
revisions are carried into consequential saves so stale edits fail with a revision conflict. No
per-window durable provider copy exists. Settings includes an application-default model selector,
effective capability/limit provenance, per-field model inheritance, and an explicit replacement-model
choice for retirement; it never silently selects a replacement.

The Connections surface now has an explicit `+ Add Connection` workflow. A compact dialog selects one
of the closed built-in connection definitions: deterministic fake, local Ollama/OpenAI-compatible,
generic OpenAI-compatible HTTP, or OpenRouter. The selected definition supplies the backend/profile,
endpoint requirement/default, authentication requirement and allowed credential sources, and whether
the existing catalogue discoverer is available. `Save` validates and persists only the durable
connection/reference state; it performs no provider call. `Save & Refresh` performs exactly one explicit
refresh after a successful save. Secret Service values are accepted only for the immediate store call,
cleared from the Qt field before returning to the event loop, and never retained by the connection
workflow. Existing connection editing/lifecycle controls remain on the Settings surface, and the
shared event/re-query path makes add/edit changes converge across open windows. This is a closed
built-in descriptor table, not a plugin or arbitrary API interpreter; AI Horde remains future work.

## File inventory

Changed or added candidate paths:

* `pyproject.toml`
* `src/bots5/bootstrap/desktop.py`
* `src/bots5/core/application.py`
* `src/bots5/core/generation.py`
* `src/bots5/core/provider_configuration.py`
* `src/bots5/core/secrets.py`
* `src/bots5/desktop/widgets.py`
* `src/bots5/desktop/window.py`
* `src/bots5/domain/models.py`
* `src/bots5/domain/provider.py`
* `src/bots5/infrastructure/generation/openai_compatible.py`
* `src/bots5/infrastructure/generation/router.py`
* `src/bots5/infrastructure/persistence/migration_runner.py`
* `src/bots5/infrastructure/persistence/migrations/versions/0005_generation_outcomes.py`
* `src/bots5/infrastructure/persistence/migrations/versions/0007_phase5_provider_model_configuration.py`
* `src/bots5/infrastructure/persistence/migrations/versions/0008_catalogue_refresh_outcomes.py`
* `src/bots5/infrastructure/persistence/migrations/env.py`
* `src/bots5/infrastructure/persistence/phase3_validation.py`
* `src/bots5/infrastructure/persistence/phase5_store.py`
* `src/bots5/infrastructure/persistence/phase5_validation.py`
* `src/bots5/infrastructure/persistence/schema.py`
* `src/bots5/infrastructure/persistence/sqlite.py`
* `src/bots5/infrastructure/persistence/transition_guard.py`
* `src/bots5/infrastructure/secrets.py`
* `src/bots5/providers/discovery.py`
* `src/bots5/providers/openai_compatible.py`
* `src/bots5/providers/openrouter.py`
* `tests/test_desktop_draft1.py`
* `tests/test_local_provider.py`
* `tests/test_phase1_persistence.py`
* `tests/test_phase2_persistence.py`
* `tests/test_phase3_generation.py`
* `tests/test_phase5_provider_model.py`

## Validation and audit history

Round 0 implementation validation:

* full deterministic matrix: **284 passed, 1 skipped** out of 285 collected;
* skipped test is the explicitly opt-in live local-Qwen acceptance test;
* Phase 1–4 persistence/core/desktop/concurrency suites, Phase 3 provider/snapshot/cancellation
  probes, legacy campaign suite, and `Provider.complete()` compatibility are included in the matrix;
* `compileall` passed;
* `git diff --check` passed;
* fresh, populated Phase 4, earlier migration-chain, retry, malformed-schema, raw-DML, snapshot,
  capability, settings, secret-boundary, recovery-collision, timeout, and fake/OpenRouter
  configuration tests passed;
* ordinary deterministic validation used no provider/public network activity.

Round 1 independent Sol/xhigh audit: **BLOCKED**. The reviewer reproduced seven in-scope defects:
generic Phase 5 routing was rejected by the Phase 3 provider-ID guard; non-`stop` Phase 5
completions were accepted; discovered output limits were not enforced; manual catalogue promotion
was lost on failed refresh; nested/case-variant secret fields escaped the snapshot boundary;
Tune pinned inherited timeout values; and open Settings/Tune windows did not converge on configuration
events.

Round 2 repair validation:

* full deterministic matrix: **290 passed, 1 skipped** out of 291 collected;
* the seven reported defects have focused regression coverage;
* Phase 5 v2 snapshots are also excluded from the public-store Phase 3 outcome classifier;
* compileall, `git diff --check`, and the complete legacy/provider/campaign/concurrency matrix passed;
* no provider/public network activity occurred.

Round 2 independent Sol/xhigh audit: **BLOCKED**. The reviewer reproduced seven additional
in-scope defects: current-schema/raw-DML authority did not fully reject malformed Phase 5 rows and
allowed deletion of a model referenced by history; non-finite timeout values crossed a public write
boundary; v2 snapshots admitted incoherent capability/credential/omitted-setting structures; the
selection revision check was racy; stale provider-metadata limits remained effective after refresh
omission; OpenRouter omitted explicit reasoning `none`; and a failed Phase 5 attempt could carry a
`stop` finish reason.

Round 3 repair validation:

* full deterministic matrix: **297 passed, 1 skipped** out of 298 collected;
* added SQLite row guards and raw-type checks, historical-safe attribution validation, atomic
  selection CAS, finite-setting validation, strict v2 snapshot coherence, stale capability
  invalidation, additive source/revision capability evidence, OpenRouter reasoning emission, and
  Phase 5 terminal-state/finish-reason/certainty guards;
* added secret-shaped metadata rejection, disabled-trigger startup rejection, backend-native timeout
  classification, duplicate-ID Settings labelling, and revision-carrying desktop configuration saves;
* compileall, `git diff --check`, migration-chain, recovery, legacy/provider/campaign/concurrency,
  and secret-boundary checks passed;
* no provider/public network activity occurred.

Round 3 independent Sol/xhigh audit: **BLOCKED** against its opening seal. It independently
reproduced raw-DML historical attribution injection, secret-bearing nested model metadata,
destructive single-row capability precedence, a disabled current-schema trigger, stale desktop
connection saves bypassing CAS, complete/stop with unknown remote outcome, and backend-native
timeouts misreported as B.O.T.S.-owned deadlines; it also identified duplicate model IDs lacking
connection labels in Settings. The target changed during that audit while repairs were in progress,
so its verdict is evidence for the repair loop rather than closure of the current candidate.

Round 4 repair validation:

* full deterministic matrix: **298 passed, 1 skipped** out of 299 collected;
* the B.O.T.S.-owned deadline now races streaming work against its own timer, so a backend-native
  `TimeoutError` remains a backend failure even when a deadline is configured;
* secret-shaped keys are normalized across case and separators, including camelCase forms;
* disabled Phase 5 triggers are rejected by current-schema startup validation;
* capability-override Settings writes carry an expected revision and reject stale forms;
* compileall, `git diff --check`, migration-chain, recovery, legacy/provider/campaign/concurrency,
  and secret-boundary checks passed;
* no provider/public network activity occurred.

Round 4 independent Sol/xhigh audit: **BLOCKED**. The reviewer reproduced five additional
in-scope defects: the selection-required selector could not emit a choice when the only visible
model was auto-selected by Qt; completion-trigger markers were incomplete; raw SQLite DML could
commit a nested secret-bearing catalogue metadata value before startup rejected it; the current
schema type map omitted capability overrides; and the report inventory omitted `openrouter.py`.

Round 5 repair validation:

* full deterministic matrix: **300 passed, 1 skipped** out of 301 collected;
* raw SQLite metadata and capability-provenance writes now reject nested secret-shaped keys before
  SQLite commit, including case- and separator-normalized forms;
* completion triggers are covered by current-schema marker validation, all Phase 5 table columns
  are checked for declared types, and the selection-required UI path has focused coverage;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Round 5 independent Sol/xhigh audit: **BLOCKED**. The reviewer reproduced six in-scope defects:
raw SQLite secret-key detection missed arbitrary separators; capability evidence accepted duplicate
identities and unbounded provenance; the current schema type map omitted `chat_model_selection.updated_at`;
valid disable/refresh workflows could leave an unavailable application default that failed reopen;
application generation settings CAS was not atomic; and core configuration imported concrete
infrastructure SecretStore implementations and Phase 5 validation.

Round 6 repair validation:

* full deterministic matrix: **302 passed, 1 skipped** out of 303 collected;
* focused regressions cover arbitrary-separator secret keys, strict capability provenance and
  identity guards, complete current-schema typing, unavailable-default restartability, and
  application-settings CAS;
* core now uses an injected SecretStore port and bootstrap composition for concrete adapters;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Round 6 independent Sol/xhigh audit: **BLOCKED**. The reviewer independently reproduced five
executable Phase 5 defects and one evidence defect: a current attribution trigger could be disabled
with an `AND 0` predicate; NUL-separated secret keys bypassed the SQLite JSON guard; equal-precedence
capability facts resolved according to insertion order; a stale Tune form could write to a newly
selected model; stamped `chat_model_selection.updated_at` nullability was not enforced; and the
report claimed 304 passes while that audit's disposable candidate ran 302.

Round 7 repair validation:

* full deterministic matrix: **304 passed, 1 skipped** out of 305 collected;
* equal-precedence capability resolution is revision-stable; stale Tune writes carry and verify the
  exact model identity; NUL secret keys and disabled `AND 0` triggers are rejected; and stamped
  Phase 5 nullability is checked at startup;
* focused Phase 5 suite: **28 passed**;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Round 7 independent Sol/xhigh audit: **BLOCKED**. The reviewer independently reproduced seven
additional in-scope defects: raw DML could commit malformed stamped application settings and reopen
them; application default-model selection accepted a retired connection; oversized discovery metadata
could partially commit catalogue state before capability persistence failed; a Settings model switch
could save defaults to the wrong model; one global inheritance control could not preserve per-field
inheritance; the minimum Settings surface lacked application-default-model and effective capability/
limit provenance controls; and desktop retirement silently selected the first available replacement.

Round 8 repair validation:

* full deterministic matrix: **308 passed, 1 skipped** out of 309 collected in 17.88 seconds;
* focused Phase 5 suite: **32 passed**;
* stamped settings revisions/timestamps and Phase 5 nullability are validated in triggers and startup
  row checks; retired connections cannot be selected as application defaults; oversized discovery is
  rejected before catalogue mutation; Settings model-default saves carry the selected model identity
  and preserve each field's inherited state; application-default model and effective capability
  provenance controls are real; and retirement requires an explicit valid replacement;
* compileall, `git diff --check`, migration-chain, recovery, legacy/provider/campaign/concurrency,
  and secret-boundary checks passed; no provider/public network activity occurred.

Round 8 independent Sol/xhigh audit: **BLOCKED**. The reviewer independently reproduced five
additional in-scope defects: a Phase 5 table rebuilt without its primary/foreign-key constraints
passed startup; a marker-preserving inert provider-connection trigger passed startup and allowed a
malformed raw row; a raw manual capability fact without an override passed startup but contradicted
the closed v2 snapshot contract; an unsupported numeric output-limit fact was treated as authoritative;
and Qt Tune/Settings saves erased hidden exact-scope timeout or reasoning values when saving unrelated
visible fields.

Round 9 repair validation:

* full deterministic matrix: **308 passed, 1 skipped** out of 309 collected in 19.38 seconds;
* focused Phase 5 suite: **32 passed**;
* current-schema validation now checks Phase 5 primary/foreign-key structure and exercises guaranteed
  DML boundaries in savepoints, capability truth is closed consistently at domain, SQLite, and snapshot
  boundaries, unsupported numeric limits are non-authoritative, and Tune/Settings preserve hidden
  timeout/reasoning values while explicit inheritance remains a clear operation;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 9 repairs.
Closure is not inferred from local tests; this audit must inspect the complete candidate and expose
an exact terminal `PASS` or `BLOCKED` disposition.

Round 10 independent Sol/xhigh audit: **BLOCKED**. The reviewer independently reproduced five
additional in-scope defects: a cancellation-resistant backend could hold the B.O.T.S. timeout path
open indefinitely; an unexpected generic-provider transport exception leaked a synthetic credential
through a durable error and event; raw capability DML could commit an outside-vocabulary key,
negative source revision, and malformed timestamp before restart rejection; stamped-schema validation
did not require the declared capability-observation CHECK constraint; and Settings could not author a
numeric limit override.

Round 11 repair validation:

* full deterministic matrix: **310 passed, 1 skipped**; focused Phase 5/provider coverage: **54 passed**;
* timeout terminalization no longer waits for cancellation-resistant backend tasks and preserves
  partial output/uncertainty; generic and OpenRouter-compatible unexpected transport failures are
  sanitized before persistence/publication; capability vocabulary, numeric value, revision, and
  timestamp guards apply at SQLite DML boundaries; stamped schema validation requires declared
  Phase 5 CHECK constraints; and Settings provides a numeric value control for supported limit
  overrides;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Round 12 repair validation:

* full deterministic matrix: **312 passed, 1 skipped in 18.26 seconds**;
* capability-fact `observed_at` and capability-override `reason` are covered by update-column
  triggers, CHECK constraints, startup validation, and raw-DML regression probes;
* discovery/application diagnostics redact credential-bearing unexpected exceptions; catalogue refresh
  CAS includes connection revision and credential identity changes invalidate discovered state;
* credential forms carry the captured connection revision/reference and reject stale writes before the
  SecretStore is touched; the Settings capability editor clears absent-key state/value/reason before
  another override can be saved;
* focused Phase 5/provider coverage: **56 passed**; compileall and `git diff --check` passed; no
  provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 12 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Round 13 repair validation:

* full deterministic matrix: **314 passed, 1 skipped in 18.58 seconds**;
* focused Phase 5 suite: **33 passed**;
* stale credential deletion now carries the captured connection revision and credential reference,
  so a stale form cannot delete a newer credential; endpoint/provider capability facts require a
  catalogue revision and are invalidated when that revision changes; stamped CHECK validation now
  verifies the declared expressions rather than names alone; and raw chat-selection DML cannot commit
  a model ID together with `selection_required`;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 13 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Fresh independent Sol/xhigh audit (Round 8): **BLOCKED**. The reviewer reproduced three further
authority defects: stamped CHECK validation case-folded quoted literals and accepted uppercase
capability values that then rejected valid lowercase writes; a caller-supplied catalogue revision
could move `1` back to `0` and resurrect stale provider metadata; and fixed recovery temporary paths
could overwrite and then delete an existing valid temporary database/metadata pair. The reviewer also
observed one load-sensitive timeout-test failure before partial output persistence, despite a later
passing rerun. Its independent evidence was `314 passed, 1 skipped in 20.33 seconds`, Phase 5
coverage `37 passed in 3.63 seconds`, and the canonical qasync production-window subset `4 passed`.
No target files, credentials, providers, public network, refs, or Git state were touched.

Round 14 repair validation:

* full deterministic matrix: **316 passed, 1 skipped in 18.62 seconds**;
* focused Phase 5 suite: **40 passed in 3.29 seconds**;
* CHECK-expression validation now preserves quoted-literal case while tolerating only SQL
  formatting/keyword variation; provider catalogue revisions are derived from the current row and
  raw DML cannot regress them; recovery database and metadata temporaries use collision-safe unique
  paths and preserve pre-existing temporary evidence; and the timeout regression waits for durable
  terminal state rather than using a load-sensitive fixed sleep;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 14 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Round 15 repair validation:

* full deterministic matrix: **317 passed, 1 skipped in 18.72 seconds**;
* the core secret boundary now rejects every discovery record containing the resolved credential
  before catalogue persistence, regardless of which discoverer produced it; discovery diagnostics
  use the same core-owned sanitizer, and no application dependency points at the concrete discovery
  adapter for secret handling;
* a synthetic `httpx.MockTransport` endpoint that echoes its bearer credential as `owned_by` is
  rejected with a sanitized error, receives the expected in-memory credential, and leaves no
  catalogue row or SQLite value containing that credential;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 15 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Round 16 repair validation:

* the startup Phase 5 DML authority probe now exercises both catalogue metadata insert and update
  guards in savepoints; an inert marker-preserving metadata-insert trigger (`AND (2 = 3)`) is covered
  by a stamped-schema regression and is rejected before the database is opened;
* full deterministic matrix: **317 passed, 1 skipped in 19.02 seconds**;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 16 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Round 17 repair validation:

* startup now behaviorally verifies model-catalogue origin, availability, and positive revision
  CHECK boundaries, and exercises capability-fact truth on INSERT as well as UPDATE; therefore a
  stamped tautological catalogue CHECK or marker-preserving inert capability-fact INSERT guard is
  rejected before the database is opened;
* the stamped-schema regressions cover both weakened `origin` CHECK text and an inert capability-fact
  truth INSERT trigger;
* full deterministic matrix: **317 passed, 1 skipped in 19.01 seconds**;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 17 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Round 18 repair validation:

* stamped Phase 5 table and trigger authority is now compared against canonical DDL freshly produced
  by the migration itself, with formatting normalization that preserves case-sensitive literals;
  runtime savepoint probes remain in place for critical invalid writes. This rejects marker-preserving
  inert triggers and tautological named CHECK constraints rather than accepting them on substring
  matches;
* startup probes also cover capability-observation truth on INSERT, using valid CHECK-level values
  and invalid closed-boundary fields so the trigger itself is required;
* full deterministic matrix: **317 passed, 1 skipped in 19.53 seconds**; focused Phase 5 suite:
  **40 passed**;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 18 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Round 19 repair validation:

* the core returned-data secret boundary now traverses the `Mapping` protocol rather than only
  concrete dictionaries, covering custom discoverer record mappings; the echoed-credential regression
  now covers both the built-in in-memory HTTP path and a custom mapping record, with no catalogue row
  containing the synthetic credential;
* full deterministic matrix: **317 passed, 1 skipped in 19.41 seconds**; focused Phase 5 suite:
  **40 passed**;
* compileall and `git diff --check` passed; no provider/public network activity occurred.

Fresh independent Sol/xhigh audit: **pending** against the resealed candidate after Round 19 repairs.
Closure remains blocked until that fresh audit ends with exactly `PASS`.

Pre-addition final fresh independent Sol/xhigh audit (Round 16): **PASS**. The reviewer independently verified
the complete candidate before and after audit, with the same report-excluding seal
`c11c8d4987b741cd38aa95411f121ae03107d5de75474893b557b6b0b61ca3a1`, unchanged refs, and zero
staged files. Its socket-blocked deterministic evidence was **317 passed, 1 skipped in 22.18 seconds**;
the sole skip was the explicitly opt-in live-Qwen test. Focused Phase 5 coverage was **40 passed in
4.63 seconds**; the Phase 1-4 preservation partition was **277 passed, 1 skipped**; the Phase 4
production-window/qasync partition was **21 passed**; and the campaign/runner/manifest/storage/CLI/
provider/`Provider.complete()` compatibility partition was **111 passed**. Sixteen selected
migration, retry, recovery-collision, malformed-schema, raw-authority, restart, and timeout cases
passed; compileall, `git diff --check`, and all four offline manifest validations exited 0.
Independent disposable probes rejected mutations to all 17 Phase 5 CHECK constraints and 28
triggers, rejected both built-in and custom-mapping credential echoes before persistence, and
confirmed clean migration, foreign-key, raw-DML, snapshot, timeout, settings, selector, convergence,
restart, and historical-compatibility behavior. No concrete reproducible in-scope defect remained.

This PASS is the closure disposition for deterministic Phase 5 work. No live local-Ollama or
OpenRouter acceptance was performed, and no public-provider traffic is implied by the audit.

That PASS covered the candidate before the bounded operator-usability completion below and is therefore
superseded for closure purposes. The implementation remained on the same Phase 5 authority and added
no migration or new execution semantics.

## Bounded Add Connection completion validation

The new connection-manager regressions cover descriptor-derived field visibility, generic and
OpenRouter shapes, no-auth configuration, environment-reference-only credentials, fake Secret Service
storage, masked input clearing, required-field validation, explicit Save versus Save & Refresh, durable
creation without discovery, explicit fake discovery, and two-window add/edit convergence.

Post-addition deterministic validation passed: **324 passed, 1 skipped in 23.89 seconds** overall;
**44 passed in 3.90 seconds** for the Phase 5 provider/model suite; **165 passed in 17.02 seconds**
for the Phase 1–4 preservation partition including Draft 1 desktop; and **115 passed in 0.41 seconds**
for the legacy campaign/provider/CLI/manifest/storage compatibility partition. `compileall` and
`git diff --check HEAD` exited 0. No provider, public network, live OpenRouter, or real credential-store
access occurred. A fresh independent Sol/xhigh audit is required against this updated seal.

Add Connection independent Sol/xhigh audit (Round 20): **BLOCKED**. The reviewer independently
reproduced five concrete Phase 5 defects: a newly discovered model on a newly added fake connection
had no trusted fake capability facts and could not prepare generation; the public edit path could
resurrect a retired connection and retire an application-default connection without a replacement;
the OpenRouter descriptor's required-auth rule was not enforced after creation or through raw SQLite
DML; connection reprofiling preserved incompatible catalogue and capability authority; and the report
inventory omitted `tests/test_desktop_draft1.py`. The audit's socket/DNS-blocked disposable evidence was
**324 passed, 1 skipped** overall, **44 passed** focused Phase 5, and **115 passed** in the
campaign/provider/CLI/storage compatibility partition. The candidate was unchanged by the reviewer,
with refs at the Phase 4 baseline and an empty index.

Round 21 repair validation:

* fake discovery now seeds the same bounded trusted capability facts as the built-in fake state;
* generic connection editing cannot mutate lifecycle flags, retired connections cannot be re-enabled,
  and raw SQLite retirement of the application-default connection requires the existing replacement
  transaction;
* OpenRouter required authentication is enforced by the core, row parser, CHECK constraint, SQLite
  transition guard, and migration-owned raw-DML trigger;
* endpoint/profile/credential identity changes stale every catalogue row on that connection and clear
  current capability facts, preserving historical attribution while preventing cross-adapter reuse;
* focused regressions cover all five findings;
* full deterministic matrix: **329 passed, 1 skipped**; focused Phase 5 suite: **49 passed**;
* compileall and `git diff --check HEAD` passed; no provider/public network or real credential-store
  access occurred.

A fresh independent Sol/xhigh audit is required against the resealed candidate after Round 21.

Add Connection follow-up independent Sol/xhigh audit (Round 22): **BLOCKED**. The reviewer independently
reproduced six concrete raw-authority/CAS defects: retired connections could be resurrected by raw DML;
raw connection identity edits could preserve available catalogue and capability authority; raw assignment
of an unavailable model could make the application default unrestartable; model and chat-model generation
settings updates lacked a revision predicate; capability override updates had the same lost-update race;
and the SQL secret-key predicate rejected the benign `apricotKey` case while differing from Python
normalization. The audit used a disposable socket/DNS-blocked copy, with **329 passed, 1 skipped** overall,
**49 passed** focused Phase 5, **165 passed, 1 skipped** in preservation, **115 passed** in compatibility,
**8 passed** in the Add Connection/qasync partition, and **21/21** selected migration/recovery/raw checks.
The prior candidate was not mutated and refs remained at the Phase 4 baseline.

Round 23 repair validation:

* migration-owned guards now reject every retired-connection resurrection, raw connection identity edit,
  and invalid application default; the core arms the sole atomic identity-edit transaction, stales the
  connection catalogue, clears current capability facts, and then advances both revisions;
* model-generation, chat-model-generation, and capability-override writes now use conditional revision
  predicates, including deterministic interleaving regressions that force a competing update between read
  and write;
* Python and SQLite now share the same normalized forbidden-secret-key policy, while raw-DML metadata
  accepts benign `apricotKey` and continues rejecting case/punctuation variants of real secret-shaped keys;
* focused Phase 5 coverage: **53 passed in 4.47 seconds**; full deterministic matrix: **333 passed, 1 skipped
  in 25.75 seconds**; the skip remains the explicitly opt-in live-Qwen test;
* Phase 1–4 preservation plus Draft 1 partition remained **165 passed, 1 skipped**; the campaign/provider/
  CLI/manifest/storage compatibility partition remained **115 passed**; compileall and `git diff --check
  HEAD` exited 0. All migration, recovery, schema, raw-authority, historical-attribution, secret-scan,
  timeout, routing, and fake-provider checks used deterministic local state only.

A fresh independent Sol/xhigh audit is required against the resealed candidate after Round 23.

Round 24 independent Sol/xhigh audit: **BLOCKED**. The reviewer found four additional concrete
defects: non-empty manual-model metadata called removed helper names; raw SQLite secret-key
normalization still differed for a non-ASCII separator such as `api•key`; Save & Refresh retained the
plaintext Secret Service value in the UI coroutine while discovery was suspended; and switching
connection definitions twice overwrote an operator-entered custom connection name. The independent
socket/DNS-blocked evidence was **333 passed, 1 skipped** overall, **53 passed** focused Phase 5,
**165 passed, 1 skipped** for Phase 1–4/Draft 1, **115 passed** for compatibility, **21 passed** in
the Phase 4/qasync partition, and **8 passed** in the Add Connection/qasync partition. The audit
recomputed the prior seal, used no target mutation or real credentials, and ended `BLOCKED`.

Round 25 repair validation:

* manual catalogue metadata now uses the shared secret predicate for non-empty valid records;
* SQLite’s migration-owned predicate now matches the Python policy for ASCII and Unicode separators,
  rejects the `api•key` raw-write probe before persistence, and still accepts benign `apricotKey`;
* Add Connection clears its transient credential local immediately after Secret Service storage and
  before any explicit Refresh await; a suspended fake discoverer regression confirms no sentinel remains
  in the live UI coroutine; custom names remain stable across adapter-definition changes;
* focused Phase 5 coverage: **56 passed**; full deterministic validation: **336 passed, 1 skipped**;
  the skip remains the explicitly opt-in live-Qwen test. The Phase 1–4/Draft 1 preservation partition
  remained **165 passed, 1 skipped**, and the campaign/provider/CLI/manifest/storage compatibility
  partition remained **115 passed**. Compileall and `git diff --check HEAD` exited 0; all provider,
  public-network, and real credential-store access remained blocked.

A fresh independent Sol/xhigh audit is required against the resealed candidate after Round 25.

Round 26 independent Sol/xhigh audit: **BLOCKED**. Against the opening candidate seal
`74f5f7ac537c1a8f30f597ce2c745d0c3ba133b42418e037ac0dea9a6df663c3`, the reviewer reproduced a
raw SQLite write-boundary gap for Unicode case-fold spellings `APİ-KEY` and `APIKEY`. Python's
`casefold()` normalization rejects both as `apikey`, while the migration-owned metadata and
capability-provenance triggers accepted and committed them; later startup detection was too late
to protect the SQLite boundary. The same audit confirmed the ASCII and separator controls, non-empty
manual metadata, descriptor-derived generic/OpenRouter/local/fake shapes, custom-name retention,
Save versus Save & Refresh, suspended-refresh secret lifetime, multi-window/CAS/lifecycle behavior,
migration/recovery, legacy `Provider.complete()` compatibility, and report inventory. Its blocked
socket/DNS evidence was **335 passed, 1 skipped, 1 failed** overall (the single timeout test was a
load-sensitive run and passed in 10/10 isolated reruns), **56 passed** focused Phase 5, **165 passed,
1 skipped** preservation, and **115 passed** compatibility. The target was not changed by the
reviewer; the local repair began only after this verdict.

Round 27 repair validation:

* the shared SQL secret-key predicate now maps every non-ASCII Unicode code point whose Python
  `casefold()` contributes ASCII alphanumeric text, then applies the existing separator and residual
  checks; this keeps raw SQLite metadata and capability-provenance writes aligned with the Python
  boundary without requiring a UDF on raw connections;
* focused raw-authority coverage now rejects `APİ-KEY` and `APIKEY` before persistence while retaining
  the benign `apricotKey` control;
* the complete deterministic suite passed **336 tests, 1 skipped**; the Phase 5 suite passed **56**;
  Phase 1–4/Draft 1 preservation remained **165 passed, 1 skipped**, and campaign/provider/CLI/
  manifest/storage compatibility remained **115 passed**. Compileall and `git diff --check HEAD`
  passed; provider, public-network, and real credential-store access remained blocked.

A fresh independent Sol/xhigh audit is required against the resealed candidate after Round 27.

Round 27 independent Sol/xhigh audit: **BLOCKED**. Against the opening seal
`c674231f060ee2e2ea5a4351dc34a3f65bafc08d177a3013c911dda563a22e2f`, the reviewer reproduced two
concrete defects. First, with a genuine full EventBus subscriber queue, Save & Refresh could block
after the fake SecretStore write while the credential remained reachable in both the Add Connection
workflow and `save_connection_credential` frames; Qt state, payload state, and SQLite were clean,
but the immediate-store lifetime boundary was not met. Second, interrupted suffixed recovery files
such as `.tmp-<uuid>` and `.json.tmp-<uuid>` were included by the recovery glob; retry attempted to
fingerprint the JSON temporary artifact as SQLite and could not proceed. The reviewer otherwise
confirmed Unicode raw-DML rejection, descriptor-derived adapters, explicit discovery, lifecycle/CAS,
historical attribution, migration, timeout, and legacy preservation. Its socket/DNS-blocked evidence
was **336 passed, 1 skipped** overall, **56 passed** focused Phase 5, **165 passed, 1 skipped**
preservation, and **115 passed** compatibility. The target was not changed by the reviewer.

Round 28 repair validation:

* credential saving now owns its command scope without the generic argument-retaining decorator,
  sanitizes failures before clearing the value, and clears the core local before status-event
  publication; Add Connection and Settings detach the Secret Service save task and clear their own
  transient value before awaiting event backpressure. A real full-queue regression scans pending
  coroutine frames and releases the queue only after proving the sentinel is unreachable;
* recovery discovery recognizes only actual suffixed recovery artifacts. Interrupted database and
  metadata temporary files are excluded from candidate fingerprinting, never overwritten or removed,
  and a retry creates and verifies a new collision-safe recovery point. A regression preserves both
  `.tmp-<uuid>` and `.json.tmp-<uuid>` files byte-for-byte across retry;
* focused Phase 5 coverage: **58 passed**; full deterministic validation: **338 passed, 1 skipped**;
  the Phase 1–4/Draft 1 preservation partition remained **165 passed, 1 skipped**, and the
  campaign/provider/CLI/manifest/storage compatibility partition remained **115 passed**. Compileall
  and `git diff --check HEAD` exited 0; no provider, public-network, or real credential-store access
  occurred.

A fresh independent Sol/xhigh audit is required against the resealed candidate after Round 28.

Round 28 independent Sol/xhigh audit: **BLOCKED**. Against the opening seal
`34de6a19ded195216bad9f02263f06849153e7f5e1dd2d7ed4eaac2de6f17e74`, the reviewer reproduced one
remaining credential-lifetime defect. With a genuinely full EventBus queue, Add Connection → Save &
Refresh completed the Secret Service write and durable catalogue update, then suspended publishing
`model_catalogue_changed`; the plaintext credential remained reachable through the awaited
`BotsApplication.refresh_models` coroutine. The Add/Settings payloads, immediate save frames, and
SQLite were clean. The candidate regression only inspected the parent task stack and did not cover
this post-discovery publication suspension. The audit otherwise confirmed the complete Unicode
raw-DML boundary, descriptor-derived connection shapes, explicit-only discovery, CAS/lifecycle and
multi-window behavior, recovery retry, timeout, historical attribution, and Phase 1–4/campaign/
`Provider.complete()` preservation. Its socket/DNS-blocked evidence was **338 passed, 1 skipped**
overall, **58 passed** focused Phase 5, **165 passed, 1 skipped** preservation, and **115 passed**
compatibility. The target remained unchanged apart from an ignored audit-created bytecode cache,
which was not used as evidence; no real credentials or provider/network calls were made.

Round 29 repair validation:

* `refresh_models` now sanitizes and records failure outcomes, clears the resolved credential and
  other secret-bearing locals before any catalogue-event await, and applies the same boundary on
  successful Save & Refresh publication;
* a focused regression fills the real EventBus queue after the Secret Service save, completes fake
  discovery, verifies catalogue persistence, recursively inspects the suspended task's awaited
  coroutine chain, and confirms the sentinel is absent before releasing backpressure;
* focused Phase 5 coverage now passes **59 tests**; a fresh full deterministic matrix and independent
  Sol/xhigh audit are required against the resealed candidate.

Round 29 independent Sol/xhigh audit: **BLOCKED**. Against the opening seal
`fc438712ae4216825d21b74730256ab01a64fa9d0757f7e2f6673a456c94eeb2`, the reviewer verified that the
Round 28 Save & Refresh backpressure defect was fixed, including recursive task/await-chain
inspection, but found a separate failure-path leak. A completed failed credential-save task retained
the submitted plaintext through its exception traceback/context in the application, provider
configuration, and Secret Service adapter frames; a stale revision/reference rejection likewise
retained it in the application frame. Displayed errors, Qt state, SQLite, snapshots, and diagnostics
were clean, but retained task exception graphs violated the secret boundary. The audit otherwise
confirmed the full Add Connection authority, strict Secret Service path, explicit-only discovery,
CAS/lifecycle/multi-window behavior, migration/recovery, immutable attribution, and Phase 1–4/
campaign/`Provider.complete()` preservation. Its socket/DNS-blocked evidence was **339 passed, 1
skipped** overall, **59 passed** focused Phase 5, **165 passed, 1 skipped** preservation, and **115
passed** compatibility. The target remained unchanged and no real credentials or provider/network
calls were made.

Round 30 repair validation:

* credential-save failures now create a fresh sanitized `SecretStoreError` after the source exception
  block ends, clear the submitted value in a `finally` boundary, and preserve stale-CAS error types
  without retaining the value in traceback frames;
* `ProviderConfiguration.save_credential`, the strict Secret Service adapter, and the deterministic
  fake store clear secret-bearing locals and discard source exception context before re-raising;
* regressions walk task exception cause/context/traceback graphs for generic store failure and stale
  revision failure, while the Save & Refresh full-queue recursive await-chain regression remains;
* the Phase 5 provider/model module passes **60 tests**, and the combined focused Phase 5/UI slice
  passes **61 tests**; a fresh full deterministic matrix and independent Sol/xhigh audit are required
  against the resealed candidate.

Round 31 independent Sol/xhigh audit: **PASS**. Against the opening seal
`f4a68848848dfed2f23f72579b861789c47018df6bcf284f504c8e88742fdc29`, the reviewer independently
confirmed the Round 30 exception-graph repair and found no concrete Phase 5 blocker. Generic Secret
Service failure, stale revision, and stale reference completed-task graphs contained no credential in
their causes, contexts, traceback frames/locals, or awaited children; strict adapter and provider
configuration boundaries were likewise clean. The full Save & Refresh queue suspension remained
credential-free after durable discovery and catalogue persistence, with exactly one discovery and no
retry; Save-only remained network-free. The reviewer also confirmed descriptor-derived generic,
local/no-auth, and OpenRouter Add Connection shapes, strict Secret Service selection, durable identity
separation, explicit discovery, CAS/lifecycle/multi-window behavior, immutable attribution, migration/
raw-DML/recovery protections, timeout semantics, and Phase 1–4/campaign/`Provider.complete()`
preservation. Its disposable Python 3.12.13 socket/DNS-blocked evidence was **341 collected, 340
passed, 1 skipped** overall, **61 passed** focused, **165 passed, 1 skipped** preservation, and
**115 passed** compatibility; compilation, diff hygiene, and four offline manifest validations all
exited 0. The sole skip was the deliberately opt-in live-Qwen acceptance path. No provider/network,
real credential-store, target mutation, Git ref operation, staging, fetch, or OrgMem write occurred.

Round 32 repair validation:

* the confirmed discovery-failure semantics defect was repaired with additive migration
  `0008_catalogue_refresh_outcomes`, typed transport/timeout/provider-HTTP/protocol/unknown failure
  classification, closed sanitized diagnostics, restart-safe state projection, and event/re-query
  convergence;
* focused discovery semantics regressions cover successful non-empty and empty catalogues, refusal,
  timeout, HTTP failure, malformed JSON/shape, stale-cache and manual-model preservation, subsequent
  success replacement, restart distinction, diagnostic secret exclusion, and a second-store
  event/re-query convergence probe;
* full deterministic matrix: **343 passed, 1 skipped** out of **344 collected**; focused Phase 5
  provider/model plus Draft 1 desktop slice: **76 passed**; the Phase 5 provider/model module:
  **63 collected and passed**;
* compileall, `git diff --check`, migration/recovery/raw-authority, and secret-boundary validation
  passed;

Round 33 bounded repair validation:

* the raw-DML refresh-outcome authority gap found by the independent Terra review was repaired with
  the canonical `ck_catalogue_refresh_failure_diagnostic` registration and a SQLite
  transition-guarded atomic refresh pair; raw catalogue-revision increments and refresh-state
  rewrites require core authority, while normal refresh transitions and identity invalidation remain
  distinct and valid;
* the focused regression `test_raw_catalogue_refresh_outcome_cannot_be_forged_or_revised_in_place`
  passed; full deterministic validation is **344 passed, 1 skipped** out of **345 collected**;
  Phase 5 provider/model coverage is **64 passed**, and compileall plus `git diff --check` passed;
* final targeted independent Terra rechecks against this seal all ended **PASS**: A verified raw
  same-revision and chained provider-revision forgery rejection plus normal transitions; B verified
  canonical migration/startup authority, raw-DML rejection, rollback, and atomic success/failure/
  empty transitions; C verified restart, event/re-query, secret-boundary, and explicit-refresh-only
  UI behavior. No live provider, credential, public-provider, or network activity occurred.
* live local-Ollama acceptance was subsequently completed against the separately authorized local
  endpoint; no public-provider traffic occurred, and the candidate remains unstaged and
  uncommitted; the live evidence is recorded below.

## Live local-Ollama acceptance

The separately authorized production-path acceptance used a fresh disposable root at
`/tmp/bots5-phase5-live-renewed3.zc1szF` and only the local endpoint
`http://192.168.50.223:11434/v1`. Public-provider credential variables and proxy variables were
unset before launch. The production desktop runtime was started with the normal Phase 5 application
bootstrap and actual `MainWindow`/Settings/Add Connection/Tune/selector/inspector widgets; no
automatic discovery occurred at startup or restart.

The operator path created `Local Ollama` through `Settings → Connections → + Add Connection` using
the generic OpenAI-compatible HTTP backend/profile, canonical endpoint above, and authentication
`none`. Ordinary `Save` persisted UUIDv7 connection
`01a074d4-4c45-7ce0-9c24-852e8622a1ec`, revision 1, without contacting the provider: refresh status
remained `never`, catalogue revision remained 0, and the connection had zero catalogue entries.
The second open production window observed the durable connection. One explicit Settings `Refresh`
then reached only the local endpoint and returned 11 models, including discovered available
`qwen3.5:35b-a3b` as model entry
`01a074d4-4e94-7522-9b67-1d70955dd20c`; catalogue and refresh revisions were both 1. No retry or
manual model substitution occurred.

The real top-bar selector durably selected that model in the current chat and the second window
converged on the same selection. Discovery left required request capabilities unknown, so the
production Settings override controls explicitly established `generation.streaming`,
`request.temperature`, `request.max_output_tokens`, and `request.reasoning_effort.none` as
`supported` with `manual` provenance. Tune then saved chat/model overrides of temperature `0`, max
output `4096`, and reasoning `none`; timeout remained unset, and the three visible settings resolved
from `chat_model` scope.

One intentional streamed generation used the exact prompt from the acceptance authorization. It
created attempt `01a074d4-5194-7444-bafc-aa4af8a2ccb9`, returned model `qwen3.5:35b-a3b`, preserved
404 characters of visible assistant output, and terminalized as `state=complete`, exactly
`finish_reason=stop`, with no retry, substitution, or remote-outcome uncertainty. The persisted
Phase 5 snapshot v2 froze the connection UUID/name/revision, `openai_compatible_http`/`generic`
identity, canonical endpoint, model entry/exact provider model, catalogue revision 1, effective
settings, `chat_model` provenance, manual capability decisions, `credential_source=none`,
`credential_status=not_configured`, and no credential value. Its SHA-256 is
`ecb208aca974cc67cf3df68af0e6c3b33eac33f3fa3c5aaca6c20ddc0f8f3bbf`.

Changing a future chat/model max-output value to `4095` left the completed snapshot unchanged and
converged to the second open Tune dialog. The live catalogue contained another genuinely
discovered model, so switching away and back restored the Qwen-specific settings. Settings also
exercised disable/re-enable without erasing cached catalogue entries. The adapter-aware Add
Connection form was inspected for OpenRouter without saving or contacting it; its endpoint and
environment/Secret Service options were present, `none` was absent, and the Secret Service input
was masked. No public connection was created.

After normal close, restart restored the same UUIDv7 connection, 11-entry cached catalogue, exact
Qwen entry and chat selection, future `4095` Tune override, all four manual capability overrides,
completed transcript, and immutable snapshot/outcome. A post-startup observation showed no
catalogue revision change, confirming no automatic restart discovery. The local connection remained
no-auth; SQLite, the snapshot, and the acceptance state contained no credential value or public
credential reference, and all public-provider credential variables were absent. The final live
classification was **LIVE ACCEPTANCE PASS**. No OpenRouter/public-provider traffic occurred.

## Deferred and known limitations

Local Ollama live acceptance is recorded above. OpenRouter support is deterministic
configuration/routing support only until separately authorized live acceptance. Durable favourites
and recents, instructions/profiles, context construction/budgeting, attachments, search, the full
Phase 8 inspector, backup/restore, campaign UI, tools/MCP, plugin architecture, and all other
explicitly deferred Phase 6+ work remain unimplemented.
