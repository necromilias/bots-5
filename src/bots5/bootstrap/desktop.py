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
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database


@dataclass(slots=True)
class DesktopRuntime:
    paths: AppPaths
    authority: AuthorityLock
    application: BotsApplication

    async def close(self) -> None:
        await self.application.close()
        self.authority.release()


def build_runtime(data_root: Path | None = None) -> DesktopRuntime:
    paths = resolve_app_paths(data_root)
    paths.ensure()
    authority = AuthorityLock(paths.authority_lock).acquire()
    try:
        upgrade_database(paths.database)
        store = SQLiteAppStateStore.open(paths.database)
        clock = SystemClock()
        ids = Uuid7Factory()
        events = EventBus(clock, ids)
        application = BotsApplication(
            store,
            events,
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        return DesktopRuntime(paths, authority, application)
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
        runtime = build_runtime(args.data_root)

        async def serve() -> None:
            window = MainWindow(runtime.application)
            closed = asyncio.Event()
            window.closed.connect(closed.set)
            try:
                await window.initialize()
                window.show()
                await closed.wait()
            finally:
                window.stop_bridge()
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
