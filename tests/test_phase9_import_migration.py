from __future__ import annotations

import sqlite3

from bots5.infrastructure.persistence.phase9_schema import REQUIRED_TABLES, REQUIRED_TRIGGERS
from tests._authority_test_support import upgrade_to


PHASE8 = "0011_phase8_inspector_state"
PHASE9 = "0012_phase9_archive_import"


def test_0012_upgrade_is_additive_and_initializes_the_single_queue_controller(tmp_path):
    database = tmp_path / "state.sqlite3"
    upgrade_to(database, PHASE8)
    with sqlite3.connect(database) as connection:
        native_count = connection.execute("SELECT count(*) FROM chats").fetchone()[0]
    upgrade_to(database, PHASE9)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PHASE9,)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert REQUIRED_TABLES <= tables
        triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert REQUIRED_TRIGGERS <= triggers
        assert connection.execute("SELECT singleton, revision, claimed_queue_id, owner_epoch FROM archive_import_queue_control").fetchall() == [(1, 0, None, None)]
        assert connection.execute("SELECT count(*) FROM chats").fetchone() == (native_count,)
