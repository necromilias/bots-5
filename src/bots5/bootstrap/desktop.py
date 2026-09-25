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
from bots5.core.errors import AuthorityError, StateError
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
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.infrastructure.generation.router import BuiltinProviderRouter
from bots5.infrastructure.secrets import secret_store_for
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.base import ReasoningEffort
from bots5.desktop.profile import DesktopSessionInfo
from bots5.desktop.session import DesktopSessionController


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


def build_runtime(
    data_root: Path | None = None,
    *,
    backend: str = "fake",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    reasoning_effort: ReasoningEffort | None = None,
) -> DesktopRuntime:
    paths = resolve_app_paths(data_root)
    authority = AuthorityLock(paths.data_root).acquire()
    try:
        paths.ensure_non_authoritative()
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
