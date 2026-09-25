from __future__ import annotations

import hashlib
import os
import sqlite3

from bots5.infrastructure.authority_lock import AuthorityLock
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.persistence.migration_runner import upgrade_database
from bots5.infrastructure.persistence.phase9_schema import REQUIRED_TABLES, REQUIRED_TRIGGERS
from tests._authority_test_support import upgrade_to


PHASE8 = "0011_phase8_inspector_state"
PHASE9 = "0012_phase9_archive_import"
PRE_LEGACY = "0008_catalogue_refresh_outcomes"


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


def test_pre_legacy_supported_start_requires_a_fresh_whole_backup(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    paths = resolve_app_paths(root)
    seed = tmp_path / "seed.sqlite3"
    upgrade_to(seed, PRE_LEGACY)
    (paths.data_root / "database").mkdir(mode=0o700)
    database = paths.data_root / "database" / "state.sqlite3"
    os.replace(seed, database)
    os.chmod(database, 0o600)
    calls = []

    def fake_recovery_point(_authority, transaction_id):
        calls.append(transaction_id)
        data = b"fresh pre-migration whole-install backup"
        leaf = f"bots5-backup-{transaction_id}.botsbackup"
        fd = os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=_authority._directory_fd("recovery"),
        )
        os.write(fd, data)
        os.close(fd)
        return {
            "verification_receipt": {
                "format": "org.necromilias.bots5.installation-backup",
                "receipt_version": 1,
                "verification_id": "018f0000-0000-7000-8000-000000000001",
                "verified_at": "2026-09-25T00:00:00.000000Z",
                "backup_id": transaction_id,
                "backup_logical_content_digest": "2" * 64,
                "artifact_size": len(data),
                "artifact_sha256": hashlib.sha256(data).hexdigest(),
                "source_db_migration_revision": PRE_LEGACY,
                "verifier_application_version": "test",
                "sqlite_runtime_version": "test",
                "outcome": "VALID",
                "passed_checks": ["artifact-sha256"],
                "failed_check_ids": [],
                "reason_code": None,
            },
            "published_leaf": leaf,
        }

    monkeypatch.setattr(
        "bots5.infrastructure.backup_capture.create_migration_recovery_point",
        fake_recovery_point,
    )
    authority = AuthorityLock(root).acquire()
    try:
        upgrade_database(authority=authority)
    finally:
        authority.close()
    assert len(calls) == 1
    with sqlite3.connect(paths.data_root / "database" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PHASE9,)
