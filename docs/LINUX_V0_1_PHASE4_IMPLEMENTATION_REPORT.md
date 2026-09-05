# Linux v0.1 Phase 4 implementation report

Status: bounded Phase 4 repair complete; optional local reasoning-effort compatibility repair complete; deterministic validation passed; fresh independent Sol/xhigh audit returned PASS; live local-Qwen acceptance passed.

## Authority and repository state

Implementation was authorized against baseline `main` / `HEAD` / locally recorded `origin/main`:

`9d921e7cd052d375ed950e39a20ad854d5b03231`

The repository remains on `main` with that commit as `HEAD`. Phase 4 and compatibility changes are intentionally unstaged and uncommitted. No staging, commit, push, fetch, OpenRouter, public-internet, or model-management operation was performed. The authorized local Ollama diagnostics and live acceptance are recorded below.

Pre-closure repository seal: branch `main`; `HEAD` and locally recorded `origin/main` both equal `9d921e7cd052d375ed950e39a20ad854d5b03231`; the index was clean; only the accepted Phase 4, reasoning-compatibility, test, migration, and report paths were present in the working tree.

OpenRouter and local-Qwen credential variables were unset for deterministic validation. The test fixture blocks ordinary socket connections.

## Architecture implemented

- `BotsApplication` remains the sole semantic authority over one SQLite store, one EventBus, one ExecutionManager, and one generation backend.
- Cross-chat concurrent generations are allowed.
- The application and SQLite store reject a second active generation in the same chat.
- Cancellation is attempt-targeted and repeated cancellation of a terminal attempt returns its durable terminal record.
- `DesktopSessionController` owns one `CoreEventBridge` subscription and fans events to multiple `MainWindow` views.
- Window selection, transcript refresh tokens, editing state, and active-attempt presentation remain window-local.
- Non-final window close unregisters only that view.
- Final-window close uses explicit active-work confirmation and hands application shutdown to the bootstrap/runtime path.
- Activity and attention are session-derived and ephemeral. Durable message and attempt state remains authoritative.
- Collapsed rail buttons expose per-chat running, background-complete, attention, and selected indicators.

## Bounded OpenAI-compatible reasoning-effort compatibility repair

The authorized direct local Ollama probe established that `qwen3.5:35b-a3b` accepts `reasoning_effort: "none"` on the OpenAI-compatible endpoint and then emits visible `delta.content` without `delta.reasoning`, terminating with `finish_reason: "stop"`.

The smallest generic seam was added:

- `CompletionRequest.reasoning_effort` is optional and defaults to `None`.
- The OpenAI-compatible provider omits `reasoning_effort` when unset and emits exactly `"none"` when configured.
- The streaming backend and existing desktop runtime/CLI path accept only the verified optional value `"none"`.
- No global default changed; model selection, context construction, output-token defaults, retry behavior, finish-reason interpretation, reasoning persistence/display, OpenRouter payloads, and UI controls were unchanged.

Direct smoke evidence is preserved at `/tmp/bots5-qwen35-reasoning-none-psvudqav/`.

## Persistence and migration

Migration `0006_phase4_workspace` adds:

- `workspace_windows` for window identity/order, geometry, selected chat, rail state, open-state metadata, and update time;
- a partial unique index enforcing at most one `running` generation attempt per chat.
- deterministic reconciliation of older same-chat duplicate `running` attempts before the unique index is installed; the newest attempt remains for normal restart reconciliation, while older attempts and their streaming assistant messages become `aborted` with preserved partial content and uncertainty.

Malformed workspace geometry is ignored and falls back to a fresh/default workspace view. Closed windows are removed from the restore set; final application shutdown preserves the open final window state for restoration.

Attention badges are not persisted.

Stamped revision `0006_phase4_workspace` is rejected at startup if the workspace table, selected-chat foreign key, or active-chat partial unique index is missing or structurally malformed.

