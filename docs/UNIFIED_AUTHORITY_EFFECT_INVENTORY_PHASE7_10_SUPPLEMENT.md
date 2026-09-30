# Unified authority/effect participation inventory — Phase 7–10 supplement

Status: **additive successor supplement** to the landed Phase 6 authority inventory. It records the
participation deltas introduced by landed Phases 7 through 10. It is not a new audit and does not
supersede the Phase 6 record.

## 1. Relationship to the Phase 6 inventory

`docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md` is the **landed Phase 6 authority inventory** for commit
`20847c7a49e26679d0d3dfe99798a2c211bec436`. That historical file remains authoritative for its Phase 6
closure scope, including its invalidation-route, effect-class and obligation dispositions and its
release-critical mutant sensitivity table. It is not obsolete, is not superseded, and is not rewritten
here.

This supplement records **participation deltas** landed after Phase 6 — the public surfaces, durable
lifecycles and pre-READY effects added by Phases 7, 8, 9 (Slices A–E) and 10 — and states, for each,
whether it joins the existing `DataRootAuthority` / grant / transition system or lies outside it.

Preparing this supplement **does not** constitute a fresh exhaustive re-verification of all Phase 6
sibling paths or obligations. The Phase 6 dispositions stand by reference; nothing in this document
re-opens the Phase 6 closure review or re-proves its finite sibling-path result.

The Phase 6 file carries a section titled "Uncommitted Phase 7 participation delta". That described
the Phase 7 implementation candidate at a time when it was not yet committed. The Phase 7 work
subsequently landed at `6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`. The historical Phase 6 file is
deliberately left byte-identical; its "Uncommitted" label is superseded by this record rather than by
editing history.

## 2. Phase 7 — search and exact navigation (`6ccdaf0`)

The substance of the Phase 6 delta is preserved below and is now landed rather than uncommitted:

- Public application search status/query/rebuild/diagnostic/navigation and archive/unarchive surfaces
  enter the existing tracked command admission. There is no separate admission path for them.
- The corresponding public SQLite store methods are members of `_SQLITE_OPERATION_METHODS` and
  therefore acquire or join the existing callee-owned logical grant. `search`, `search_status`,
  `diagnose_search_index`, `rebuild_search_index`, `resolve_search_result` and `archive_chat` are
  members of that set.
- Authoritative search-visible writes execute under the existing transition gate. Their business
  mutation and one `search_source_state.source_revision` increment commit in the same SQLite
  transaction; the transition guard reads `search_source_state.source_revision` under that gate.
- Receipt draining and rebuild are forward derived effects under the existing transition gate. Rebuild
  is awaited, writer-serialized, and has no detached task, destructor, or garbage-collection
  correctness dependency.
- Clean derived logical failure leaves authoritative business success intact. Unknown commit,
  rollback, physical close, native, or verified-attachment outcomes continue through the Phase 6
  fail-closed paths.
- Revoked grants cannot start or continue forward indexing. Invalidated teardown remains release-only
  and cannot open a fresh database session.
- Search-result navigation is a bounded read projection. Historical navigation uses the selected
  message as a temporary presentation leaf and never mutates `chats.head_message_id`.

Phase 7 introduced no second **data-root** admission or invalidation coordinator; these surfaces
continue to use the existing command-admission, grant and transition machinery. That statement is
scoped to the data-root authority domain only and does not deny other kinds of authority semantics
elsewhere in the repository.

## 3. Phase 8 — inspection and provenance UX (`f72e0ea`)

- **Read-only projection.** Phase 8 delivered a read-only Details/Inspector surface over authoritative
  persisted state. The typed `InspectionProjection`/`InspectionField` model lives in core
  (`core/inspection.py`); Qt presents the projection (`desktop/widgets.py`) and does not interpret raw
  versioned request snapshots.
- **Migration/startup effect.** The additive migration `0011_phase8_inspector_state` participates in the
  existing all-revision migration sequence, which Phase 6 already owns under the startup grant
  (dispositions E15/E19). It introduces no new admission or invalidation route.
- **Participation.** Read projections participate only in the existing read and command-admission
  classes. Phase 8 adds no new authority class, no new invalidation route and no new effect class, and
  no new data-root coordinator.

## 4. Phase 9 — interchange, import, backup, restore and desktop integration

### 4.1 Slice A — Transcript v0.1 and Archive v1 export (`35b206a4`)

- Export is a read projection over the active path or the full lineage.
- Archive v1 export **refuses a chat with a running generation** (`core/export.py`) rather than
  claiming a settled full-fidelity snapshot of unsettled chat truth.
- Participation is limited to existing read boundaries; export mints no forward authority and adds no
  admission, invalidation or effect class.

### 4.2 Slice B — validated archive import and durable provenance (`9a84d38b`)

