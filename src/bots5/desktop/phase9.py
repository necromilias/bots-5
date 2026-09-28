"""Phase 9 desktop integration: task orchestration and thread-safe publication.

Ownership rule (SLICE_E_DESIGN.md §2): widgets are selection + presentation;
:class:`Phase9DesktopController` is task orchestration + thread-safe
publication; :class:`~bots5.core.application.BotsApplication` is the semantic
command surface; core/infrastructure own the product semantics and filesystem
effects.  No product semantics live in this module.

Invariants I4/I6: the controller awaits every application command on the
qasync loop (each landed command already offloads its blocking work
internally), so nothing here blocks the loop either.  Typed outcomes
(``StateError``, ``ArchiveVersionRequired``, ``ArchiveImportStoreError``,
``RevisionConflict`` and the landed backup refusals, including
``BackupUncertainPublication``) are surfaced honestly; refusal is never
collapsed into success.  Queue commands never auto-retry: a stale token
produces a "queue changed — refresh" prompt.  Backup progress crosses from
the landed worker thread only through :class:`Phase9ProgressBridge`'s queued
Qt signal; widgets are never touched from a worker thread.  Backup creation
drives the landed backup service through the landed fresh-context offload
pattern under the application command admission scope (see
``run_backup_creation`` for the landed-shape rationale).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from bots5.core.errors import (
    BackupResolutionCancelled,
    BackupUncertainPublication,
    StateError,
)
from bots5.core.export import ArchiveVersionRequired

from .widgets import continuation_readiness_needs_resolution


# The landed store/app surface stale CAS preconditions as errors whose message
# contains "revision conflict": the row token conflicts ("queue revision
# conflict") and the reorder page-token refusal ("queue reorder revision
# conflict" — deliberately not RevisionConflict, oracle R-6).
_STALE_TOKEN_MARKERS = ("revision conflict",)


def _is_stale_queue_token(error: BaseException) -> bool:
    message = str(error)
    return any(marker in message for marker in _STALE_TOKEN_MARKERS)


class Phase9ProgressBridge(QObject):
    """The only sanctioned worker-thread → GUI-thread publication channel.

    Worker threads never touch Qt widgets (master §6); they call the
    thread-safe publish methods, which only emit Qt signals.  Qt delivers an
    emit from a non-GUI thread to a GUI-thread receiver through a queued
    connection, so the receiving slot may safely mutate widgets.

    ``progress_updated`` carries ``(state, operation_id)`` and is the seam M4
    will use for backup progress; ``completed``/``failed`` carry one object
    each for bounded single-shot publication outcomes.
    """

    progress_updated = Signal(object, object)
    completed = Signal(object)
    failed = Signal(object)

    def publish_progress(self, state: object, operation_id: object) -> None:
        """Thread-safe: Qt signal emission may be made from any thread."""

        self.progress_updated.emit(state, operation_id)

    def callback(self, progress: object) -> None:
        """``create_backup`` progress_callback shape; invoked on the worker thread."""

        self.publish_progress(
            getattr(progress, "state", progress),
            getattr(progress, "operation_id", None),
        )

    def publish_completed(self, result: object) -> None:
        self.completed.emit(result)

    def publish_failed(self, error: object) -> None:
        self.failed.emit(error)


class Phase9DesktopController:
    """Awaits application commands on behalf of the Phase 9 UI and publishes
    results back to widgets on the GUI thread (the qasync loop thread)."""

    def __init__(
        self,
        application,
        *,
        parent: QObject | None = None,
        bridge: Phase9ProgressBridge | None = None,
        notify=None,
    ) -> None:
        self._application = application
        self.bridge = (
            bridge if bridge is not None else Phase9ProgressBridge(parent)
        )
        self._notify = notify
        self._tasks: set[asyncio.Task[None]] = set()
        self._dialogs: dict[str, object] = {}
        self._queue_dock = None
        self._refresh_generation = 0
        self._closed = False

    # ------------------------------------------------------------------
    # Task orchestration
    # ------------------------------------------------------------------

    def schedule(self, coroutine) -> asyncio.Task[None] | None:
        if self._closed:
            coroutine.close()
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Widget visibility can change outside a running loop (a bare
            # Qt-only context); the reload simply does not happen there.
            coroutine.close()
            return None
        task = loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Test/teardown seam: wait for every scheduled controller task."""

        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        """Detach from the window shutdown path; idempotent and safe."""

        self._closed = True
        for task in tuple(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        dock = self._queue_dock
        if dock is not None:
            dock.mark_closed()
        self._dialogs.clear()

    def _notify_status(self, message: str) -> None:
        if self._notify is not None:
            self._notify(message)

    def _forget_dialog(self, key: str, dialog: object) -> None:
        if self._dialogs.get(key) is dialog:
            self._dialogs.pop(key, None)

    # ------------------------------------------------------------------
    # Workflow 1 — transcript export
    # ------------------------------------------------------------------

    def open_transcript_export(self, parent, chat_id: str | None) -> None:
        if chat_id is None:
            self._notify_status("Select a chat before exporting its transcript")
            return
        existing = self._dialogs.get("transcript_export")
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        dialog = self._build_dialog("transcript_export", parent, chat_id)
        if dialog is None:
            return
        dialog.confirm_requested.connect(
            lambda destination, scope, chat_id=chat_id, dialog=dialog: self.schedule(
                self.run_transcript_export(
                    chat_id, destination, scope, dialog=dialog
                )
            )
        )
        dialog.show()

    def _build_dialog(self, key: str, parent, chat_id: str | None):
        from .phase9_dialogs import TranscriptExportDialog

        dialog = TranscriptExportDialog(parent)
        dialog.finished.connect(
            lambda _result, key=key, dialog=dialog: self._forget_dialog(key, dialog)
        )
        self._dialogs[key] = dialog
        return dialog

    async def run_transcript_export(
        self, chat_id: str, destination, scope, *, dialog=None
    ) -> None:
        try:
            written = await self._application.write_transcript_export(
                chat_id, destination, scope=scope
            )
        except Exception as exc:
            if dialog is not None:
                dialog.submission_failed(str(exc))
            else:
                self._notify_status(f"Transcript export failed: {exc}")
            return
        if dialog is not None:
            dialog.submission_succeeded(written)
        self._notify_status(f"Transcript exported to {written}")

    # ------------------------------------------------------------------
    # Workflow 2 — archive export
    # ------------------------------------------------------------------

    def open_archive_export(self, parent, chat_id: str | None) -> None:
        if chat_id is None:
            self._notify_status("Select a chat before exporting its archive")
            return
        existing = self._dialogs.get("archive_export")
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from .phase9_dialogs import ArchiveExportDialog

        dialog = ArchiveExportDialog(parent)
        dialog.finished.connect(
            lambda _result, dialog=dialog: self._forget_dialog("archive_export", dialog)
        )
        dialog.confirm_requested.connect(
            lambda destination, policy, version, chat_id=chat_id, dialog=dialog: self.schedule(
                self.run_archive_export(
                    chat_id,
                    destination,
                    attachment_policy=policy,
                    archive_version=version,
                    dialog=dialog,
                )
            )
        )
        self._dialogs["archive_export"] = dialog
        dialog.show()

    async def run_archive_export(
        self,
        chat_id: str,
        destination,
        *,
        attachment_policy,
        archive_version,
        dialog=None,
    ) -> None:
        try:
            result = await self._application.write_archive_export(
                chat_id,
                destination,
                attachment_policy=attachment_policy,
                archive_version=archive_version,
            )
        except ArchiveVersionRequired as exc:
            if dialog is None or exc.required != 2 or not dialog.confirm_switch_to_v2():
                if dialog is not None:
                    dialog.submission_failed(str(exc))
                else:
                    self._notify_status(f"Archive export refused: {exc}")
                return
            # The operator explicitly consented to v2 — an operator choice,
            # never an automatic retry.
            await self.run_archive_export(
                chat_id,
                destination,
                attachment_policy=attachment_policy,
                archive_version=2,
                dialog=dialog,
            )
            return
        except Exception as exc:
            if dialog is not None:
                dialog.submission_failed(str(exc))
            else:
                self._notify_status(f"Archive export failed: {exc}")
            return
        if dialog is not None:
            dialog.submission_succeeded(result)
        self._notify_status(
            f"Archive exported: {result.archive_id} ({result.entry_count} entries)"
        )

    # ------------------------------------------------------------------
    # Workflow 3 — archive import admission
    # ------------------------------------------------------------------

    def open_archive_import(self, parent) -> None:
        existing = self._dialogs.get("archive_import")
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from .phase9_dialogs import ArchiveImportAdmissionDialog

        dialog = ArchiveImportAdmissionDialog(parent)
        dialog.finished.connect(
            lambda _result, dialog=dialog: self._forget_dialog("archive_import", dialog)
        )
        dialog.admit_requested.connect(
            lambda source, roots, as_archived, dialog=dialog: self.schedule(
                self.run_archive_import(
                    source, roots, as_archived, dialog=dialog
                )
            )
        )
        self._dialogs["archive_import"] = dialog
        dialog.show()

    async def run_archive_import(
        self, source, resolver_roots, import_as_archived: bool, *, dialog=None
    ) -> None:
        try:
            queued = await self._application.enqueue_archive_import(
                source,
                resolver_roots=tuple(resolver_roots),
                import_as_archived=import_as_archived,
            )
        except Exception as exc:
            if dialog is not None:
                dialog.submission_failed(str(exc))
            else:
                self._notify_status(f"Archive import refused: {exc}")
            return
        if dialog is not None:
            dialog.submission_succeeded(queued)
        self._notify_status(
            f"Archive import queued: {queued.state.value}"
        )
        await self._post_queue_command()

    # ------------------------------------------------------------------
    # Workflow 4 — queue visibility and controls
    # ------------------------------------------------------------------

    def attach_queue_dock(self, dock) -> None:
        """Wire the dock's control signals to the landed application commands."""

        self._queue_dock = dock
        dock.set_controller(self)
        dock.cancel_requested.connect(
            lambda queue_id, revision: self.schedule(
                self.run_cancel(queue_id, revision)
            )
        )
        dock.remove_requested.connect(
            lambda queue_id, revision: self.schedule(
                self.run_remove(queue_id, revision)
            )
        )
        dock.retry_requested.connect(
            lambda queue_id: self.schedule(self.run_retry(queue_id))
        )
        dock.reorder_requested.connect(
            lambda queue_revision, ordered_ids: self.schedule(
                self.run_reorder(queue_revision, tuple(ordered_ids))
            )
        )
        dock.clear_history_requested.connect(
            lambda ids: self.schedule(self.run_clear_history(tuple(ids)))
        )

    def schedule_refresh(self) -> None:
        if self._closed:
            return
        self.schedule(self.refresh_queue())

    async def refresh_queue(self) -> None:
        dock = self._queue_dock
        if dock is None or self._closed:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            page = await self._application.queued_import_display(limit=50)
        except Exception as exc:
            if generation == self._refresh_generation and not self._closed:
                dock.show_notice(f"Queue refresh failed: {exc}")
            return
        if (
            generation == self._refresh_generation
            and self._queue_dock is dock
            and not self._closed
        ):
            dock.apply_page(page)

    async def run_cancel(self, queue_id: str, expected_revision: int) -> None:
        try:
            await self._application.cancel_archive_import(
                queue_id, expected_revision=expected_revision
            )
        except Exception as exc:
            self._queue_command_failed(exc)
            return
        await self._post_queue_command()

    async def run_remove(self, queue_id: str, expected_revision: int) -> None:
        try:
            await self._application.remove_waiting_archive_import(
                queue_id, expected_revision=expected_revision
            )
        except Exception as exc:
            self._queue_command_failed(exc)
            return
        await self._post_queue_command()

    async def run_retry(self, queue_id: str) -> None:
        try:
            await self._application.retry_archive_import(queue_id)
        except Exception as exc:
            self._queue_command_failed(exc)
            return
        await self._post_queue_command()

    async def run_reorder(
        self, expected_queue_revision: int, ordered_ids: tuple[str, ...]
    ) -> None:
        try:
            await self._application.reorder_archive_imports(
                expected_queue_revision, tuple(ordered_ids)
            )
        except Exception as exc:
            self._queue_command_failed(exc)
            return
        await self._post_queue_command()

    async def run_clear_history(self, ids: tuple[str, ...]) -> None:
        try:
            await self._application.clear_archive_import_history(tuple(ids))
        except Exception as exc:
            self._queue_command_failed(exc)
            return
        await self._post_queue_command()

    async def _post_queue_command(self) -> None:
        """Immediate reload after every locally issued queue command (D-3)."""

        dock = self._queue_dock
        if dock is not None and not self._closed:
            dock.reload_now()

    def _queue_command_failed(self, error: BaseException) -> None:
        dock = self._queue_dock
        if dock is None:
            self._notify_status(f"Import queue command failed: {error}")
            return
        if _is_stale_queue_token(error):
            # Stale row revision / queue revision: a truthful refresh prompt,
            # never a silent retry (no auto-retry invariant).
            dock.show_refresh_prompt(str(error))
        else:
            dock.show_notice(str(error))

    # ------------------------------------------------------------------
    # Workflow 5 — imported-continuation resolution (send/regenerate only)
    # ------------------------------------------------------------------

    _CONTINUATION_DIALOG_KEY = "continuation_resolution"

    async def ensure_imported_continuation_ready(
        self, parent, chat_id: str, message_id: str | None
    ) -> bool:
        """Workflow 5 intercept for the send and regenerate paths only.

        Consults the landed readiness with the same base key the core gate
        uses — the authoritative chat head for a send (``application.py:1951``)
        or the regenerated assistant message for a regenerate
        (``application.py:2211``) — and, only when the mirrored gate predicate
        says the continuation is not usable, opens the resolution dialog
        instead of letting the landed
        ``StateError("imported continuation requires a ready explicit choice")``
        surface.  Returns ``True`` when the caller may proceed (readiness
        usable without a prompt, or an explicit choice was just admitted);
        ``False`` when the operator cancelled, which leaves the chat exactly
        as it was.  The Edit path never calls this; the core gate remains.
        """

        if self._closed:
            return False
        try:
            if message_id is None:
                chat, _messages = await self._application.open_chat(chat_id)
                base_key = chat.head_message_id
            else:
                base_key = message_id
            readiness = (
                None
                if base_key is None
                else await self._application.import_continuation_readiness(
                    chat_id, base_key
                )
            )
        except Exception as exc:
            self._notify_status(f"Continuation readiness failed: {exc}")
            return False
        if not continuation_readiness_needs_resolution(readiness):
            return True
        return await self._resolve_imported_continuation(parent, chat_id, readiness)

    async def _resolve_imported_continuation(
        self, parent, chat_id: str, readiness
    ) -> bool:
        from .phase9_dialogs import ContinuationResolutionDialog

        existing = self._dialogs.get(self._CONTINUATION_DIALOG_KEY)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return False
        try:
            # Only currently-active providers are selectable; import never
            # resurrects, enables or reconfigures a provider (I5).
            connections = tuple(
                connection
                for connection in await self._application.list_provider_connections()
                if getattr(connection, "available", False)
            )
            models = tuple(await self._application.list_model_catalogue())
            resolved_settings = await self._application.resolve_chat_generation_settings(
                chat_id
            )
            credential_statuses: dict[str, str] = {}
            for connection in connections:
                try:
                    status = await self._application.provider_credential_status(
                        connection.id
                    )
                except Exception as exc:
                    credential_statuses[connection.id] = f"unavailable: {exc}"
                else:
                    value = None if status is None else getattr(status, "status", None)
                    credential_statuses[connection.id] = (
                        str(value) if value is not None else "unknown"
                    )
        except Exception as exc:
            self._notify_status(f"Continuation resolution is unavailable: {exc}")
            return False

        dialog = ContinuationResolutionDialog(
            parent,
            readiness=readiness,
            connections=connections,
            models=models,
            credential_statuses=credential_statuses,
            resolved_settings=resolved_settings,
        )
        key = self._CONTINUATION_DIALOG_KEY
        loop = asyncio.get_running_loop()
        outcome: asyncio.Future[bool] = loop.create_future()
        banner = getattr(parent, "continuation_banner", None)

        def on_commit(
            connection_id, model_entry_id, explicit_settings, excluded_refs
        ) -> None:
            self.schedule(
                self._commit_imported_continuation(
                    chat_id,
                    readiness,
                    connection_id,
                    model_entry_id,
                    dict(explicit_settings),
                    tuple(excluded_refs),
                    dialog=dialog,
                    outcome=outcome,
                )
            )

        def on_finished(_result, dialog=dialog) -> None:
            # The pending-resolution presentation ends with the dialog itself,
            # synchronously with the close (admitted or cancelled).
            self._forget_dialog(key, dialog)
            if banner is not None:
                banner.clear_for(chat_id)
            if not outcome.done():
                outcome.set_result(False)

        dialog.commit_requested.connect(on_commit)
        dialog.finished.connect(on_finished)
        self._dialogs[key] = dialog
        if banner is not None:
            banner.show_for(
                chat_id,
                "Imported continuation needs an explicit local choice — "
                "historical messages are unchanged.",
            )
        try:
            dialog.show()
            return await outcome
        finally:
            if banner is not None:
                banner.clear_for(chat_id)
            if dialog.isVisible():
                dialog.close()

    async def _commit_imported_continuation(
        self,
        chat_id: str,
        readiness,
        connection_id: str,
        model_entry_id: str,
        explicit_settings: dict,
        excluded_refs: tuple,
        *,
        dialog,
        outcome: asyncio.Future,
    ) -> None:
        """Commit with the landed command only; historical rows are untouched."""

        try:
            await self._application.choose_import_continuation(
                chat_id,
                readiness.base_key,
                expected_choice_revision=readiness.choice_revision,
                connection_id=connection_id,
                model_entry_id=model_entry_id,
                explicit_settings=explicit_settings,
                excluded_refs=excluded_refs,
            )
        except Exception as exc:
            # Landed typed refusal, surfaced verbatim; the dialog stays open.
            dialog.submission_failed(str(exc))
            return
        if not outcome.done():
            outcome.set_result(True)
        dialog.submission_succeeded()

    # ------------------------------------------------------------------
    # Workflow 6 — backup v1 creation
    # ------------------------------------------------------------------

    def open_backup_creation(self, parent) -> None:
        existing = self._dialogs.get("backup_creation")
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from .phase9_dialogs import BackupCreationDialog

        dialog = BackupCreationDialog(parent)
        dialog.finished.connect(
            lambda _result, dialog=dialog: self._forget_dialog(
                "backup_creation", dialog
            )
        )
        dialog.confirm_requested.connect(
            lambda destination, overwrite, dialog=dialog: self.schedule(
                self.run_backup_creation(destination, overwrite, dialog=dialog)
            )
        )
        self._dialogs["backup_creation"] = dialog
        dialog.show()

    async def run_backup_creation(
        self, destination, overwrite: bool, *, dialog=None
    ) -> None:
        """Await the landed backup creation and publish truthfully.

        The blocking creation runs off the qasync loop on a worker thread;
        progress crosses back only through the bridge's queued Qt signal and
        this coroutine resumes on the qasync (GUI) thread, so widgets are
        only ever mutated from GUI-thread slots.  ``cancellation`` is exactly
        the landed shape: the dialog's ``threading.Event.is_set`` bound
        method, polled by the worker.  ``BackupUncertainPublication`` is
        reported as an UNCERTAIN outcome — never success, never a clean
        cancellation, never silently swallowed.

        Offload shape: this milestone drives the landed semantic command
        ``BotsApplication.create_backup``, which owns the command admission,
        the worker-thread offload and the product semantics.  (That command
        previously could not complete against a real data-root authority
        because its offload copied the caller's authority grant into the
        worker; that pre-existing landed defect is repaired in
        ``core/application.py`` as R-M4-1, so the controller no longer needs
        to reach into application internals.)
        """

        cancel_event = threading.Event()
        bridge = Phase9ProgressBridge(dialog)
        if dialog is not None:
            dialog.begin_run()
            cancel_event = dialog.cancel_event
            # The only sanctioned worker → GUI channel: the worker thread (via
            # the landed service callback) emits on the bridge; Qt delivers to
            # the dialog's slot on the GUI thread through a queued connection.
            bridge.progress_updated.connect(dialog.progress_update)
        try:
            result = await self._application.create_backup(
                destination,
                overwrite=overwrite,
                cancellation=cancel_event.is_set,
                progress_callback=bridge.callback,
            )
        except BackupUncertainPublication as exc:
            if dialog is not None:
                dialog.publication_uncertain(str(exc))
            else:
                self._notify_status(
                    f"Backup publication outcome is uncertain: {exc}"
                )
            return
        except BackupResolutionCancelled as exc:
            if dialog is not None:
                dialog.report_cancelled(str(exc))
            else:
                self._notify_status(f"Backup cancelled: {exc}")
            return
        except Exception as exc:
            if dialog is not None:
                dialog.submission_failed(str(exc))
            else:
                self._notify_status(f"Backup failed: {exc}")
            return
        if dialog is not None:
            dialog.creation_succeeded(result)
        self._notify_status(
            f"Backup created: {result.backup_id} "
            f"({result.receipt.artifact_size} bytes)"
        )

    # ------------------------------------------------------------------
    # Workflow 7 — independent backup verification (separate from creation)
    # ------------------------------------------------------------------

    def open_backup_verification(self, parent) -> None:
        existing = self._dialogs.get("backup_verification")
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from .phase9_dialogs import BackupVerificationDialog

        dialog = BackupVerificationDialog(parent)
        dialog.finished.connect(
            lambda _result, dialog=dialog: self._forget_dialog(
                "backup_verification", dialog
            )
        )
        dialog.verify_requested.connect(
            lambda package, expected_id, dialog=dialog: self.schedule(
                self.run_backup_verification(package, expected_id, dialog=dialog)
            )
        )
        self._dialogs["backup_verification"] = dialog
        dialog.show()

    async def run_backup_verification(
        self, package, expected_backup_id, *, dialog=None
    ) -> None:
        """Await the landed (already offloaded) ``verify_backup`` command.

        Typed refusals (``BackupArchiveInvalid`` / ``BackupUnsupported`` /
        ``BackupResourceLimit`` / expected-id mismatch) are surfaced verbatim;
        success presents the verification receipt truthfully.
        """

        if dialog is not None:
            dialog.begin_run()
        try:
            result = await self._application.verify_backup(
                package,
                expected_backup_id=expected_backup_id,
            )
        except Exception as exc:
            if dialog is not None:
                dialog.submission_failed(str(exc))
            else:
                self._notify_status(f"Backup verification refused: {exc}")
            return
        if dialog is not None:
            dialog.verification_succeeded(result)
        self._notify_status(
            f"Backup verified: {result.receipt.backup_id} "
            f"({result.receipt.outcome})"
        )

    # ------------------------------------------------------------------
    # Workflow 8 — whole-installation restore handoff (no live restore)
    # ------------------------------------------------------------------

    _RESTORE_HANDOFF_DIALOG_KEY = "restore_handoff"

    def open_restore_handoff(self, parent, handoff) -> None:
        """Tools → "Restore From Backup…" (workflow 8, S1).

        ``handoff`` is the runtime-owned capability injected into the window;
        with none attached (a window constructed without the keyword) the
        action is inert and says so, instead of guessing a route.  The dialog
        is a plain selection surface: the coordinator owns the sealed chain.
        """

        if handoff is None:
            self._notify_status(
                "Restore is unavailable: no restore handoff capability is "
                "attached to this window"
            )
            return
        existing = self._dialogs.get(self._RESTORE_HANDOFF_DIALOG_KEY)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from .phase9_dialogs import RestoreHandoffDialog

        dialog = RestoreHandoffDialog(parent)
        dialog.finished.connect(
            lambda _result, dialog=dialog: self._forget_dialog(
                self._RESTORE_HANDOFF_DIALOG_KEY, dialog
            )
        )
        coordinator = self._restore_handoff_coordinator(handoff)
        dialog.confirm_requested.connect(
            lambda package, expected_id, dialog=dialog: coordinator.start(
                package, expected_id, dialog=dialog
            )
        )
        self._dialogs[self._RESTORE_HANDOFF_DIALOG_KEY] = dialog
        dialog.show()

    def _restore_handoff_coordinator(self, handoff) -> "RestoreHandoffCoordinator":
        coordinator = getattr(self, "_restore_handoff_coordinator_instance", None)
        if coordinator is None:
            coordinator = RestoreHandoffCoordinator(
                self._application,
                handoff,
                notify=self._notify_status,
            )
            self._restore_handoff_coordinator_instance = coordinator
        return coordinator


#: The sealed S6 close bound (RESTORE_UI_HANDOFF.md §2 S6).  Module-level so
#: tests can shorten the bound without touching the sealed default's call site.
_RESTORE_HANDOFF_CLOSE_TIMEOUT_SECONDS = 10.0


class RestoreHandoffCoordinator:
    """Workflow 8 orchestration (S2/S5/S6) through the injected capability.

    The coordinator uses ONLY the injected restore-handoff capability
    (RESTORE_UI_HANDOFF.md §2.1): it never touches ``DesktopRuntime``, the
    window set or the Qt window lifecycle directly, and it never calls or
    offers whole-installation restore in this live session — restore runs
    only in the pre-store bootstrap child after the runtime has fully closed
    and released authority.  There is no second restore state machine and no
    destructive override surface here (invariants I2/I3).

    Task ownership (oracle R-1): the flow is deliberately NOT scheduled
    through :meth:`Phase9DesktopController.schedule` or
    ``MainWindow._schedule`` — both task sets are cancelled as soon as any
    window's ``_finish_close`` begins, which would cancel the orderly close
    at its first suspension during a *successful* close.  The coordinator
    keeps a strong reference to its own task and lets it run to the close
    outcome; the capability in turn never clears a request whose close has
    already begun when the awaiter is cancelled.
    """

    def __init__(self, application, handoff, *, notify=None) -> None:
        self._application = application
        self._handoff = handoff
        self._notify = notify
        self._tasks: set[asyncio.Task[None]] = set()
        self.last_report: str | None = None

    def start(
        self,
        package,
        expected_backup_id,
        *,
        dialog=None,
        close_timeout: float | None = None,
    ) -> asyncio.Task[None] | None:
        """Run the sealed chain as a coordinator-owned task (see class doc)."""

        coroutine = self.run(
            package, expected_backup_id, dialog=dialog, close_timeout=close_timeout
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Bare Qt context without a running loop: nothing has changed.
            coroutine.close()
            if dialog is not None:
                dialog.submission_failed(
                    "Restore is unavailable without a running event loop."
                )
            return None
        task = loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run(
        self,
        package,
        expected_backup_id,
        *,
        dialog=None,
        close_timeout: float | None = None,
    ) -> None:
        """S2 verify → S3 consequence → S5 register → S6 orderly close.

        S2 runs the landed (already offloaded) ``verify_backup`` command: a
        typed refusal aborts with the error surfaced verbatim and changes
        nothing.  On success the verified backup id is bound as the expected
        id of the handoff request.  S5 registers the one-shot immutable
        request — registering alone does NOT close windows.  S6 closes every
        open window through the ordinary ``window.close()`` path; on a
        decline/timeout the capability has already cleared the request and
        this coordinator reports that NO restore was initiated.  When every
        window is closed, ``serve()``'s own path closes the runtime
        (authority released LAST) and its post-close step consumes the
        still-pending request: the result surface is presented there, not by
        this (now closed) controller.
        """

        from bots5.bootstrap.desktop import RestoreHandoffRequest

        if close_timeout is None:
            # Resolved at call time so the sealed default lives in exactly
            # one place and tests can shorten the bound without touching
            # this call site.
            close_timeout = _RESTORE_HANDOFF_CLOSE_TIMEOUT_SECONDS

        # S2 — live independent verification (offloaded inside the landed
        # command; the loop stays responsive).
        try:
            result = await self._application.verify_backup(
                package, expected_backup_id=expected_backup_id
            )
        except Exception as exc:
            if dialog is not None:
                dialog.submission_failed(str(exc))
            else:
                self._notify_status(f"Restore refused: {exc}")
            return
        bound_backup_id = result.receipt.backup_id

        if dialog is not None and not dialog.isVisible():
            # The operator dismissed the selection dialog while the
            # verification ran: abort before any consequence is presented.
            # Nothing was registered and nothing has changed.
            return

        # S3 — the explicit modal consequence warning, presented by the
        # dialog.  Cancel aborts before anything has changed.
        if dialog is not None and not dialog.confirm_consequence():
            return
        if dialog is not None:
            # The dialog closes itself BEFORE registering (oracle R-3); it is
            # not a member of DesktopRuntime.windows, so the orderly close
            # below never has to close it and no parentless restore dialog is
            # left open.
            dialog.accept()

        # S5 — register the ONE immutable, one-shot request.  A second
        # restore can never be queued behind a pending one.
        try:
            self._handoff.register(
                RestoreHandoffRequest(
                    package=Path(package).expanduser(),
                    expected_backup_id=bound_backup_id,
                )
            )
        except StateError as exc:
            self._notify_status(f"Restore not initiated: {exc}")
            return

        # S6 — orderly close of every open window through the ordinary
        # window.close() path (including the active-generation prompt for the
        # last window, oracle R-2).  On decline/timeout the capability has
        # already cleared the request: nothing destructive happened and no
        # child is launched.
        outcome = await self._handoff.request_orderly_close(timeout=close_timeout)
        if not outcome.all_closed:
            self.report_handoff_aborted(outcome.remaining)
            return

    def report_handoff_aborted(self, remaining: int) -> str:
        """Truthful abort report: NO restore was initiated, nothing changed."""

        message = (
            "no restore was initiated: the application did not close cleanly "
            f"({remaining} window(s) still open).  Nothing was changed."
        )
        self.last_report = message
        if self._notify is not None:
            self._notify(message)
        return message