This repair round tightened the stamped active-chat index check from a substring test to an exact approved predicate check. The validator now requires the unique partial index on `generation_attempts.chat_id` to use only the exact lowercase literal `state = 'running'`, while tolerating safe identifier/keyword case, quoting, and SQL formatting. Extra predicates such as `AND 0`, `OR 1`, uppercase literals, and mixed-case literals are rejected.

Production `MainWindow.initialize()` now persists the approved workspace presentation state after successful initialization. Runtime cleanup removes that row if initialization or showing the window fails, so failed construction does not create a durable phantom. New window identities come from the shared application UUIDv7 `IdFactory`; existing non-final deletion and final-window restoration semantics remain unchanged.

The fresh independent audit of the preceding candidate returned `BLOCKED` with two concrete findings: the predicate validator made the SQL string literal case-insensitive, and production window IDs bypassed the UUIDv7 factory with UUIDv4 generation. Both findings were reproduced, repaired, and covered by focused regressions in this round.

Phase 3 outcome columns, transition guards, remote-outcome certainty, immutable lineage, recovery points, and restart reconciliation remain in force.

## Exact implementation inventory

Modified source:

- `src/bots5/bootstrap/desktop.py`
- `src/bots5/core/application.py`
- `src/bots5/core/ports.py`
- `src/bots5/desktop/widgets.py`
- `src/bots5/desktop/window.py`
- `src/bots5/domain/models.py`
- `src/bots5/infrastructure/persistence/migration_runner.py`
- `src/bots5/infrastructure/persistence/schema.py`
- `src/bots5/infrastructure/persistence/sqlite.py`
- `src/bots5/infrastructure/generation/openai_compatible.py`
- `src/bots5/providers/base.py`
- `src/bots5/providers/openai_compatible.py`

Added source:

- `src/bots5/desktop/session.py`
- `src/bots5/infrastructure/persistence/migrations/versions/0006_phase4_workspace.py`

Modified tests:

- `tests/test_desktop_draft1.py`
- `tests/test_phase1_persistence.py`
- `tests/test_phase2_persistence.py`
- `tests/test_phase3_generation.py`
- `tests/test_local_provider.py`
- `tests/test_phase1_desktop.py`

Added tests:

- `tests/test_phase4.py`

No campaign-engine, OpenRouter, provider completion semantics, model-selection, or context/memory source was modified. The local OpenAI-compatible adapter received only the bounded optional request field described above.

Production `DesktopRuntime.open_window()` now owns creation of additional `MainWindow` instances. The New Window action attaches each view to the existing runtime session, application, store, EventBus, execution manager, backend, and authority lock.

## Deterministic validation

Commands:

`./.venv/bin/pytest -o addopts=''`

`./.venv/bin/pytest -o addopts='' tests/test_phase3*.py`

`./.venv/bin/pytest -o addopts='' tests/test_phase4.py`

Final full-suite result:

- 276 passed
- 1 skipped, the existing opt-in local-Qwen acceptance test
- no provider/model/API/network calls

The final focused matrix was:

- Phase 3 regression: 80 passed, 1 skipped
- Phase 4: 21 passed
- total: 276 passed, 1 skipped

The focused provider/backend, desktop-parser, and Phase 3 streaming regressions passed, including unset-field omission, explicit `reasoning_effort: "none"`, unchanged payload fields, streaming content parsing, exact-stop completion, length incompleteness, and no-retry behavior.

Focused Phase 4 regressions include canonical stamped-schema acceptance; malformed `AND 0`, `OR 1`, uppercase, and mixed-case predicates; safe quoted identifiers; missing uniqueness; wrong indexed column; missing index; terminal generation followed by a later same-chat generation; immediate one- and two-window durability; UUIDv7 window identities; immediate restart restoration with stable IDs; failed initialization cleanup; non-final close deletion; and final-window restoration.

## Fresh independent closure audit

The fresh independent Sol/xhigh read-only audit of this candidate returned exactly one terminal disposition: `PASS`. It independently reproduced the repaired uppercase/mixed-case predicate cases, raw duplicate-active rejection, UUIDv7 factory identity sharing, immediate persistence, restart identity stability, initialization/show failure cleanup, non-final deletion, final restoration, shared-core behavior, shutdown/restart reconciliation, supported qasync/offscreen lifecycle, unchanged campaign/provider surfaces, and the zero-spend deterministic matrix.