- The additive migration `0012_phase9_archive_import` participates in the existing all-revision
  migration sequence under the startup grant (E15/E19).
- Import introduces a persistent import-queue/recovery state machine
  (`core/import_queue.py`, `infrastructure/persistence/archive_import_store.py`) with explicit queue
  states and revision-guarded CAS/reorder/remove/cancel operations.
- Import settlement is **authority-owned**: application shutdown waits for cutoff-past import
  settlements while they remain authority-owned rather than abandoning them.
- Authoritative import writes and their source-revision/search interaction execute through the existing
  transition gate and command admission, not through a new coordinator.
- Archive v2 preserves import/continuation provenance across later export/import hops without
  resurrecting providers.

These are additional participants in the existing system plus one new durable queue/recovery
lifecycle. No new data-root admission or invalidation coordinator is introduced.

### 4.3 Slice C — Backup v1 and independent verification (`81a818a3`)

- Whole-installation capture executes **under the data-root authority fence**:
  `infrastructure/backup_capture.py` holds `DataRootAuthority.operation()` together with the store
  mutation transition while capturing one authority-owned coherent recovery cut.
- Capture stages and re-verifies the package before publication, with a typed publication-uncertainty
  outcome (which is not rewritten as success).
- Migration recovery points are produced through the same authority-fenced capture path.
- Independent artifact-only verification (`domain/backup.py`, `core/backup.py`,
  `infrastructure/backup_package.py`) checks a closed-manifest package without altering live state.

This is a genuine **new participant** in the existing authority system — authority-fenced
whole-installation capture plus a publication-uncertainty outcome — and not a new authority.

### 4.4 Slice D — staged restart restore and destructive failure semantics (`7c2fe400`)

- Whole-installation restore is a durable journal state machine (`infrastructure/restore_service.py`):
  validate, preserve, stage, verify, an fsynced adoption barrier, and a single atomic leaf exchange.
- **Startup reconciliation of interrupted restores runs before ordinary store admission**:
  `bootstrap/desktop.py` invokes the `RestoreStartupCoordinator` with the authority before the store is
  opened, so an interrupted restore journal is reconciled before any ordinary store can admit work.
- The displaced installation is retained indefinitely under operator direction; the non-UI
  `bots5-desktop --restore-from` entry point reports typed outcomes.
- Fail-closed behaviour is preserved under authority or identity uncertainty.

This participates in the pre-READY startup class the Phase 6 inventory already names (E19
"acquisition/migration/**restore**"; E15 startup/reconciliation). It adds a durable restore journal and
a retained-installation lifecycle, but no new admission or invalidation coordinator.

### 4.5 Slice E — desktop integration and Phase 9 closure (`0756904481`)

- Thin-client desktop surfaces, controller orchestration and bootstrap handoff over the landed Slice
  A–D capabilities.
- `DataRootAuthority` and the default-off destructive override are **unchanged**. Slice E adds no
  `BotsApplication.restore_*` command and never calls restore inside the live session; restore remains
  a pre-store bootstrap operation reached through the Slice D path.
- Participation is limited to calling the existing core commands, which therefore enter the existing
  command admission. No new authority participant is added.

## 5. Phase 10 — campaign desktop surface and evidence v2 (`9762170`)

Phase 10 requires a narrow and precise statement.

- Phase 10 did **not** introduce a second **data-root** admission or invalidation coordinator competing
  with `DataRootAuthority`. This is verified directly against landed source: `core/campaign.py` and
  `desktop/campaign_dock.py` contain no `DataRootAuthority`, grant or transition references at all.
- `CampaignBridge`, campaign evidence-v2 approval, one-shot approval consumption, attempt selection,
  regeneration and synthesis-rerun authorization, and the derived-selected cost authority are a
  **separate campaign-execution domain**. Their "authority" is evidence-selection and approval
  semantics over a run directory. That is not the data-root grant protocol.
- Those semantics must not be described as `DataRootAuthority` participation, and must not be described
  as a replacement for, or a second, data-root authority.
- Phase 10 adds no migration and does not change the desktop data-root authority boundary.

It would be inaccurate to summarise the above as a broad claim that "no new authority system exists".
The verified conclusion is the narrower one: no second **data-root** admission/invalidation coordinator
was introduced, and the campaign evidence/approval domain is separate from `DataRootAuthority`.

## 6. Scope and conclusion

- The landed Phase 6 inventory (`docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md`) together with this
  Phase 7–10 supplement provide the current documented authority/effect participation-delta record
  through Phase 10.
- This supplement records **deltas**. It is not a new proof that all historical Phase 6 obligations
  were re-audited from scratch, and it does not disturb the Phase 6 closure disposition.
- Future authority-boundary changes must inspect **both** records — the Phase 6 inventory and this
  supplement — together with the relevant later phase implementation and closure evidence.
