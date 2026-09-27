from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bots5.core.application import (
    ApplicationCloseState,
    BotsApplication,
    GenerationMode,
    TerminalCloseError,
    TerminalCloseResult,
)
from bots5.core.errors import AuthorityError, BackupError, BackupUnclassifiedState, CoreError, StateError
from bots5.core.events import EventBus
from bots5.core.provider_configuration import ProviderConfiguration
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import AppPaths, resolve_app_paths
from bots5.infrastructure.authority_lock import AuthorityLock
from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
)
from bots5.core.backup import BackupService
from bots5.domain.backup import canonical_backup_json
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.infrastructure.generation.router import BuiltinProviderRouter
from bots5.infrastructure.restore_service import RestoreService
from bots5.infrastructure.secrets import secret_store_for
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.base import ReasoningEffort
# Qt-free module import: bots5.desktop.session (a QObject subclass) imports
# PySide6, so it is imported where the desktop session is actually composed
# (build_runtime).  The non-UI restore initiation path below must be able to
# run with PySide6 entirely unavailable.
from bots5.desktop.profile import DesktopSessionInfo


@dataclass(slots=True)
class DesktopRuntime:
    paths: AppPaths
    authority: AuthorityLock
    application: BotsApplication
    session: DesktopSessionInfo
    workspace: DesktopSessionController
    windows: list[object] = field(default_factory=list)
    _opening_windows: set[asyncio.Task[None]] = field(default_factory=set)
    _close_state: ApplicationCloseState = field(
        default=ApplicationCloseState.OPEN, init=False
    )
    _close_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _close_task: asyncio.Task[TerminalCloseResult] | None = field(
        default=None, init=False
    )
    _close_result: TerminalCloseResult | None = field(default=None, init=False)

    def _forget_window(self, window: object) -> None:
        if window in self.windows:
            self.windows.remove(window)

    def _request_new_window(self) -> None:
        if self._close_state is not ApplicationCloseState.OPEN:
            return
        task = asyncio.create_task(self.open_window())
        self._opening_windows.add(task)
        task.add_done_callback(self._opening_windows.discard)

    async def open_window(self, state=None):
        if self._close_state is not ApplicationCloseState.OPEN:
            raise StateError("desktop runtime is closed")
        from bots5.desktop.window import MainWindow

        window = MainWindow(
            self.application,
            self.session,
            workspace=self.workspace,
            window_state=state,
        )
        self.windows.append(window)
        window.closed.connect(lambda window=window: self._forget_window(window))
        window.new_window_requested.connect(
            lambda: self._request_new_window()
        )
        try:
            await window.initialize()
            window.show()
        except BaseException:
            self._forget_window(window)
            await window.stop_bridge_async()
            raise
        return window

    @staticmethod
    def _runtime_error(stage: str, *, authority: bool = False) -> TerminalCloseError:
        return TerminalCloseError(
            stage=stage,
            code=f"runtime_close_{stage}_failed",
            public_kind="authority" if authority else "state",
            message=f"desktop runtime close failed during {stage}",
        )

    async def _close_driver(self) -> TerminalCloseResult:
        errors: list[TerminalCloseError] = []
        try:
            for task in tuple(self._opening_windows):
                if not task.done():
                    task.cancel()
            if self._opening_windows:
                results = await asyncio.gather(
                    *self._opening_windows, return_exceptions=True
                )
                if any(
                    isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                    for result in results
                ):
                    errors.append(self._runtime_error("opening_windows"))
        except BaseException:
            errors.append(self._runtime_error("opening_windows"))

        try:
            await self.workspace.close()
        except BaseException:
            errors.append(self._runtime_error("workspace"))

        try:
            await self.application.close()
        except BaseException:
            application_result = self.application._close_result
            if application_result is None:
                errors.append(self._runtime_error("application", authority=True))
            else:
                errors.extend(application_result.errors)

        try:
            self.authority.release()
        except BaseException:
            errors.append(self._runtime_error("outer_authority", authority=True))

        precedence = {
            "store": 0,
            "outer_authority": 1,
            "application": 1,
            "workspace": 2,
            "opening_windows": 2,
            "execution": 3,
            "reconciliation": 4,
            "events": 5,
        }
        errors.sort(key=lambda error: precedence[error.stage])
        result = TerminalCloseResult(tuple(errors))
        self._close_result = result
        self._close_state = (
            ApplicationCloseState.CLOSED
            if result.succeeded
            else ApplicationCloseState.FAILED
        )
        return result

    def _forget_close_task(
        self, task: asyncio.Task[TerminalCloseResult]
    ) -> None:
        if self._close_task is task:
            task.result()
            self._close_task = None

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        if self._close_state is ApplicationCloseState.OPEN:
            self._close_state = ApplicationCloseState.CLOSING
            self._close_loop = loop
            task = loop.create_task(self._close_driver())
            self._close_task = task
            task.add_done_callback(self._forget_close_task)
        elif self._close_result is None and self._close_loop is not loop:
            raise StateError("desktop runtime close belongs to another event loop")
        result = self._close_result
        if result is None:
            task = self._close_task
            if task is None:
                raise StateError("desktop runtime close has no terminal operation")
            result = await asyncio.shield(task)
        if not result.succeeded:
            error = result.errors[0]
            if error.public_kind == "authority":
                raise AuthorityError(error.message) from None
            raise StateError(error.message) from None


