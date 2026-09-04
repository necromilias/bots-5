"""SQLite state persistence and migrations for the desktop core."""

from .migration_runner import upgrade_database
from .sqlite import SQLiteAppStateStore

__all__ = ["SQLiteAppStateStore", "upgrade_database"]
