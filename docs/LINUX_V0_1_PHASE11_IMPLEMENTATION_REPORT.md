# B.O.T.S. Linux v0.1 — Phase 11 Implementation Report

Status: **implementation complete except M7 (stopped for Mick on a tool dependency) and M8 (deferred).**
This report is the M9 deliverable. It is written at the **pre-commit boundary**: nothing has been staged,
committed, pushed, or had any ref mutated.

Implementation target: sealed design v5, sha256
`407d99df5e0d96c3e2a4cbfb16cb89ba7b0ae549cffbcc3e7e89441a17a2ccff` (verified this session), and its accepted
§12 mutation fence.

---

## 1. What landed

| Milestone | State |
|---|---|
| M0 — fence-free visual floor (theme tokens, scalable metrics, campaign-dock token migration) | landed |
| M1 — actions + command palette + keybinding UI | landed |
| M2 — Markdown/code/table rendering pipeline | landed |
| M3 — organisation (folders, pins, archive) + deletion (R-11 include, both forms) | **landed round 11** (see §4a) |
| M3b — A-1 titles / duplicate / sorts (F16/F17/F18) | landed |
| M4a / M0a — geometry clamping + restore-aware scrolling | landed |
| M4b / M6 — authoritative SQLite workspace/settings plane, wired into the desktop | landed |
| M5 / F8 — model-selector polish | landed |
| M7 — standalone build + target validation | **STOPPED FOR MICK** (see §5) |
| M8 — AppImage (conditional on R-9 and M7) | deferred |
| M9 — T4 closure + this report | this document |

Migration head is **`0016_phase11_workspace_state`**. The Phase 11 chain is
`0013_phase11_organisation` → `0014_phase11_message_tombstone` → `0015_phase11_duplicate_admission` →
`0016_phase11_workspace_state`.

## 2. Validation evidence

- **Full suite: exit 0, 0 failures**, re-run independently by the supervisor after every milestone
  (M4a wiring, M5, inventory regeneration, A-1 conformance). Runner:
  `/home/mick/.venvs/bots5-validate-314/bin/python` (Python 3.14, shared `libsqlite3`).
- **Non-vacuous collection proof:** all 22 T0–T3 design selectors verified to collect non-zero via
  `--collect-only` before execution.
- **Design T0 counts met exactly:** `test_phase11_chat_titles.py` 8, `test_phase11_chat_duplicate.py` 10,
  `test_phase1_acceptance.py` 2, `test_phase4.py` 24, `test_phase7_desktop.py` 10,
  `test_phase7_search_navigation.py` 29, `test_phase9_desktop_slice_e.py` 31 (extended in place).
- **Population floor preserved:** 0 of the 1,598 reviewed baseline node IDs were lost.
- **T4 inventory regenerated in-candidate:** `scripts/ci/t4_baseline_inventory.txt` now holds **1,846** node
  IDs, sha256 `5fcff24d46a17dd28b1b719c9ef3d13ba902feebcd6bdac278d2a4afd3f4927d`. `ci_support build-shards`
  reconciles `baseline_count == canonical_count == 1846` across 6 shards.
- **Migration round-trips proven:** 0015 → 0014 removes the duplicate-admission guard and re-upgrading restores
  it; 0016 → 0015 removes all four tables and both columns and restores the prior revision.
- **T4 itself has NOT been run.** By the design it must run once against the *exact final sealed* candidate; the
  candidate is not yet sealed, and M7 is stopped. Candidate bytes changed after any T4 supersede it.

## 3. Real defects found and fixed during implementation

Every one of these was invisible to the tests that existed at the time and was exposed only when a test that
actually invoked the feature was written.

1. **Draft/chrome persistence was dead code.** Migration 0016 and its store methods landed, but nothing in
   `desktop/` read or wrote them, so no user-visible restoration existed.
2. **`rename_chat` did not exist on the store.** `BotsApplication.rename_chat` delegated to it and raised
   `AttributeError`; F16 rename was entirely non-functional end to end (launch 38).
3. **`duplicate_chat` copied per-chat model configuration via a nonexistent `id` column** —
   `CompileError: Unconsumed column names: id` for any chat with per-chat model settings (launch 38).
4. **`duplicate_chat` minted a fresh `lineage_id` per message**, so the landed
   `messages_revision_consistency` trigger rejected any copied lineage at revision ≥ 2; full-branch-tree
   duplication of a regenerated chat could not run (launch 38).
5. **The duplicate-admission arm carried raw dataclass values** (`created_at` as a `datetime`,
   `lineage_id` possibly `None`) while the trigger compares serialised column values, so every duplicated
   assistant insert aborted as `message fields are invalid` (round 6).
6. **The copy omitted the ordinary `start` transition arm**, so `messages_active_head_parent_guard` aborted with
   `message cannot be inserted beneath the active head` (round 6).