class RestoreStartupCoordinator:
    """Slice D startup interception (invariant I12).

    Runs in ``build_runtime`` after ``paths.ensure_non_authoritative()`` and
    before ``authority.open_store()`` so that an interrupted whole-install
    restore is reconciled strictly from its durable journal before normal
    startup can fabricate or open a fresh installation.  With no restore
    journal present the coordinator performs no live mutation and normal
    startup is unchanged in behaviour.

    The coordinator owns the two remaining coordinator-side obligations:

    - receipt finalisation: on a later startup, once the adopted
      installation has reached the application head, the restore receipt is
      updated to ``post_adoption_migration="completed"`` with
      ``target_revision_after_migration``.  If the deferred post-adoption
      forward migration failed and rolled back (D-B=B.1), the durable
      migration recovery journal attributes that failure and the receipt
      records ``post_adoption_migration="failed_rolled_back"`` together
      with the migration recovery reference; evidence that cannot be
      attributed raises fail-closed and never fabricates a terminal state.
      This is a receipt-record update only — no authoritative data
      mutation, no lifecycle state, no reopening of the restore journal.
    - the D-D=D.2 destructive-override surface: an explicit, separately
      supplied operator authorization (keyword-only on ``build_runtime``,
      default OFF, no UI).  With the authorization supplied, a pending
      restore transaction halted at ``PRESERVING`` — the one state in which
      no rollback source exists — is *executed* destructively by the M5
      ``RestoreService.execute_destructive_override`` machine, which
      re-proves all four validity conditions under the transaction lock and
      never waives authority/target-identity uncertainty.  A supplied
      authorization that cannot be exercised (no qualifying transaction, a
      rollback-capable transaction, an unverifiable source) stops startup
      fail-closed instead of being silently discarded; the normal restore
      path never falls back to the override.
    """

    def __init__(
        self,
        authority: AuthorityLock,
        *,
        destructive_override: bool = False,
    ) -> None:
        if type(destructive_override) is not bool:
            raise TypeError(
                "destructive_override must be an explicit bool operator "
                "authorization; it is never implied or defaulted"
            )
        self._authority = authority
        self._destructive_override = destructive_override

    @property
    def destructive_override_authorized(self) -> bool:
        """The separately-supplied operator authorization (default OFF)."""
        return self._destructive_override

    def before_store_open(self) -> dict[str, object] | None:
        """Reconcile any interrupted restore, then finalise its receipt.

        With the D-D=D.2 operator authorization supplied, a transaction
        halted at ``PRESERVING`` is executed destructively instead of being
        aborted; a supplied authorization that cannot be exercised raises
        and stops startup (fail closed, never silently discarded).
        Otherwise the interrupted restore is reconciled strictly from its
        durable journal.  Either way an unattributable or ambiguous journal
        state raises and stops startup, and a rolled-back restore requires a
        clean restart.  Returns the reconciliation/finalisation summary when
        a journal or a pending receipt was observed, else ``None``.
        """
        service = RestoreService(self._authority)
        summary: dict[str, object] | None = None
        if self._destructive_override:
            summary = self.execute_destructive_override()
        else:
            summary = service.reconcile()
        if summary is not None:
            action = summary.get("action")
            if action not in {"aborted", "completed", "awaiting-restart"}:
                raise BackupError(
                    "restore reconciliation returned an unattributable action: "
                    f"{action!r}"
                )
            if summary.get("restart_required"):
                raise BackupError(
                    "restore rolled back to the preserved installation; restart "
                    "required before startup can continue"
                )
        finalisation = service.finalise_restore_receipt()
        if finalisation is not None:
            action = finalisation.get("action")
            if action not in {
                "pending_migration",
                "migration_recovery_pending",
                "finalised",
                "failed_rolled_back",
            }:
                raise BackupError(
                    "restore receipt finalisation returned an unattributable "
                    f"action: {action!r}"
                )
        if summary is None and finalisation is None:
            return None
        return {"reconciliation": summary, "receipt_finalisation": finalisation}

    def evaluate_destructive_override(self) -> dict[str, object]:
        """Evaluate the D-D=D.2 gate with the build_runtime operator authorization.

        Delegates to the restore service's gate; refuses unless the target
        identity is proven, the Backup v1 source independently verified, the
        current installation truthfully will not remain rollback-capable,
        and the operator explicitly authorized the destructive consequence.
        """
        return RestoreService(self._authority).evaluate_destructive_override(
            operator_authorized=self._destructive_override
        )

    def execute_destructive_override(self) -> dict[str, object]:
        """Execute the D-D=D.2 destructive continuation (M5).

        Runs only when the separately supplied operator authorization is
        set; the restore service re-proves all four validity conditions
        under the transaction lock before the destructive step and fails
        closed on any authority/target-identity uncertainty.  A supplied
        authorization that cannot be exercised raises and stops startup
        instead of being silently discarded.
        """
        return RestoreService(self._authority).execute_destructive_override(
            operator_authorized=self._destructive_override
        )


