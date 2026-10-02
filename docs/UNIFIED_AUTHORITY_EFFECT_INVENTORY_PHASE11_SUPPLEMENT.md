# Unified authority/effect participation inventory — Phase 11 M3 supplement

Status: **additive successor supplement** to the landed Phase 6 authority inventory and the landed
Phase 7–10 supplement. It records the participation delta introduced by the Phase 11 M3 milestone
(organisation: folders, pins, archive/palette integration, and R-11 deletion UX). It is not a new
audit and does not supersede either earlier record.

## 1. Relationship to the earlier records

`docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md` (Phase 6) and
`docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY_PHASE7_10_SUPPLEMENT.md` remain authoritative for their
scopes. This supplement records **only the M3 delta** and states, for each new surface, whether it
joins the existing `DataRootAuthority` / grant / transition system or lies outside it. Nothing here
re-opens a prior closure review.

## 2. New public surfaces

- **Store operations** (`SQLiteAppStateStore`): `create_folder`, `rename_folder`, `delete_folder`,
  `list_folders`, `set_chat_folder`, `set_chat_pinned`, `describe_chat_deletion`, `delete_message`,
  `delete_chat`. **Every one of them is a member of `_SQLITE_OPERATION_METHODS`**, so each acquires or
  joins the existing callee-owned logical grant (E06). This closes the recorded A-2 defect class for
  the new surface: no public mutating store method is introduced outside the decorated family.
- **Application commands** (`BotsApplication`): `create_folder`, `rename_folder`, `delete_folder`,
  `list_folders`, `set_chat_folder`, `set_chat_pinned`, `describe_chat_deletion`, `delete_message`,
  `delete_chat`. All are `@_tracked_command`s and therefore enter the existing E01 command admission;
  there is no second admission path. Each publishes exactly one event on the existing bound EventBus
  (E04): `folder_created`, `folder_renamed`, `folder_deleted`, `chat_folder_changed`,
  `chat_pin_changed`, `message_deleted`, `chat_deleted`.

## 3. Effect classification per surface

- **Folder/pin operations (F4/F5)** are metadata mutations inside the existing logical grant and one
  engine transaction. `folder_id` and `is_pinned` are deliberately outside the Phase 7
  `phase7_chat_update_source` trigger column list, so a folder move or pin toggle consumes **no**
  search-source revision and owes **no** receipt; the ordering contract (pins float) is satisfied by
  the read path. Neither the chat revision nor `updated_at` moves (mirror of the landed rename
  semantics), so no lifecycle trigger is involved.
- **Message tombstone (F7, lossless per-message deletion)** is a settled-message `state='deleted',
  content=''` update. That single shape is admitted by the 0017 trigger set; every other
  content/state change for a settled message is still refused exactly as before. The update is a
  Phase 7 search-visible source mutation, so it arms and consumes exactly one source mutation inside
  the authoritative transaction and accepts one receipt for `{chat, message}` — the same discipline
  the landed `archive_chat`/`rename_chat` surfaces use. The receipt drain removes the message's
  search document (a tombstone is no longer a terminal searchable state), so deleted content leaves
  the derived index.
- **Whole-chat deletion (F7, physically destructive)** is the design's "ordinary physically
  destructive chat operation". It runs one authoritative transaction under the existing transition
  gate, arms and consumes exactly one Phase 7 source mutation for the full removed document set
  (`chat` plus every removed `message`), and accepts one receipt before returning. The deletion needs
  to remove message/attempt/link/context-plan rows that frozen immutability triggers protect, so the
  0017 revision admits those deletes behind a **connection-local chat-deletion arm**
  (`arm_phase11_chat_deletion` in `transition_guard.py`): the arm names the ONE chat whose rows may
  be removed, is cleared with the transaction, and without it every one of those deletes aborts with
  its original message. This is the same arm-function pattern the sealed 0015 duplicate-admission
  revision introduced; it creates no raw-DML escape hatch.
- **Deletion refusals** stay inside the existing typed error boundary: unknown chat, stale CAS
  revision, a running generation (streaming message or running attempt), and archive import
  provenance (the RESTRICT foreign keys protect provenance; the store surfaces this as a typed
  `StateError`, never as a raw DB error).

## 4. Migration 0017 participation

`0017_phase11_integrity` participates in the existing all-revision migration sequence under the
startup grant (E15/E19). Its scope:

- **F5 repair** — adds the INSERT-time pin range guard (`phase11_chat_pin_insert_guard`) that the
  frozen 0013 UPDATE-only trigger lacked.
- **F4 repair** — adds INSERT/UPDATE folder-reference guards so `chats.folder_id` is referentially
  enforced against `folders`.
- **F7 enablement** — recreates the frozen message/attempt/link/context-plan delete guards and the
  message update guards behind, respectively, the chat-deletion arm and the exact tombstone shape.

The **default upgrade target deliberately remains `0016_phase11_workspace_state`**: several existing
tests assert that a store open lands exactly on 0016, so M3 must not silently move the product head.
0017 is instead a **supported terminal revision** — `upgrade_database` accepts a database already at
0017 and opens it without journalling, and the store validates it like any other Phase 9-or-later
revision. Databases only reach 0017 by an explicit upgrade, and `0017.downgrade()` restores the 0016
schema definition-for-definition (proven byte-identically in the M3 tests). Flipping the default
head is a one-line change to be made together with the head-pinning tests' owners.

The exact-DDL validators are extended, not weakened: `validate_phase6_schema` accepts the three
recreated phase 6 delete guards at revision 0017 only when the installed SQL carries the
chat-deletion arm token — the same revision-gated token mechanism 0012 introduced for the archive
import world. The triggers keep their names, tables, timing and abort messages.

## 5. Desktop participation

The rail folder area, pin markers, context-menu entries, move-to-folder dialog, deletion
confirmation dialog (which renders the store-computed `ChatDeletionInventory` BEFORE the destructive
command), the tombstone transcript row, and the palette `chat.pin`/`chat.delete` actions are thin
client over the new core commands. The window keeps no product semantics: every state change is one
admitted command plus one event, and the dialogs never touch state. No new desktop-side admission,
grant, or invalidation path exists.

## 6. Is bulk row-copy (duplicate_chat) a new effect class?

**Judgement: no.** `duplicate_chat` (landed with 0015) copies N message rows plus auxiliaries inside
ONE authoritative transaction that arms/consumes exactly one Phase 7 source mutation and accepts one
receipt for the whole produced document set. Effect classes in the Phase 6 inventory are
classifications of *authority participation shapes* (how admission, transition, receipt and
failure-classification obligations are met), not of row counts. A multi-row insert that satisfies
the E06 callee grant, the transition gate, and the E01/E04 event discipline is a larger *instance*
of the existing "authoritative search-visible write" effect, not a new class. What 0015 added was a
new **admission arm** (duplicate admission), i.e. a new participant inside the existing effect
system — the same judgement this supplement applies to the 0017 chat-deletion arm. For M3 the same
reasoning covers `delete_chat`: it removes more rows than a single-row mutation, but it joins the
same grant, the same transition gate, the same one-receipt discipline, and the same fail-closed
error classes.

## 7. Scope and conclusion

- M3 introduces **no second data-root admission or invalidation coordinator** and no new effect
  class. It adds: nine members of the existing decorated store-operation family, nine tracked
  application commands, one connection-local deletion admission arm, and one supported terminal
  migration revision — all participants in the existing system.
- This supplement records deltas only; the Phase 6 and Phase 7–10 dispositions stand by reference.
- Future authority-boundary changes must read all three records together.
