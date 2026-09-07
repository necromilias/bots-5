"""Test-only compatibility adapter for pre-Phase-6 persistence tests.

Production deliberately has no pathname store facade or public Engine.  Older
tests still need a pathname at which a stock SQLite connection can exercise the
raw-DML threat boundary, so this adapter maps that test pathname to a private
authority root and exposes the private Engine only through a proxy.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from bots5.infrastructure.data_root_authority import DataRootAuthority


def _root_for(database: Path) -> Path:
    return database.with_name(f".{database.name}.bots5-data-root")


def _move_seed(database: Path, canonical: Path) -> None:
    if database.is_symlink():
        if database.resolve(strict=False) != canonical:
            raise RuntimeError("test database alias points outside its authority root")
        return
    if not database.exists():
        return
    if canonical.exists():
        raise RuntimeError("test seed collides with an existing authoritative main")
    os.rename(database, canonical)
    os.chmod(canonical, 0o600)
    for suffix in ("-journal", "-wal", "-shm"):
        source = database.with_name(database.name + suffix)
        if source.exists() or source.is_symlink():
            destination = canonical.with_name(canonical.name + suffix)
            os.rename(source, destination)
            os.chmod(destination, 0o600)


class _StoreProxy:
    __slots__ = ("_store",)

    def __init__(self, store):
        object.__setattr__(self, "_store", store)

    @property
    def engine(self):
        return self._store._engine

    def __getattr__(self, name):
        return getattr(self._store, name)

    def __setattr__(self, name, value):
        setattr(self._store, name, value)


class SQLiteAppStateStore:
    @classmethod
    def open(cls, database: Path | str):
        del cls
        path = Path(database).absolute()
        authority = DataRootAuthority(_root_for(path)).acquire()
        canonical = Path(authority.root) / "database" / "state.sqlite3"
        try:
            _move_seed(path, canonical)
            store = authority.open_store()
            if not path.is_symlink():
                path.symlink_to(canonical)
            return _StoreProxy(store)
        except BaseException:
            if canonical.exists() and not path.exists() and not path.is_symlink():
                path.symlink_to(canonical)
            authority.close()
            raise


def upgrade_database(database: Path | str) -> None:
    store = SQLiteAppStateStore.open(database)
    store.close()


def upgrade_to(database: Path | str, revision: str) -> None:
    """Build a historical fixture using an explicitly supplied test connection."""
    path = Path(database).absolute()
    engine = create_engine(f"sqlite:///{path}", future=True)
    config = Config()
    config.set_main_option(
        "script_location",
        str(
            Path(__file__).resolve().parents[1]
            / "src/bots5/infrastructure/persistence/migrations"
        ),
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()