def _prepare_data_root_topology(paths: AppPaths) -> None:
    """Prepare the data-root topology every startup entry point shares.

    The XDG application directories live under the data root whenever an
    explicit override root is used, so creating them changes the root
    topology.  They must exist before the authority baselines the root:
    every acquisition — and therefore every restore journal identity
    record — must observe the same root topology for restart
    reconciliation to attribute a journal by identity (I11) instead of
    failing closed forever on a transient first-run link-count delta.

    The data root itself must therefore exist first, and it must be
    owner-only: DataRootAuthority._check_directory requires mode 0o700 and
    creates the root that way when it does not exist.  Creating the XDG
    directories first would otherwise let mkdir() create the root with
    default (umask-derived) permissions, so a genuine first-ever run would
    fail acquisition with "unsafe owner or permissions".
    """
    paths.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.ensure_non_authoritative()


def build_runtime(
    data_root: Path | None = None,
    *,
    backend: str = "fake",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    reasoning_effort: ReasoningEffort | None = None,
    destructive_restore_override: bool = False,
) -> DesktopRuntime:
    from bots5.desktop.session import DesktopSessionController

    paths = resolve_app_paths(data_root)
    _prepare_data_root_topology(paths)
    authority = AuthorityLock(paths.data_root).acquire()
    try:
        # Slice D (I12): restore interception.  If a restore journal is
        # present, reconcile the interrupted restore before normal startup
        # can fabricate or open a fresh installation; with no journal this
        # changes nothing.  Must stay after the root is baselined and
        # non-authoritative state is prepared, and before open_store().
        RestoreStartupCoordinator(
            authority,
            destructive_override=destructive_restore_override,
        ).before_store_open()
        store = authority.open_store()
        clock = SystemClock()
        ids = Uuid7Factory()
        events = EventBus(clock, ids)
        if backend == "fake":
            generation_backend = BuiltinProviderRouter(fake_backend=FakeStreamingBackend())
            backend_id = "fake"
            selected_model = "fake-v0.1"
            provider_id = None
            selected_base_url = None
            selected_api_key_env = None
        elif backend == "local_openai":
            if not base_url or not model:
                raise ValueError(
                    "local_openai requires --base-url and --model"
                )
            provider = OpenAICompatibleProvider(base_url, api_key_env=api_key_env)
            generation_backend = OpenAICompatibleStreamingBackend(
                provider,
                provider_id="local_openai",
                base_url=provider.base_url,
                api_key_env=provider.api_key_env,
                reasoning_effort=reasoning_effort,
            )
            backend_id = OpenAICompatibleStreamingBackend.backend_id
            selected_model = model
            provider_id = "local_openai"
            selected_base_url = provider.base_url
            selected_api_key_env = provider.api_key_env
        else:
            raise ValueError(f"unsupported desktop backend: {backend}")
        configuration = (
            ProviderConfiguration(store, ids, clock, secret_store_factory=secret_store_for)
            if backend == "fake"
            else None
        )
        generation_mode = (
            GenerationMode.CONFIGURED
            if backend == "fake"
            else GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI
        )
        backup_service = BackupService(
            RootedBackupCaptureAdapter(
                authority,
                store,
                paths,
                BackupZipPackageAdapter(),
                data_root_is_override=data_root is not None,
            ),
            BackupZipPackageAdapter(),
            BackupFilePublicationAdapter(),
            ids,
        )
        application = BotsApplication(
            store,
            events,
            generation_backend,
            ids=ids,
            clock=clock,
            backend_id=backend_id,
            model=selected_model,
            provider_id=provider_id,
            base_url=selected_base_url,
            api_key_env=selected_api_key_env,
            configuration=configuration,
            generation_mode=generation_mode,
            backup_service=backup_service,
        )
        session = DesktopSessionInfo(
            backend_id=backend_id,
            model=selected_model,
            provider_id=provider_id,
            generation_mode=generation_mode.value,
            phase6_enabled=application.phase6_enabled,
        )
        return DesktopRuntime(
            paths,
            authority,
            application,
            session,
            DesktopSessionController(application, session, ids=ids),
        )
    except Exception:
        authority.release()
        raise


