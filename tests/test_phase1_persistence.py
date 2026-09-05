from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, text

from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.models import Chat
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database


def test_real_migration_creates_state_schema_and_enables_sqlite_safety(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        assert set(inspect(store.engine).get_table_names()) == {
            "alembic_version",
            "chats",
            "generation_attempts",
            "messages",
            "workspace_windows",
        }
        with store.engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006_phase4_workspace"

        now = datetime.now(timezone.utc)
        chat = Chat(str(uuid4()), "Test", now, now)
        store.create_chat(chat)
        persisted = store.list_chats()[0]
        assert persisted.id == chat.id
        assert persisted.title == chat.title
        assert persisted.created_at == parse_utc(utc_iso(chat.created_at))
        assert persisted.updated_at == parse_utc(utc_iso(chat.updated_at))
    finally:
        store.close()
