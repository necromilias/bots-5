from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import AppPaths, resolve_app_paths
from bots5.infrastructure.authority_lock import AuthorityLock
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.desktop.profile import DesktopSessionInfo


@dataclass(slots=True)
class DesktopRuntime:
    paths: AppPaths
    authority: AuthorityLock
    application: BotsApplication
    session: DesktopSessionInfo

    async def close(self) -> None:
        await self.application.close()
        self.authority.release()


def build_runtime(
    data_root: Path | None = None,
    *,
    backend: str = "fake",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
) -> DesktopRuntime:
    paths = resolve_app_paths(data_root)
    paths.ensure()
    authority = AuthorityLock(paths.authority_lock).acquire()
    try:
        upgrade_database(paths.database)
        store = SQLiteAppStateStore.open(paths.database)
        clock = SystemClock()
        ids = Uuid7Factory()
        events = EventBus(clock, ids)
        if backend == "fake":
            generation_backend = FakeStreamingBackend()
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
            )
            backend_id = OpenAICompatibleStreamingBackend.backend_id
            selected_model = model
            provider_id = "local_openai"
            selected_base_url = provider.base_url
            selected_api_key_env = provider.api_key_env
        else:
            raise ValueError(f"unsupported desktop backend: {backend}")
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
        )
        return DesktopRuntime(
            paths,
            authority,
            application,
            DesktopSessionInfo(
                backend_id=backend_id,
                model=selected_model,
                provider_id=provider_id,
            ),
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
        help="generation backend (fake is the default)",
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
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        async def serve() -> None:
            window = MainWindow(runtime.application, runtime.session)
            closed = asyncio.Event()
            window.closed.connect(closed.set)
            try:
                await window.initialize()
                window.show()
                await closed.wait()
            finally:
                await window.stop_bridge_async()
                await runtime.close()

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