7. **`validate_phase9_schema` was run against the frozen 0014 tombstone**, whose hardcoded trigger text
   necessarily lacks the 0015 guard the canonical DDL contains. The exact-DDL comparison can only hold at
   0012/0013/0015/0016; at 0014 it always failed. Row validation and presence checks still run there; only the
   head-vs-frozen equality is skipped (round 7).
8. **A migration-journal target set dropped `0015`** when the head moved to `0016`, so a journal targeting 0015
   would have been rejected (round 7).

Two further worker-introduced regressions were caught and repaired: a **deleted `chat_drafts` foreign key**
(removed because a sloppy test used a hardcoded chat id; the FK is correct and was restored with
`ON DELETE CASCADE`), and a **silently retargeted test** whose `PRIOR_HEAD` bump made it assert the opposite of
its own name.

## 4. Open finding A-2 — authority-grant inventory gap (tabled for the oracle)

The Phase 11 public store operations were never registered in `_SQLITE_OPERATION_METHODS`, and the milestone's
own operations split across two transition helpers with different authority semantics:

- `rename_chat` and `duplicate_chat` open `with self._authority.transition():` and therefore **succeed outside
  `command_admission()`**. Directly demonstrated: `rename_chat` outside admission returned without error and the
  title was actually changed to `'RENAMED-OUTSIDE'`.
- `save_chat_draft`, `save_dock_layout`, `save_keybinding_override`, `save_font_scale_settings` use
  `self.mutation_transition()` and **fail closed** with `AuthorityError: database resource has no forward grant`.

The existing guard `test_public_store_api_coverage_has_callee_grant_wrapper` only checks that *listed* methods
are wrapped; it never checks that the lists are complete. A set-difference over the public API finds 32 public
methods neither registered nor wrapped — including pre-existing Phase 9 archive-import operations, so the lists
are evidently a partial allow-list by design.

**This was deliberately not changed.** The correct direction is ambiguous (are rename/duplicate over-granted, or
are the four under-granted?), and guessing would move the authority boundary. This is exactly what accepted v5
notes **NEW-11** and **NEW-12** flag. It is tabled for the independent review and the final oracle.

## 4a. M3 RESOLVED IN ROUND 11 (superseding the schema-only correction)

Round 9 found and round 10 confirmed that M3 was **schema only**. That has been repaired. The M3 product path
now exists: nine store operations (folder create/rename/delete, `list_folders`, `set_chat_folder`,
`set_chat_pinned`, `describe_chat_deletion`, `delete_message` tombstone, `delete_chat`), all nine registered in
`_SQLITE_OPERATION_METHODS` so they carry the callee-owned grant; matching port and application commands with
events; desktop UI (folder grouping, pin markers, flat rail context menu, deletion confirmation dialog with a
loss inventory, tombstone rendering, palette actions); migration `0017_phase11_integrity` (INSERT-time pin guard
and `folder_id` referential enforcement, with a downgrade proven to restore 0016 exactly); and
`docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY_PHASE11_SUPPLEMENT.md`. The migration head is now
`0017_phase11_integrity`.

The implementer mutation-tested its own work with 22 mutations and **self-reported that three of its tests were
initially vacuous**, strengthening them rather than working around them. An independent verifier confirmed all
major claims and corrected one (test count 46, not 26).

**Residual coverage concern (recorded, not hidden).** `test_phase11_deletion_product_path.py::test_deletion_path_requires_the_0017_schema`
was semantically rewritten: with 0017 as the head the open path auto-migrates, so its former premise (a 0016
store refuses the tombstone) is unreachable through the product API. The consequence is that the **fail-closed
guarantee is now covered by no test**, although the guard itself is still implemented. That coverage should be
restored by a test that constructs a 0016 store directly, bypassing auto-migration.



### 4a-i. Historical record — how M3 was found to be schema-only (rounds 9–10)

Retained because the finding and its evidence remain the reason M3 was reopened. An earlier draft of this
report, and the round-8 progress log, described M3 as "landed". **That was wrong.** Independent falsification
(finding P11-03) and direct supervisor verification agreed:

    grep folder|pin|delete_chat  src/bots5/core/ports.py          -> no matches
    grep "async def .*folder|.*pin|delete_chat" src/bots5/core/application.py  -> no matches
    grep "def .*folder|set_chat_pinned" src/bots5/infrastructure/persistence/sqlite.py -> no matches
    ls src/bots5/desktop/ | grep delet                             -> no deletion dialog module

What actually exists is migration `0013_phase11_organisation` (which creates the `folders` table and adds
`chats.folder_id` / `chats.is_pinned`) and migration `0014_phase11_message_tombstone`, plus
`tests/test_phase11_folders_pins_deletion.py`. That test file drives **raw SQL through
`store.engine.begin()`** rather than any product API, because there is no product API to drive.

Consequences:
- Users cannot create folders, move a chat into a folder, pin/unpin a chat, or delete a message or a whole chat.
- The R-11 whole-chat deletion confirmation flow with its loss inventory does not exist.
- `MessageRow` renders `message.content` with no handling for the `DELETED` state, so even a tombstone row
  inserted directly would render as blank rather than as a tombstone.