Additional checks passed:

- `./.venv/bin/python -m compileall -q src tests`
- `git diff --check`
- Phase 1–3 regression suite
- legacy campaign tests and `Provider.complete()` compatibility tests
- Phase 4 cross-chat concurrency and failure isolation
- authoritative same-chat rejection
- independent and duplicate cancellation
- malformed workspace fallback
- ephemeral attention clearing
- multi-attempt restart reconciliation
- shared-core multi-window lifecycle
- legacy duplicate-active migration and stamped-schema structural validation
- closed-window detachment, remaining-window event delivery, and production New Window reachability
- qasync/offscreen desktop tests

## Local-Qwen smoke and live acceptance

The intended model was `qwen3.5:35b-a3b` at `http://192.168.50.223:11434/v1`, with effective Ollama context `16384`. The B.O.T.S. smoke used `temperature=0`, acceptance-only `max_output_tokens=4096`, and `reasoning_effort="none"`. It produced visible assistant content, durable `COMPLETE`, and `finish_reason="stop"` with `provider_id="local_openai"` and `backend_id="openai_compatible_http"`. Evidence: `/tmp/bots5-local-qwen-smoke-rpx4esib/`.

The final bounded Phase 4 live acceptance used the same model and endpoint, `reasoning_effort="none"`, and acceptance-only `max_output_tokens=8192`. Evidence: `/tmp/bots5-phase4-live-fs4_civ9/`.

Observed acceptance evidence:

- Chat A continued in the background while Chat B was started; each had independent attempt identity and transcript state.
- Repeated switching remained possible while both attempts were active.
- The production New Window action created a third view sharing the same application/workspace core; the probe window closed without affecting the remaining views.
- The collapsed rail showed per-chat running indicators.
- A was stopped through the production Stop control and became `ABORTED` with partial content preserved and uncertainty true.
- B continued after A stopped and reached durable `COMPLETE` with `finish_reason="stop"`.
- Background completion indication appeared while B was unviewed and cleared when B was viewed.
- Final-window shutdown asked for confirmation exactly once; the separately started shutdown attempt became `ABORTED` with partial content preserved.
- Restart preserved A aborted/partial truth, B completed truth, and shutdown aborted/partial truth; no generation remained active.
- Workspace restoration reopened the approved selected chat with collapsed-rail state.
- Saved captures were visually inspected: `02-collapsed-rail-running.png`, `04-background-completion.png`, `05-attention-cleared-on-view.png`, and `06-restored-workspace.png`.

The successful run emitted non-fatal `httpcore` async-generator cleanup warnings while canceled streaming connections were being torn down. The process exit status was zero, all acceptance assertions passed, and durable outcomes were correct. No provider/backend repair was made for that runtime cleanup observation; it is recorded for human closure review.

## Deviations

No material deviation from the approved bounded authority was identified.

Existing persistence tests were updated where their assertions encoded the previous schema head or intentionally expected same-chat concurrent starts. Those tests now assert the approved Phase 4 invariant instead.

## Known limitations

- Same-chat concurrent generation is intentionally rejected.
- Attention and background-completion indicators reset on restart.
- Workspace restoration is limited to the approved window presentation fields.
- Live local-Qwen acceptance was performed only against the explicitly authorized endpoint/model; no provider or public-internet substitution was used.
- Existing README, roadmap, and older Phase 3 report sections still contain stale historical wording about the pre-Phase 4 unstaged candidate; those documents were not used as implementation authority.

## Readiness

The two newly reproduced in-scope blockers and the previously reproduced Phase 4 blockers were repaired and covered by focused tests. The bounded reasoning-effort compatibility repair received a fresh independent Sol/xhigh audit returning exactly `PASS`. The local-Qwen smoke and Phase 4 live acceptance passed. The candidate is ready for human Phase 4 closure/commit adjudication, with all changes unstaged and uncommitted.