def _initiate_restore(
    package: Path,
    data_root: Path | None,
    expected_backup_id: str | None,
) -> int:
    """Authorized non-UI whole-installation restore initiation (Slice D).

    Enters at the accepted bootstrap/composition boundary: exactly the root
    preparation and ``DataRootAuthority`` acquisition of ``build_runtime``,
    then the accepted ``RestoreStartupCoordinator`` interception
    (``before_store_open``: reconcile any interrupted restore strictly
    before anything can open a store), then one ``RestoreService.restore``
    transaction — the existing restore coordinator/service, with every
    validation, preservation, staging, adoption, restart, rollback and
    fail-closed semantic intact.  Nothing on this path ever opens the
    store: the adopted installation is migrated forward and its receipt
    finalised by a later normal startup (D-B=B.1).  The destructive
    override stays at the coordinator default (OFF); this surface exposes
    no override of its own.

    It never imports Qt: the module import is Qt-free and the Qt machinery
    in ``main`` sits strictly behind the desktop branch.

    Typed outcomes are reported truthfully and the exit status follows
    them:

    - ``0`` — the restore committed: the receipt is written to stdout as
      canonical JSON, byte-identical to the durable receipt file.
    - ``2`` — not committed, typed refusal or typed rollback
      (``restore not committed: <qualified type>: <message>`` on stderr),
      e.g. package verification refusal, unsupported revision, quiescence
      or lock refusal, or ``restore rolled back to the preserved
      installation; restart required``.  A refused verification mutates no
      live state; a rollback converges the live installation back to the
      preserved pre-restore state and leaves the durable journal for the
      restart acknowledgment.
    - ``3`` — failed closed (``restore failed closed: ...`` on stderr):
      unattributable evidence, authority poisoned, human inspection
      required.
    - ``1`` — the restore committed but the data-root authority then failed
      to release cleanly, or an unexpected non-B.O.T.S. error escaped
      (fail loud).
    """
    paths = resolve_app_paths(data_root)
    _prepare_data_root_topology(paths)
    try:
        authority = AuthorityLock(paths.data_root).acquire()
    except CoreError as exc:
        print(
            f"restore not committed: {type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            file=sys.stderr,
        )
        return 2
    try:
        RestoreStartupCoordinator(authority).before_store_open()
        receipt = RestoreService(authority).restore(
            package, expected_backup_id=expected_backup_id
        )
    except BackupUnclassifiedState as exc:
        outcome, code = (
            f"restore failed closed: "
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            3,
        )
    except CoreError as exc:
        outcome, code = (
            f"restore not committed: "
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            2,
        )
    else:
        sys.stdout.buffer.write(canonical_backup_json(receipt))
        sys.stdout.buffer.flush()
        return _release_after_restore_initiation(authority, 0)
    print(outcome, file=sys.stderr)
    return _release_after_restore_initiation(authority, code)