- The design's required "authority/effect-inventory supplement (only if deletion lands)" is therefore also not
  written, and the M3 milestone cannot be called complete.

**M3 must be reopened.** The schema and the tests are real work and are retained, but the milestone's
user-facing requirements are unimplemented. This is the single largest remaining scope gap and it is not
something the remaining budget can be assumed to close.

## 5. M7 — STOPPED FOR MICK (new tool dependency)

Observed directly:

    PySide6 6.11.2   present (/usr/lib/python3.14/site-packages/PySide6)
    pyside6-deploy   NOT FOUND
    nuitka           NOT FOUND
    patchelf         NOT FOUND

The sealed design states that `pyside6-deploy` "ships with PySide6 and **auto pip-installs** `Nuitka==4.1.1` +
`patchelf` ... — two new dependencies plus a network build step."

Dependency authority is limited to `markdown-it-py` and `Pygments`, and any further package/tool dependency
requires stopping for Mick. Consequently `scripts/build/build_standalone_linux.sh`,
`scripts/ci/validate_standalone_build.sh` and the artifact-dependent assertions in
`tests/test_phase11_packaging.py` are **not written** — writing an unexecutable build script would be
unverified work. M8 stays deferred behind M7 and R-9.

## 6. Accepted v5 notes preserved as recorded limitations

These are the six v5 acceptance-time notes plus the observations. Repairing any of them would change the sealed
bytes and supersede seal v5. They are carried forward unchanged and are **not** silently fixed or dropped.

| Note | Severity | Content | Status here |
|---|---|---|---|
| NEW-9 | MINOR | candidate JSON `candidate_hashes_note` still says "seal v3 binds these exact candidate bytes"; the operative bind is seal v5 | recorded limitation, unchanged |
| NEW-10 | MINOR | the scope addition is not propagated to design §1/§3.2/§8 or the candidate's `feature_boundary`/`t0_new` fields (A-1 details live in §4.9, §9b and `a1_scope_extension`) | recorded limitation, unchanged |
| NEW-11 | MINOR | `_SQLITE_OPERATION_METHODS` is not named in the fence | **materialised** as finding A-2 (§4) |
| NEW-12 | MINOR | the A-1 reports' authority/effect-inventory supplement question (whether `duplicate_chat`, the first bulk row-copy, is a new effect class) is not carried into the design | **materialised** as finding A-2 (§4); no supplement written |
| NEW-13 | MINOR | seal v5 binds neither the V3/V4 oracle reports nor the adjudication/acceptance records | recorded limitation, unchanged |
| NEW-14 | MINOR | citation error: `transition_guard.py:30–33` should be `:35–38` | recorded limitation, unchanged |
| OBS | OBS | three further observations recorded in the oracle report | recorded, unchanged |

None of these is a correctness, authority or feasibility defect; each is documentation completeness or a
citation.

## 7. Governance deviations requiring Mick before the final oracle

1. **Amendment B adds a migration.** Sealed v5 states "A-1 adds no migration". The implemented candidate adds
   `0015_phase11_duplicate_admission` (a duplication admission guard), which Mick explicitly authorised as
   option B. The sealed design therefore no longer describes the product exactly.
2. **The workspace revision is renumbered.** Sealed v5 names the workspace-state revision
   `..._0015_phase11_workspace_state.py`. The `0015` slot was consumed by (1), so the implemented revision is
   `0016_phase11_workspace_state`.

Both are direct consequences of an authorised decision and neither is a correctness defect, but together they
mean the candidate deviates from the sealed bytes. This requires either a **v6 reseal** or an **explicit
recorded Mick acceptance**, and it must be resolved before a final oracle can be meaningful.

## 8. Other recorded limitations (not defects, not silently dropped)

- **Active-path duplication of a lineage-truncated chat** (after a regenerate/edit branch) still fails the
  `messages_revision_consistency` trigger, because the superseded predecessor is not in the active path. The
  full-branch-tree path works. Resolving this is a product-semantics decision (copy whole lineages vs refuse)
  and is recorded rather than guessed.
- **`dock_layout` carries no foreign key** deliberately: the design calls it an advisory, Qt-version-tied opaque
  blob, and a malformed or incompatible blob falls back to the default layout rather than raising.
- **Recents/favourites are session-only**, per fork R-6 as adjudicated for Phase 11; no persistence was added.
- **M4b draft semantics are hybrid**: a stored draft replaces the composer on reopen, while the Draft-1
  behaviour of preserving in-flight composer text across a chat switch is retained.

## 9. Pre-commit boundary

No staging, commit, push, ref mutation, OrgMem mutation, Phase 12 work or scope expansion has been performed.
The working tree is the candidate. The authoritative CI v1 T4 run has **not** been dispatched, because it
requires the exact final sealed candidate, which cannot exist until §7 is resolved and M7 is decided.