def _release_after_restore_initiation(authority: AuthorityLock, code: int) -> int:
    """Release the authority after restore initiation without masking it.

    On a committed restore a release failure is integrity-relevant and
    turns the exit non-zero.  On a refused or failed-closed outcome the
    authority may already be poisoned; a terminal close failure then
    carries the same evidence that was already reported, so it is noted
    and never allowed to mask the restore outcome.
    """
    try:
        authority.release()
    except BaseException as exc:
        if code == 0:
            print(
                "restore committed but the data-root authority did not "
                f"release cleanly: {type(exc).__module__}.{type(exc).__qualname__}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(
            "note: the data-root authority also failed to release cleanly: "
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}",
            file=sys.stderr,
        )
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bots5-desktop")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="override the XDG application data root",
    )
    parser.add_argument(
        "--backend",
        choices=("fake", "local_openai"),
        default="fake",
        help=(
            "generation backend; local_openai is explicit Phase 3 legacy "
            "compatibility mode (Phase 6 planning/accounting disabled)"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="normalized local OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model identifier for the selected real backend",
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="optional environment-variable name for local backend authentication",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none",),
        default=None,
        help="optional OpenAI-compatible reasoning setting",
    )
    parser.add_argument(
        "--restore-from",
        type=Path,
        default=None,
        help=(
            "initiate one whole-installation restore from this Backup v1 "
            "package, report the typed outcome, and exit without opening "
            "the store or any UI (generation-backend options are not "
            "consulted on this path)"
        ),
    )
    parser.add_argument(
        "--expected-backup-id",
        default=None,
        help=(
            "with --restore-from: refuse the restore unless the package "
            "independently verifies against this backup id"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.expected_backup_id is not None and args.restore_from is None:
        parser.error("--expected-backup-id requires --restore-from")
    if args.restore_from is not None:
        # Non-UI restore initiation: returns before any Qt import below.
        return _initiate_restore(
            args.restore_from, args.data_root, args.expected_backup_id
        )
    from PySide6.QtWidgets import QApplication
    from qasync import QEventLoop

    from bots5.desktop.window import MainWindow

    qt_application = QApplication.instance() or QApplication(sys.argv)
    original_quit_on_last_window_closed = qt_application.quitOnLastWindowClosed()
    qt_application.setQuitOnLastWindowClosed(False)

    try:
        event_loop = QEventLoop(qt_application)
        asyncio.set_event_loop(event_loop)
        try:
            runtime = build_runtime(
                args.data_root,
                backend=args.backend,
                base_url=args.base_url,
                model=args.model,
                api_key_env=args.api_key_env,
                reasoning_effort=args.reasoning_effort,
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        async def serve() -> None:
            workspace = runtime.workspace
            # build_runtime runs before qasync enters this loop.  Resume any
            # durable queue work before ordinary desktop admission without
            # exposing a UI lifecycle control surface.
            runtime.application._ensure_import_scheduler()
            states = tuple(
                state for state in await workspace.load_workspace() if state.restore_open
            )
            if not states:
                states = (None,)
            try:
                for state in states:
                    await runtime.open_window(state)
                await workspace.wait_closed()
            finally:
                cancelled = False
                try:
                    await runtime.close()
                except asyncio.CancelledError:
                    cancelled = True
                    # The outer waiter may be cancelled, but qasync must not
                    # stop while the shared non-exceptional close driver owns
                    # live application or authority capabilities.
                    await runtime.close()
                if cancelled:
                    raise asyncio.CancelledError from None

        try:
            with event_loop:
                event_loop.run_until_complete(serve())
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        qt_application.setQuitOnLastWindowClosed(original_quit_on_last_window_closed)


if __name__ == "__main__":
    raise SystemExit(main())
