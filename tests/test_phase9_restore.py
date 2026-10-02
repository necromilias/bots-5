"""Phase 9 Slice D M2 T0/T1 oracles for the whole-installation restore service.

The tests use the production ``DataRootAuthority`` and rooted VFS with the
same authority/store/paths setup pattern as ``tests/test_phase9_backup.py``.
Crash windows are exercised with real subprocess death (the pattern of
``tests/test_phase7_migration_authority_faults.py``) so the transaction lock,
journal and on-disk state are exactly what a crashed restorer leaves behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bots5.bootstrap.desktop import (
    RestoreStartupCoordinator,
    build_runtime,
    main as desktop_main,
)
from bots5.bootstrap.desktop import _parser
from bots5.core.backup import BackupProgress, BackupProgressState
from bots5.core.errors import (
    AuthorityError,
    BackupArchiveInvalid,
    BackupError,
    BackupUnclassifiedState,
    BackupUnsupported,
)
from bots5.domain.backup import canonical_backup_json
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.attachments import _AttachmentFS
from bots5.infrastructure.backup_capture import (
    RootedBackupCaptureAdapter,
    create_migration_recovery_point,
)
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
)
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase6_blob_transition,
    install_transition_guard,
)
from bots5.infrastructure.restore_service import RestoreService
from tests._authority_test_support import upgrade_to


REPO = Path(__file__).resolve().parents[1]
HEAD = "0019_phase11_generation_settings"
PRIOR_HEAD = "0012_phase9_archive_import"

_RESTORE_JOURNAL = ".bots5-restore-journal.json"
_RESTORE_RECEIPT = ".bots5-restore-receipt.json"

#: The closed journal field set of the sealed design (spec section 1),
#: declared here as an independent oracle.  ``staged_attachment_state`` is the
#: durable publication plan (B2/B3 repair): the exact CAS payloads and
#: lifecycle artefacts the transaction is authorized to publish into the live
#: attachment namespaces, journaled before any live namespace mutation so an
#: abort withdraws precisely those and nothing else.
JOURNAL_FIELDS = {
    "journal_version",
    "restore_transaction_id",
    "backup_id",
    "phase",
    "sequence",
    "package_path",
    "package_sha256",
    "target_db_revision",
    "candidate_leaf",
    "displaced_leaf",
    "preservation_record",
    "staged_attachment_state",
    "directory_identities",
}

RECEIPT_FIELDS = {
    "backup_id",
    "package_sha256",
    "source_revision",
    "target_revision_at_commit",
    "post_adoption_migration",
    "target_revision_after_migration",
    "migration_recovery_reference",
    "transaction_id",
    "outcome",
}

#: The durable migration recovery journal of the existing migration machine
#: (``migration_runner``), which owns the post-adoption forward migration
#: under the adjudicated D-B=B.1 ordering.
_MIGRATION_JOURNAL = "database/migration/phase6-journal-v3.json"


def _new_authority(root: Path) -> DataRootAuthority:
    return DataRootAuthority(root.absolute()).acquire()


def _seed_database(
    tmp_path: Path, name: str, max_output_tokens: int, revision: str = HEAD
) -> Path:
    seed = tmp_path / f"{name}-seed.sqlite3"
    upgrade_to(seed, revision)
    connection = sqlite3.connect(seed)
    try:
        # The 0007 settings triggers call the guard SQL functions; a raw
        # test connection installs the same functions the store installs.
        install_transition_guard(connection, SimpleNamespace(info={}))
        connection.execute(
            "UPDATE application_generation_config SET max_output_tokens = ? "
            "WHERE id = 1",
            (max_output_tokens,),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(seed, 0o600)
    return seed


def _make_root(
    tmp_path: Path, name: str, max_output_tokens: int, revision: str = HEAD
) -> Path:
    """Build one disposable root at HEAD whose singleton setting is distinct."""
    root = tmp_path / name
    root.mkdir(mode=0o700)
    authority = _new_authority(root)
    authority.close()
    seed = _seed_database(tmp_path, name, max_output_tokens, revision=revision)
    os.rename(seed, root / "database" / "state.sqlite3")
    os.chmod(root / "database" / "state.sqlite3", 0o600)
    return root


def _capture_package(
    tmp_path: Path,
    name: str,
    max_output_tokens: int,
    revision: str = HEAD,
) -> tuple[Path, dict[str, object]]:
    """Capture one real, independently verified Backup v1 from a live root."""
    root = _make_root(tmp_path, name, max_output_tokens, revision=revision)
    authority = _new_authority(root)
    try:
        authority._claim_database()
        whole = create_migration_recovery_point(authority, Uuid7Factory().new())
    finally:
        authority.close()
    receipt = whole["verification_receipt"]
    assert receipt["source_db_migration_revision"] == revision
    return root / "recovery" / str(whole["published_leaf"]), receipt


@pytest.fixture
def restore_environment(tmp_path: Path) -> SimpleNamespace:
    """A captured package plus a distinct live target root (authority closed)."""
    package_path, receipt = _capture_package(tmp_path, "source", 2222)
    outside = tmp_path / "outside"
    outside.mkdir()
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    target_root = _make_root(tmp_path, "target", 1024)
    return SimpleNamespace(
        root=target_root,
        package=portable,
        backup_id=str(receipt["backup_id"]),
        artifact_sha256=str(receipt["artifact_sha256"]),
    )


def _live_sha(root: Path) -> str:
    """Hash the canonical database while no authority owns the root."""
    return hashlib.sha256((root / "database" / "state.sqlite3").read_bytes()).hexdigest()


def _stored_max_output_tokens(root: Path) -> int:
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    try:
        row = connection.execute(
            "SELECT max_output_tokens FROM application_generation_config WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _store_settings(root: Path) -> int:
    authority = _new_authority(root)
    store = authority.open_store()
    try:
        return int(store.get_application_generation_settings().max_output_tokens)
    finally:
        store.close()  # closing the store closes its authority


def _close_tolerant(authority: DataRootAuthority) -> None:
    """Terminal-close a poisoned authority; the poison is the assertion target."""
    try:
        authority.close()
    except Exception:
        pass


def _run_restore_child(root: Path, package: Path, source: str, expected_code: int) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), os.fspath(package)],
        cwd=REPO,
        env={
            **os.environ,
            "PYTHONPATH": os.fspath(REPO / "src"),
            "QT_QPA_PLATFORM": "offscreen",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == expected_code, (completed.stdout, completed.stderr)


def _close_runtime(runtime) -> None:
    asyncio.run(runtime.close())


def _prepare_desktop_root(root: Path) -> None:
    """Give a test root the topology every desktop startup observes.

    ``build_runtime`` prepares the XDG application directories under an
    explicit override data root before the authority baselines it, so any
    restore journal recorded for a desktop root must be recorded against
    that same topology.  Restore children in these tests run before
    ``build_runtime`` and therefore prepare the root the same way.
    """
    resolve_app_paths(root).ensure_non_authoritative()


@pytest.fixture
def startup_order(monkeypatch):
    """Mechanically observe the coordinator-vs-open_store order in build_runtime."""
    order: list[str] = []
    original_open_store = DataRootAuthority.open_store
    original_reconcile = RestoreService.reconcile

    def spy_open_store(self):
        order.append("open_store")
        return original_open_store(self)

    def spy_reconcile(self):
        order.append("reconcile")
        return original_reconcile(self)

    monkeypatch.setattr(DataRootAuthority, "open_store", spy_open_store)
    monkeypatch.setattr(RestoreService, "reconcile", spy_reconcile)
    return order


_ADOPT_INTENT_CRASH = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

def die(point):
    if point == "after-journal-write-ADOPT_INTENT":
        os._exit(61)

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(sys.argv[1]).acquire()
RestoreService = restore_service.RestoreService
RestoreService(authority).restore(sys.argv[2])
os._exit(92)
'''

_MID_EXCHANGE_CRASH = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

def die(point):
    if point == "after-adopt-exchange":
        os._exit(62)

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(sys.argv[1]).acquire()
RestoreService = restore_service.RestoreService
RestoreService(authority).restore(sys.argv[2])
os._exit(92)
'''

_ROLLBACK_INTENT_CRASH = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

def die(point):
    if point == "post-adoption-validation":
        raise RuntimeError("forced post-adoption validation failure")
    if point == "after-journal-write-RESTORE_ROLLBACK_INTENT":
        os._exit(63)

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(sys.argv[1]).acquire()
RestoreService = restore_service.RestoreService
RestoreService(authority).restore(sys.argv[2])
os._exit(92)
'''

#: The terminal rollback crash window: the rollback machine completed
#: deterministically (re-exchange re-validated) and the transaction died
#: immediately after the durable ``ROLLED_BACK`` journal write — the state in
#: which the next startup must withdraw the published attachments by the
#: durable plan.
_ROLLED_BACK_CRASH = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

def die(point):
    if point == "post-adoption-validation":
        raise RuntimeError("forced post-adoption validation failure")
    if point == "after-journal-write-ROLLED_BACK":
        os._exit(73)

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(sys.argv[1]).acquire()
restore_service.RestoreService(authority).restore(sys.argv[2])
os._exit(92)
'''

#: W2 scratch: the journal exists at PRESERVING with no preservation record
#: and the live installation is untouched.  This is the durable state of a
#: restore whose preservation could not (or did not) complete.
_PRESERVING_CRASH = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

def die(point):
    if point == "after-journal-write-PRESERVING":
        os._exit(64)

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(sys.argv[1]).acquire()
restore_service.RestoreService(authority).restore(sys.argv[2])
os._exit(92)
'''

#: A transaction that durably established a valid preservation record: the
#: current installation IS rollback-capable at this point.
_PRESERVED_CRASH = r'''
import os, sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

def die(point):
    if point == "after-journal-write-PRESERVED":
        os._exit(65)

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(sys.argv[1]).acquire()
restore_service.RestoreService(authority).restore(sys.argv[2])
os._exit(92)
'''

#: A startup whose deferred post-adoption forward migration (D-B=B.1) fails
#: at promoted validation and rolls back through the normal migration
#: machine, then dies immediately after the durable ``ROLLED_BACK`` journal
#: write — the crash window between the journaled failure and the terminal
#: cleanup (performed by the next startup's convergence under the approved
#: terminal-cleanup deferral).  This leaves exactly the crash-window state in
#: which the migration failure is durably journaled and attributable to the
#: restore receipt on the next startup.
_MIGRATION_ROLLBACK_CRASH = r'''
import os, sys
from pathlib import Path
from bots5.bootstrap.desktop import build_runtime
from bots5.infrastructure.persistence import migration_runner

original = migration_runner._verify_database
calls = 0
def fail_promoted(vfs, revision, **kwargs):
    global calls
    if revision == migration_runner._HEAD:
        calls += 1
        if calls == 2:
            raise RuntimeError("injected promoted validation failure")
    return original(vfs, revision, **kwargs)
migration_runner._verify_database = fail_promoted

def die(point):
    if point == "after-journal-directory-fsync-ROLLED_BACK":
        os._exit(66)

migration_runner._TEST_FAULT_HOOK = die
build_runtime(Path(sys.argv[1]))
os._exit(67)
'''

#: The ORDINARY (non-crash) failed post-adoption forward migration under the
#: approved terminal-cleanup deferral: the startup completes its rollback,
#: leaves the durable ``ROLLED_BACK`` journal plus the rolled-back private
#: candidate and recovery leaves in place, and fails with the ordinary
#: ``RuntimeError`` — nothing dies mid-flight.  This is the exact durable
#: state the next startup's receipt finaliser must attribute.
_ORDINARY_MIGRATION_FAILURE = r'''
import os, sys
from pathlib import Path
from bots5.bootstrap.desktop import build_runtime
from bots5.infrastructure.persistence import migration_runner

original = migration_runner._verify_database
calls = 0
def fail_promoted(vfs, revision, **kwargs):
    global calls
    if revision == migration_runner._HEAD:
        calls += 1
        if calls == 2:
            raise RuntimeError("injected promoted validation failure")
    return original(vfs, revision, **kwargs)
migration_runner._verify_database = fail_promoted

try:
    build_runtime(Path(sys.argv[1]))
except RuntimeError as exc:
    assert "prior source was restored" in str(exc)
    # The deferral: the durable rollback evidence survives the ordinary
    # failed run.
    journal = Path(sys.argv[1]) / "database/migration/phase6-journal-v3.json"
    assert journal.exists(), "deferred cleanup lost the durable rollback journal"
    os._exit(81)
os._exit(82)
'''

#: A startup whose post-adoption forward migration (D-B=B.1) crashes after
#: the durable ``PROMOTED`` journal write but before promoted validation.
#: The canonical database is the unvalidated candidate at head while the
#: migration machine still holds an unconverged journal.
_MIGRATION_PROMOTED_CRASH = r'''
import os, sys
from pathlib import Path
from bots5.bootstrap.desktop import build_runtime
from bots5.infrastructure.persistence import migration_runner

def die(point):
    if point == "after-journal-directory-fsync-PROMOTED":
        os._exit(68)

migration_runner._TEST_FAULT_HOOK = die
build_runtime(Path(sys.argv[1]))
os._exit(69)
'''


def test_verify_failure_leaves_live_state_untouched(tmp_path, restore_environment):
    env = restore_environment
    before = _live_sha(env.root)
    broken = tmp_path / "outside" / "broken.botsbackup"
    broken.write_bytes(env.package.read_bytes()[:-9])
    authority = _new_authority(env.root)
    try:
        with pytest.raises(BackupArchiveInvalid):
            RestoreService(authority).restore(broken)
        database = env.root / "database"
        assert not (database / _RESTORE_JOURNAL).exists()
        assert not list(database.glob(".bots5-restore-*.lock"))
        assert not list(database.glob(".bots5-restore-*.tmp"))
        assert list((env.root / "retained-installations").iterdir()) == []
        assert not authority.poison_pending
    finally:
        authority.close()
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024


def test_restore_commits_receipt_and_retains_preserved_installation(
    tmp_path, restore_environment
):
    env = restore_environment
    before = _live_sha(env.root)
    authority = _new_authority(env.root)
    try:
        receipt = RestoreService(authority).restore(env.package)
        assert set(receipt) == RECEIPT_FIELDS
        assert receipt["backup_id"] == env.backup_id
        assert receipt["package_sha256"] == env.artifact_sha256
        assert receipt["source_revision"] == HEAD
        assert receipt["target_revision_at_commit"] == HEAD
        assert receipt["post_adoption_migration"] == "not_required"
        assert receipt["target_revision_after_migration"] is None
        assert receipt["outcome"] == "RESTORED"
        database = env.root / "database"
        assert not (database / _RESTORE_JOURNAL).exists()
        assert not list(database.glob(".bots5-restore-*.lock"))
        assert not (env.root / "attachments" / "staging" / f"restore-{receipt['transaction_id']}").exists()
        # D-A=A.2: the displaced installation survives CLEANUP_PENDING and
        # RESTORE_COMMITTED and is never auto-removed by the transaction.
        artefact = (
            env.root
            / "retained-installations"
            / f"{receipt['transaction_id']}-{env.backup_id}.sqlite3"
        )
        assert artefact.exists()
        assert hashlib.sha256(artefact.read_bytes()).hexdigest() == before
        written = json.loads((database / _RESTORE_RECEIPT).read_text("utf-8"))
        assert written == receipt
        assert (database / _RESTORE_RECEIPT).read_bytes() == canonical_backup_json(receipt)
    finally:
        authority.close()
    assert _stored_max_output_tokens(env.root) == 2222
    assert _store_settings(env.root) == 2222


def test_adopt_intent_crash_aborts_cleanly_on_restart(tmp_path, restore_environment):
    env = restore_environment
    before = _live_sha(env.root)
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    assert journal.exists()
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert summary["restart_required"] is False
        txid = summary["transaction_id"]
        database = env.root / "database"
        assert not journal.exists()
        assert not (database / f".bots5-restore-{txid}.candidate.sqlite3").exists()
        assert not (database / f".bots5-restore-{txid}.lock").exists()
        assert not (env.root / "attachments" / "staging" / f"restore-{txid}").exists()
        assert not authority.poison_pending
        # W4 keeps the recorded preservation artefact (only unrecorded W2
        # scratch is discarded).
        assert (
            env.root / "retained-installations" / f"{txid}-{env.backup_id}.sqlite3"
        ).exists()
    finally:
        authority.close()
    assert _live_sha(env.root) == before
    assert _store_settings(env.root) == 1024


def test_mid_exchange_crash_is_atomically_attributed(tmp_path, restore_environment):
    env = restore_environment
    _run_restore_child(env.root, env.package, _MID_EXCHANGE_CRASH, 62)
    journal = env.root / "database" / _RESTORE_JOURNAL
    assert journal.exists()
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "completed"
        assert summary["restart_required"] is False
        assert summary["receipt"] is not None
        assert summary["receipt"]["backup_id"] == env.backup_id
        assert not journal.exists()
        assert not authority.poison_pending
    finally:
        authority.close()
    assert _stored_max_output_tokens(env.root) == 2222
    assert _store_settings(env.root) == 2222


def test_promoted_validation_failure_rolls_back_and_requires_restart(
    tmp_path, restore_environment
):
    env = restore_environment
    before = _live_sha(env.root)
    journal = env.root / "database" / _RESTORE_JOURNAL

    def hook(point):
        if point == "post-adoption-validation":
            raise RuntimeError("forced post-adoption validation failure")

    from bots5.infrastructure import restore_service as restore_service_module

    restore_service_module._TEST_FAULT_HOOK = hook
    authority = _new_authority(env.root)
    try:
        with pytest.raises(BackupError, match="restart required"):
            RestoreService(authority).restore(env.package)
        record = json.loads(journal.read_text("utf-8"))
        assert record["phase"] == "ROLLED_BACK"
        assert not authority.poison_pending
    finally:
        restore_service_module._TEST_FAULT_HOOK = None
        authority.close()
    assert _live_sha(env.root) == before
    # W9: a later startup reconciles the terminal rolled-back state away.
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "awaiting-restart"
        assert summary["restart_required"] is True
        assert not journal.exists()
        assert not authority.poison_pending
    finally:
        authority.close()
    assert _stored_max_output_tokens(env.root) == 1024
    assert _store_settings(env.root) == 1024


def test_rollback_intent_crash_completes_rollback_on_restart(
    tmp_path, restore_environment
):
    env = restore_environment
    before = _live_sha(env.root)
    journal = env.root / "database" / _RESTORE_JOURNAL
    _run_restore_child(env.root, env.package, _ROLLBACK_INTENT_CRASH, 63)
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "RESTORE_ROLLBACK_INTENT"
    assert _stored_max_output_tokens(env.root) == 2222  # adoption had happened
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "awaiting-restart"
        assert summary["restart_required"] is True
        assert json.loads(journal.read_text("utf-8"))["phase"] == "ROLLED_BACK"
        assert not authority.poison_pending
    finally:
        authority.close()
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["restart_required"] is True
        assert not journal.exists()
    finally:
        authority.close()
    assert _store_settings(env.root) == 1024


def test_journal_is_canonical_with_closed_field_set(tmp_path, restore_environment):
    env = restore_environment
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    raw = journal.read_bytes()
    record = json.loads(raw)
    # Byte-exact canonical JSON, duplicate-key-free, closed field set.
    assert canonical_backup_json(record) == raw
    assert set(record) == JOURNAL_FIELDS
    assert record["phase"] == "ADOPT_INTENT"
    assert record["sequence"] == 5
    assert record["journal_version"] == 1
    assert record["target_db_revision"] == HEAD
    assert record["displaced_leaf"] == record["candidate_leaf"]
    assert record["candidate_leaf"] == (
        f".bots5-restore-{record['restore_transaction_id']}.candidate.sqlite3"
    )
    assert set(record["directory_identities"]) == {
        "root", "database", "attachments", "recovery"
    }
    assert record["preservation_record"]["path"] == (
        "retained-installations/"
        f"{record['restore_transaction_id']}-{env.backup_id}.sqlite3"
    )
    assert set(record["preservation_record"]) == {
        "mode", "path", "statx_identity", "sha256", "bytes"
    }
    # The whole-transaction flock is held at the crash point (I11).
    locks = list((env.root / "database").glob(".bots5-restore-*.lock"))
    assert [lock.name for lock in locks] == [
        f".bots5-restore-{record['restore_transaction_id']}.lock"
    ]


def test_unattributable_rollback_source_fails_closed(tmp_path, restore_environment):
    """D-E=E.1: a mismatched rollback source is never guessed around."""
    env = restore_environment
    _run_restore_child(env.root, env.package, _MID_EXCHANGE_CRASH, 62)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    displaced = env.root / "database" / record["candidate_leaf"]
    displaced.write_bytes(displaced.read_bytes()[:-16])
    authority = _new_authority(env.root)
    try:
        with pytest.raises(BackupUnclassifiedState, match="cannot be attributed"):
            RestoreService(authority).reconcile()
        assert authority.poison_pending
    finally:
        _close_tolerant(authority)
    # The durable failed-closed marker is recorded and no forward mutation or
    # reconstruction happened: the evidence stays exactly where it was.
    assert json.loads(journal.read_text("utf-8"))["phase"] == "RESTORE_FAILED_CLOSED"
    assert displaced.exists()
    assert (
        env.root
        / "retained-installations"
        / f"{record['restore_transaction_id']}-{env.backup_id}.sqlite3"
    ).exists()


def test_torn_journal_fails_closed_and_preserves_bytes(tmp_path, restore_environment):
    env = restore_environment
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    corrupted = journal.read_bytes()[:-2]
    journal.write_bytes(corrupted)
    authority = _new_authority(env.root)
    try:
        with pytest.raises(BackupUnclassifiedState, match="torn or non-canonical"):
            RestoreService(authority).reconcile()
        assert authority.poison_pending
    finally:
        _close_tolerant(authority)
    # W10: the ambiguous bytes are preserved, never rewritten or guessed at.
    assert journal.read_bytes() == corrupted
    # Nothing was discarded on ambiguous evidence.
    locks = list((env.root / "database").glob(".bots5-restore-*.lock"))
    assert locks, "transaction lock leaf was removed on ambiguous evidence"
    txid = locks[0].name.removeprefix(".bots5-restore-").removesuffix(".lock")
    assert (env.root / "database" / f".bots5-restore-{txid}.candidate.sqlite3").exists()
    assert (env.root / "attachments" / "staging" / f"restore-{txid}").exists()


# ---------------------------------------------------------------------------
# Bounded-repair regressions (independent-review blockers B1/B2/B3).  Each
# test derives from a reproduced reviewer attack and asserts observable
# recovery: startup succeeds and the live installation is intact.
# ---------------------------------------------------------------------------


def test_stale_journal_temp_leaf_does_not_wedge_startup(tmp_path, restore_environment):
    """B1: a crash between the O_EXCL temp write and the rename leaves the
    deterministic ``.bots5-restore-journal-<txid>-<seq>.tmp`` leaf behind.
    Every later startup must still converge deterministically (I4): the
    re-attempted publication removes the debris and publishes provably
    intended content, the restore commits, and the live installation is
    usable — twice, so recovery is idempotent."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    # Crash right after the adoption exchange: journal at ADOPT_INTENT, the
    # live database is the candidate, the displaced leaf is the pre-restore
    # installation.
    _run_restore_child(env.root, env.package, _MID_EXCHANGE_CRASH, 62)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    txid = str(record["restore_transaction_id"])
    # Residue of a crash inside the seq-6 PROMOTED journal write: the temp
    # leaf was created (O_EXCL) but the rename never happened.
    stale = env.root / "database" / f".bots5-restore-journal-{txid}-6.tmp"
    stale.write_bytes(b'{"half":"written"')
    os.chmod(stale, 0o600)

    # Startup must converge instead of failing on FileExistsError forever.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert not journal.exists()
    assert not stale.exists()
    assert not list((env.root / "database").glob(".bots5-restore-journal-*.tmp"))
    assert not list((env.root / "database").glob(".bots5-restore-receipt-*.tmp"))
    # The restore committed: the live installation is the restored one.
    assert _stored_max_output_tokens(env.root) == 2222
    written = json.loads(
        (env.root / "database" / _RESTORE_RECEIPT).read_text("utf-8")
    )
    assert written["transaction_id"] == txid
    assert written["outcome"] == "RESTORED"
    # Recovery is idempotent (I4): the second startup behaves identically.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert _stored_max_output_tokens(env.root) == 2222


def test_aborted_staging_withdraws_published_payloads_and_keeps_preexisting(tmp_path):
    """B2: payloads published into the live CAS before adoption are withdrawn
    by the abort reconciliation exactly by the journaled publication plan; a
    pre-existing canonical object is never touched; the next normal startup
    succeeds and the live installation is intact."""
    orphan_payload = b"orphan-cas-payload-" + b"o" * 24
    kept_payload = b"pre-existing-target-payload" + b"k" * 15
    source = _make_root(tmp_path, "orphan-cas-source", 2222)
    orphan_digest = _install_object(source, orphan_payload)
    _install_blob_row(source, orphan_payload, state="ready")
    kept_digest = _install_object(source, kept_payload)
    _install_blob_row(source, kept_payload, state="ready")
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "orphan-cas.botsbackup")

    target = _make_root(tmp_path, "orphan-cas-target", 1024)
    # The target already carries the kept payload as a canonical object the
    # restored database attributes: the transaction verifies it in place and
    # must never publish — and never withdraw — it.
    _install_object(target, kept_payload)
    _install_blob_row(target, kept_payload, state="ready")
    assert not (target / "attachments" / "objects" / orphan_digest).exists()

    from bots5.infrastructure import restore_service as restore_service_module

    def hook(point: str) -> None:
        # After the payloads were published into the live CAS, before the
        # STAGED_VALIDATED journal write.
        if point == "before-journal-write-STAGED_VALIDATED":
            raise RuntimeError("injected crash after payload publication")

    restore_service_module._TEST_FAULT_HOOK = hook
    authority = _new_authority(target)
    try:
        with pytest.raises(RuntimeError):
            RestoreService(authority).restore(portable)
    finally:
        restore_service_module._TEST_FAULT_HOOK = None
        authority.close()

    journal = target / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "STAGING"
    plan = record["staged_attachment_state"]
    # The journaled plan covers exactly the orphan payload: the pre-existing
    # object is excluded because the transaction never publishes it.
    assert plan["objects"] == [orphan_digest]
    assert kept_digest not in plan["objects"]
    orphan = target / "attachments" / "objects" / orphan_digest
    assert orphan.exists(), "payload should have been published before the fault"

    authority = _new_authority(target)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        authority.close()
    assert not journal.exists()
    txid = str(record["restore_transaction_id"])
    assert not (target / "attachments" / "staging" / f"restore-{txid}").exists()
    assert not list((target / "database").glob(".bots5-restore-*.tmp"))
    # Observable recovery: the transaction's own payload is withdrawn and the
    # pre-existing object is untouched.
    assert not orphan.exists()
    assert (target / "attachments" / "objects" / kept_digest).exists()

    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _stored_max_output_tokens(target) == 1024
    assert (target / "attachments" / "objects" / kept_digest).exists()
    assert not orphan.exists()


def test_aborted_staging_withdraws_published_gc_artefacts_and_keeps_preexisting(
    tmp_path,
):
    """B3: package GC lifecycle artefacts published into the live
    ``attachments/gc`` namespace before adoption are withdrawn by the abort
    reconciliation exactly by the journaled plan; a pre-existing tombstone
    and its payload are never touched; the next normal startup succeeds and
    the live installation is intact."""
    orphan_payload = b"orphan-gc-payload-" + b"g" * 24
    kept_payload = b"kept-gc-payload-" + b"t" * 24
    gc_orphan = Uuid7Factory().new()
    gc_kept = Uuid7Factory().new()

    source = _make_root(tmp_path, "orphan-gc-source", 2222)
    orphan_digest = _install_object(source, orphan_payload)
    _install_blob_row(source, orphan_payload, state="deleting", gc_id=gc_orphan)
    _install_gc_artifact(
        source,
        gc_orphan,
        _AttachmentFS.tombstone_bytes(gc_orphan, bytes.fromhex(orphan_digest)),
    )
    kept_digest = _install_object(source, kept_payload)
    _install_blob_row(source, kept_payload, state="deleting", gc_id=gc_kept)
    _install_gc_artifact(
        source,
        gc_kept,
        _AttachmentFS.tombstone_bytes(gc_kept, bytes.fromhex(kept_digest)),
    )
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "orphan-gc.botsbackup")

    target = _make_root(tmp_path, "orphan-gc-target", 1024)
    # The target already carries the kept GC state (deleting row + payload +
    # tombstone): the restored database attributes it, the transaction
    # verifies the artefact in place, and the abort must never withdraw it.
    _install_object(target, kept_payload)
    _install_blob_row(target, kept_payload, state="deleting", gc_id=gc_kept)
    _install_gc_artifact(
        target,
        gc_kept,
        _AttachmentFS.tombstone_bytes(gc_kept, bytes.fromhex(kept_digest)),
    )
    assert not (target / "attachments" / "gc" / gc_orphan).exists()

    from bots5.infrastructure import restore_service as restore_service_module

    def hook(point: str) -> None:
        if point == "before-journal-write-STAGED_VALIDATED":
            raise RuntimeError("injected crash after gc artefact publication")

    restore_service_module._TEST_FAULT_HOOK = hook
    authority = _new_authority(target)
    try:
        with pytest.raises(RuntimeError):
            RestoreService(authority).restore(portable)
    finally:
        restore_service_module._TEST_FAULT_HOOK = None
        authority.close()

    journal = target / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "STAGING"
    plan = record["staged_attachment_state"]
    # The capture packages deleting-state payloads only as GC lifecycle
    # artefacts, so the plan carries exactly the orphan tombstone; the kept
    # tombstone is excluded because the transaction never publishes it.
    assert plan["objects"] == []
    assert [entry[0] for entry in plan["gc"]] == [gc_orphan]
    assert gc_kept not in [entry[0] for entry in plan["gc"]]
    orphan_tombstone = target / "attachments" / "gc" / gc_orphan
    assert orphan_tombstone.exists(), "tombstone should have been published"

    authority = _new_authority(target)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        authority.close()
    assert not journal.exists()
    # Observable recovery: the transaction's own artefact is withdrawn and
    # the pre-existing GC state is untouched.
    assert not orphan_tombstone.exists()
    assert (target / "attachments" / "objects" / kept_digest).exists()
    assert (target / "attachments" / "gc" / gc_kept).exists()

    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _store_settings(target) == 1024
    # The kept GC state completed through the existing Phase 6 recovery; the
    # withdrawn artefact never reappears.
    assert not (target / "attachments" / "gc" / gc_orphan).exists()


# ---------------------------------------------------------------------------
# Bounded-repair wave 5a: the LATE crash windows of the same B2/B3 defect.
# The early-window tests above crash BEFORE the STAGED_VALIDATED journal
# write — the exact write that used to wipe the plan — so they passed while
# the defect was live.  These tests crash/abort AFTER that write (at the
# durable ADOPT_INTENT boundary and on the terminal ROLLED_BACK path) and
# assert observable recovery: startup succeeds, the live installation is
# intact, and no published residue remains.
# ---------------------------------------------------------------------------


def _late_window_cas_package(tmp_path: Path, prefix: str):
    """A package carrying one payload the target lacks, plus one it has."""
    orphan_payload = f"{prefix}-orphan-payload-".encode() + b"o" * 24
    kept_payload = f"{prefix}-kept-payload-".encode() + b"k" * 24
    source = _make_root(tmp_path, f"{prefix}-cas-source", 2222)
    orphan_digest = _install_object(source, orphan_payload)
    _install_blob_row(source, orphan_payload, state="ready")
    kept_digest = _install_object(source, kept_payload)
    _install_blob_row(source, kept_payload, state="ready")
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, f"{prefix}-cas.botsbackup")

    target = _make_root(tmp_path, f"{prefix}-cas-target", 1024)
    # The target already carries the kept payload as a canonical object the
    # restored database attributes: the transaction verifies it in place and
    # must never publish — and never withdraw — it.
    _install_object(target, kept_payload)
    _install_blob_row(target, kept_payload, state="ready")
    return portable, target, orphan_digest, kept_digest


def _late_window_gc_package(tmp_path: Path, prefix: str):
    """A package carrying one GC tombstone the target lacks, plus one it has."""
    orphan_payload = f"{prefix}-orphan-gc-payload-".encode() + b"g" * 20
    kept_payload = f"{prefix}-kept-gc-payload-".encode() + b"t" * 20
    gc_orphan = Uuid7Factory().new()
    gc_kept = Uuid7Factory().new()

    source = _make_root(tmp_path, f"{prefix}-gc-source", 2222)
    orphan_digest = _install_object(source, orphan_payload)
    _install_blob_row(source, orphan_payload, state="deleting", gc_id=gc_orphan)
    _install_gc_artifact(
        source,
        gc_orphan,
        _AttachmentFS.tombstone_bytes(gc_orphan, bytes.fromhex(orphan_digest)),
    )
    kept_digest = _install_object(source, kept_payload)
    _install_blob_row(source, kept_payload, state="deleting", gc_id=gc_kept)
    _install_gc_artifact(
        source,
        gc_kept,
        _AttachmentFS.tombstone_bytes(gc_kept, bytes.fromhex(kept_digest)),
    )
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, f"{prefix}-gc.botsbackup")

    target = _make_root(tmp_path, f"{prefix}-gc-target", 1024)
    _install_object(target, kept_payload)
    _install_blob_row(target, kept_payload, state="deleting", gc_id=gc_kept)
    _install_gc_artifact(
        target,
        gc_kept,
        _AttachmentFS.tombstone_bytes(gc_kept, bytes.fromhex(kept_digest)),
    )
    return portable, target, gc_orphan, gc_kept, kept_digest


def test_adopt_intent_crash_withdraws_published_payloads_by_durable_plan(tmp_path):
    """B2 late window: a crash after the durable ADOPT_INTENT write — long
    after the STAGED_VALIDATED write that used to wipe the plan — must still
    withdraw exactly the published payload.  The plan is durable across every
    journal write from STAGING to the abort, so the live installation is
    byte-identical and bootable afterwards."""
    portable, target, orphan_digest, kept_digest = _late_window_cas_package(
        tmp_path, "late-adopt"
    )
    _prepare_desktop_root(target)
    _run_restore_child(target, portable, _ADOPT_INTENT_CRASH, 61)

    journal = target / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    plan = record["staged_attachment_state"]
    assert isinstance(plan, dict), (
        "the publication plan must survive the STAGED_VALIDATED journal write"
    )
    assert plan["objects"] == [orphan_digest]
    assert kept_digest not in plan["objects"]
    orphan = target / "attachments" / "objects" / orphan_digest
    assert orphan.exists(), "payload should have been published before the crash"

    authority = _new_authority(target)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        authority.close()

    txid = str(record["restore_transaction_id"])
    database = target / "database"
    assert not journal.exists()
    assert not (database / f".bots5-restore-{txid}.candidate.sqlite3").exists()
    assert not (database / f".bots5-restore-{txid}.lock").exists()
    assert not (target / "attachments" / "staging" / f"restore-{txid}").exists()
    assert not list(database.glob(".bots5-restore-*.tmp"))
    assert not list(database.glob(".bots5-restore-journal-*.tmp"))
    # Observable recovery: the transaction's own payload is withdrawn and the
    # pre-existing object is untouched.
    assert not orphan.exists()
    assert (target / "attachments" / "objects" / kept_digest).exists()

    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _stored_max_output_tokens(target) == 1024
    assert (target / "attachments" / "objects" / kept_digest).exists()
    assert not orphan.exists()


def test_adopt_intent_crash_withdraws_published_gc_artefacts_by_durable_plan(tmp_path):
    """B3 late window: a crash after the durable ADOPT_INTENT write must still
    withdraw the published GC lifecycle artefact by the journaled plan; the
    pre-existing tombstone and its payload are never touched and the next
    startup succeeds."""
    portable, target, gc_orphan, gc_kept, kept_digest = _late_window_gc_package(
        tmp_path, "late-adopt"
    )
    _prepare_desktop_root(target)
    _run_restore_child(target, portable, _ADOPT_INTENT_CRASH, 61)

    journal = target / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    plan = record["staged_attachment_state"]
    assert isinstance(plan, dict), (
        "the publication plan must survive the STAGED_VALIDATED journal write"
    )
    assert plan["objects"] == []
    assert [entry[0] for entry in plan["gc"]] == [gc_orphan]
    assert gc_kept not in [entry[0] for entry in plan["gc"]]
    orphan_tombstone = target / "attachments" / "gc" / gc_orphan
    assert orphan_tombstone.exists(), "tombstone should have been published"

    authority = _new_authority(target)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        authority.close()

    assert not journal.exists()
    assert not orphan_tombstone.exists()
    assert (target / "attachments" / "objects" / kept_digest).exists()
    assert (target / "attachments" / "gc" / gc_kept).exists()

    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _store_settings(target) == 1024
    # The kept GC state completed through the existing Phase 6 recovery; the
    # withdrawn artefact never reappears.
    assert not (target / "attachments" / "gc" / gc_orphan).exists()


def test_rolled_back_crash_withdraws_published_payloads_by_durable_plan(tmp_path):
    """B2 rollback window: a crash right after the durable ROLLED_BACK journal
    write must still withdraw the payloads the transaction published before
    adoption, by the journaled plan, so the next clean startup succeeds and
    the rolled-back installation is intact."""
    portable, target, orphan_digest, kept_digest = _late_window_cas_package(
        tmp_path, "late-rollback"
    )
    _prepare_desktop_root(target)
    _run_restore_child(target, portable, _ROLLED_BACK_CRASH, 73)

    journal = target / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ROLLED_BACK"
    plan = record["staged_attachment_state"]
    assert isinstance(plan, dict), (
        "the publication plan must survive every journal write to ROLLED_BACK"
    )
    assert plan["objects"] == [orphan_digest]
    assert kept_digest not in plan["objects"]
    orphan = target / "attachments" / "objects" / orphan_digest
    assert orphan.exists(), "payload should have been published before the crash"

    # The next startup converges the terminal rollback (restart still
    # required) and withdraws the published payloads by the durable plan.
    with pytest.raises(BackupError, match="restart required"):
        build_runtime(target)
    assert not journal.exists()
    txid = str(record["restore_transaction_id"])
    assert not (target / "database" / f".bots5-restore-{txid}.lock").exists()
    assert not list((target / "database").glob(".bots5-restore-journal-*.tmp"))
    assert not (target / "attachments" / "staging" / f"restore-{txid}").exists()
    assert not orphan.exists()
    assert (target / "attachments" / "objects" / kept_digest).exists()

    # The clean restart proceeds normally on the rolled-back installation.
    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _stored_max_output_tokens(target) == 1024
    assert (target / "attachments" / "objects" / kept_digest).exists()
    assert not orphan.exists()


def test_rolled_back_crash_withdraws_published_gc_artefacts_by_durable_plan(tmp_path):
    """B3 rollback window: a crash right after the durable ROLLED_BACK journal
    write must still withdraw the published GC lifecycle artefact by the
    journaled plan; the pre-existing tombstone is never touched and the next
    clean startup succeeds."""
    portable, target, gc_orphan, gc_kept, kept_digest = _late_window_gc_package(
        tmp_path, "late-rollback"
    )
    _prepare_desktop_root(target)
    _run_restore_child(target, portable, _ROLLED_BACK_CRASH, 73)

    journal = target / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ROLLED_BACK"
    plan = record["staged_attachment_state"]
    assert isinstance(plan, dict), (
        "the publication plan must survive every journal write to ROLLED_BACK"
    )
    assert plan["objects"] == []
    assert [entry[0] for entry in plan["gc"]] == [gc_orphan]
    orphan_tombstone = target / "attachments" / "gc" / gc_orphan
    assert orphan_tombstone.exists(), "tombstone should have been published"

    with pytest.raises(BackupError, match="restart required"):
        build_runtime(target)
    assert not journal.exists()
    assert not orphan_tombstone.exists()
    assert (target / "attachments" / "objects" / kept_digest).exists()
    assert (target / "attachments" / "gc" / gc_kept).exists()

    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _store_settings(target) == 1024
    # The kept GC state completed through the existing Phase 6 recovery; the
    # withdrawn artefact never reappears.
    assert not (target / "attachments" / "gc" / gc_orphan).exists()


# ---------------------------------------------------------------------------
# Bounded-repair wave 5a: the journal size cap (I4) is enforced at WRITE time.
# The STAGING authorize check alone lets an authorized journal land within
# _JOURNAL_MAX_BYTES of the cap, after which later phase writes (+9 bytes for
# STAGED_VALIDATED, +17 for RESTORE_ROLLBACK_INTENT) durably publish an
# oversized journal that _read_journal refuses on every later startup — a
# permanent fail-closed wedge.
# ---------------------------------------------------------------------------


def test_write_journal_never_publishes_an_oversized_journal(tmp_path):
    """B4/I4: the size cap is enforced at EVERY journal write, before any byte
    reaches the filesystem.  A record at exactly the cap is publishable and
    readable back (the boundary is precise — no over-refusal); one byte over
    is refused with no journal leaf and no temp leaf, so an oversized journal
    can never be durably published and a later startup can never wedge."""
    from bots5.infrastructure import restore_service as restore_service_module

    cap = restore_service_module._JOURNAL_MAX_BYTES
    root = _make_root(tmp_path, "journal-cap-root", 1024)
    authority = _new_authority(root)
    try:
        service = RestoreService(authority)

        def record_at_size(target: int) -> dict[str, object]:
            record = service._base_record(
                txid=str(Uuid7Factory().new()),
                backup_id=str(Uuid7Factory().new()),
                package_path="/p",
                package_sha256="a" * 64,
                target_db_revision=HEAD,
            )
            base = len(canonical_backup_json(record))
            # The package path is a recorded journal string, so each added
            # character scales the canonical journal size one-for-one.
            record["package_path"] = "/" + "x" * (target - base + 1)
            assert len(canonical_backup_json(record)) == target
            return record

        journal = root / "database" / _RESTORE_JOURNAL

        # Exactly at the cap: publishable, and the reader accepts it.
        record = record_at_size(cap)
        service._write_journal(record)
        assert journal.stat().st_size == cap
        read_back, _raw = service._read_journal()
        assert read_back == record
        service._unlink_journal()
        assert not journal.exists()

        # One byte over the cap: refused BEFORE publication — no temp leaf,
        # no journal leaf, nothing oversized ever on disk.
        record = record_at_size(cap + 1)
        with pytest.raises(BackupError, match="size limit"):
            service._write_journal(record)
        assert not journal.exists()
        assert not list((root / "database").glob(".bots5-restore-journal-*.tmp"))
    finally:
        authority.close()


def _padded_package_copy(tmp_path: Path, package: Path, pad: int, name: str) -> Path:
    """Copy the package to a path carrying ``pad`` extra recorded bytes.

    The journal records the absolute package path verbatim, so nested
    directory components scale the authorized journal size one byte per path
    character, giving byte-level control over the authorized size.
    """
    assert pad >= 2
    parts: list[str] = []
    remaining = pad
    while remaining > 0:
        component = min(remaining - 1, 200)
        parts.append("d" * component)
        remaining -= component + 1
    directory = tmp_path / "outside-padded"
    for part in parts:
        directory = directory / part
    directory.mkdir(parents=True, exist_ok=True)
    portable = directory / name
    shutil.copyfile(package, portable)
    return portable


def _bulk_install_ready_objects(root: Path, payloads: list[bytes]) -> list[str]:
    """Install many canonical payload objects and their ready rows quickly."""
    digests: list[str] = []
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    record = SimpleNamespace(info={})
    try:
        install_transition_guard(connection, record)
        for payload in payloads:
            digest = hashlib.sha256(payload).digest()
            operation = Uuid7Factory().new()
            arm_phase6_blob_transition(
                record,
                digest,
                "",
                "staging",
                operation_id=operation,
                stage_name=operation,
                byte_size=len(payload),
            )
            connection.execute(
                "INSERT INTO attachment_blobs "
                "(digest, byte_size, state, operation_id, stage_name, gc_id, created_at) "
                "VALUES (?, ?, 'staging', ?, ?, NULL, ?)",
                (digest, len(payload), operation, operation, _M5_TIMESTAMP),
            )
            arm_phase6_blob_transition(
                record, digest, "staging", "ready", byte_size=len(payload)
            )
            connection.execute(
                "UPDATE attachment_blobs SET state='ready', operation_id=NULL, "
                "stage_name=NULL WHERE digest=?",
                (digest,),
            )
            leaf = root / "attachments" / "objects" / digest.hex()
            leaf.write_bytes(payload)
            os.chmod(leaf, 0o600)
            digests.append(digest.hex())
        connection.commit()
    finally:
        connection.close()
    return digests


def test_publication_plan_over_the_journal_ceiling_is_refused_before_staging(
    tmp_path, monkeypatch
):
    """B4 end to end: a package whose publication plan lands the authorized
    journal inside the hostile band ``(cap - 17, cap]`` — which the authorize
    used to accept and a later phase write then published OVER the cap — is
    refused before any live mutation, nothing oversized is ever durably
    published, and the next startup converges cleanly (no wedge)."""
    from bots5.infrastructure import restore_service as restore_service_module

    cap = restore_service_module._JOURNAL_MAX_BYTES
    # The widest later phase state is +17 bytes over the STAGING
    # authorization (RESTORE_ROLLBACK_INTENT, sequence 10 vs 3), so anything
    # authorized above cap - 17 used to publish an oversized journal later.
    ceiling_slack = 17
    payload_count = 3860
    payloads = [
        f"bulk-payload-{index:05d}-".encode() + b"p" * 16
        for index in range(payload_count)
    ]
    source = _make_root(tmp_path, "journal-cap-source", 2222)
    _bulk_install_ready_objects(source, payloads)
    package_path, _receipt = _capture_from_source(source)
    portable_short = _portable_copy(tmp_path, package_path, "sized.botsbackup")

    target = _make_root(tmp_path, "journal-cap-target", 1024)
    _prepare_desktop_root(target)

    # Measure the exact authorized journal size on THIS root: spy on the
    # authorization (the authorized record is the caller's record plus the
    # plan) and stop the transaction at that write, before it publishes
    # anything.
    measured: dict[str, int] = {}
    original_authorize = (
        restore_service_module.RestoreService._authorize_staged_attachment_state
    )

    def spy(self, record, plan):
        probe = dict(record)
        probe["staged_attachment_state"] = plan
        measured["authorized_size"] = len(canonical_backup_json(probe))
        return original_authorize(self, record, plan)

    staging_writes = 0

    def hook(point: str) -> None:
        nonlocal staging_writes
        if point == "before-journal-write-STAGING":
            staging_writes += 1
            if staging_writes == 2:
                # The second STAGING write is the authorization itself.
                raise RuntimeError("measurement complete")

    monkeypatch.setattr(
        restore_service_module.RestoreService,
        "_authorize_staged_attachment_state",
        spy,
    )
    restore_service_module._TEST_FAULT_HOOK = hook
    try:
        authority = _new_authority(target)
        try:
            with pytest.raises(RuntimeError, match="measurement complete"):
                RestoreService(authority).restore(portable_short)
        finally:
            authority.close()
    finally:
        restore_service_module._TEST_FAULT_HOOK = None
    assert "authorized_size" in measured

    # Converge the measurement transaction away (scratch abort); the root is
    # pristine again with identical directory identities, so the measured
    # size transfers exactly to the padded attempt below.
    authority = _new_authority(target)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        authority.close()
    database = target / "database"
    assert not (database / _RESTORE_JOURNAL).exists()
    assert list((target / "attachments" / "objects").iterdir()) == []

    authorized_size = measured["authorized_size"]
    assert authorized_size <= cap - ceiling_slack, (
        "payload_count must leave room to pad the path into the hostile band"
    )
    pad = (cap - 8) - authorized_size
    assert 2 <= pad <= 3000
    portable_padded = _padded_package_copy(
        tmp_path, package_path, pad, "sized.botsbackup"
    )

    # The regression: the authorized journal (cap - 8) is still within the
    # cap the plain authorize check used to accept, yet the restore is
    # refused because the transaction's journal size CEILING (authorized
    # size + the widest later phase delta) exceeds the cap.  The refusal
    # happens before any live mutation and can never wedge startup.
    authority = _new_authority(target)
    try:
        with pytest.raises(BackupError, match="journal size budget"):
            RestoreService(authority).restore(portable_padded)
        # Refused before any live mutation: the CAS is untouched and the
        # durable journal (still at STAGING) is within the cap.
        assert list((target / "attachments" / "objects").iterdir()) == []
        journal = database / _RESTORE_JOURNAL
        assert journal.exists()
        assert journal.stat().st_size <= cap
        assert not list(database.glob(".bots5-restore-journal-*.tmp"))
        # The refused transaction converges as scratch on reconciliation.
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        _close_tolerant(authority)
    assert not journal.exists()
    assert list((target / "attachments" / "objects").iterdir()) == []
    assert not list(database.glob(".bots5-restore-*.tmp"))

    # No wedge: the next startup is a normal, clean startup.
    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert _stored_max_output_tokens(target) == 1024
    assert list((target / "attachments" / "objects").iterdir()) == []


# ---------------------------------------------------------------------------
# M3: RestoreStartupCoordinator startup interception, receipt finalisation
# and the D-D=D.2 destructive-override surface.
# ---------------------------------------------------------------------------


def test_startup_skipping_restore_interception_with_pending_journal_fails_closed(
    tmp_path, restore_environment, monkeypatch
):
    """Sealed startup oracle (I12): a startup can never slip past the restore
    interception while a pending restore journal exists.  With an
    unattributable (W10) journal, build_runtime must fail closed,
    ``open_store()`` must never be reached, the ambiguous bytes must be
    preserved, and the live installation must stay untouched.
    """
    env = restore_environment
    before = _live_sha(env.root)
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    torn = journal.read_bytes()[:-2]
    journal.write_bytes(torn)

    opened: list[bool] = []

    def refusing_open_store(self):
        opened.append(True)
        raise AssertionError("open_store was reached despite the pending journal")

    monkeypatch.setattr(DataRootAuthority, "open_store", refusing_open_store)
    with pytest.raises(BackupUnclassifiedState, match="torn or non-canonical"):
        build_runtime(env.root)
    # The interception stopped startup before the store could be opened or a
    # fresh installation fabricated.
    assert opened == []
    # W10: ambiguous bytes preserved, never rewritten or laundered.
    assert journal.read_bytes() == torn
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024


def test_normal_startup_without_journal_is_unchanged(
    tmp_path, restore_environment, startup_order
):
    """Normal startup with no restore journal behaves exactly as before:
    the coordinator no-ops, the store opens, and no restore state exists."""
    env = restore_environment
    before = _live_sha(env.root)
    runtime = build_runtime(env.root)
    try:
        assert startup_order == ["reconcile", "open_store"]
        assert runtime.session.backend_id == "fake"
    finally:
        _close_runtime(runtime)
    database = env.root / "database"
    assert not (database / _RESTORE_JOURNAL).exists()
    assert not (database / _RESTORE_RECEIPT).exists()
    assert not list(database.glob(".bots5-restore-*"))
    assert list((env.root / "retained-installations").iterdir()) == []
    assert _live_sha(env.root) == before
    assert _store_settings(env.root) == 1024


def test_startup_reconciles_pending_journal_before_open_store(
    tmp_path, restore_environment, startup_order
):
    """The interception runs between acquire()/ensure_non_authoritative()
    and open_store(): an interrupted restore is reconciled from its journal
    before the store can be opened (W4 abort keeps the live installation)."""
    env = restore_environment
    before = _live_sha(env.root)
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    txid = str(record["restore_transaction_id"])
    runtime = build_runtime(env.root)
    try:
        assert startup_order == ["reconcile", "open_store"]
    finally:
        _close_runtime(runtime)
    database = env.root / "database"
    assert not journal.exists()
    assert not (database / f".bots5-restore-{txid}.candidate.sqlite3").exists()
    assert not (database / f".bots5-restore-{txid}.lock").exists()
    assert not (env.root / "attachments" / "staging" / f"restore-{txid}").exists()
    # W4 keeps the recorded preservation artefact (only unrecorded W2
    # scratch is discarded).
    assert (
        env.root / "retained-installations" / f"{txid}-{env.backup_id}.sqlite3"
    ).exists()
    assert _live_sha(env.root) == before
    assert _store_settings(env.root) == 1024


def test_rolled_back_restore_blocks_startup_until_clean_restart(
    tmp_path, restore_environment
):
    """W7/W9: a rollback completed by the interception reports restart
    required and stops this startup; the next startup is clean."""
    env = restore_environment
    before = _live_sha(env.root)
    journal = env.root / "database" / _RESTORE_JOURNAL
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _ROLLBACK_INTENT_CRASH, 63)
    assert json.loads(journal.read_text("utf-8"))["phase"] == "RESTORE_ROLLBACK_INTENT"
    with pytest.raises(BackupError, match="restart required"):
        build_runtime(env.root)
    # W7: the interception completed the rollback deterministically; the
    # terminal ROLLED_BACK marker remains for the restart acknowledgment.
    assert json.loads(journal.read_text("utf-8"))["phase"] == "ROLLED_BACK"
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024
    # W9: the next startup verifies the pre-restore identity, unlinks the
    # journal, and still refuses so the operator observes a clean restart.
    with pytest.raises(BackupError, match="restart required"):
        build_runtime(env.root)
    assert not journal.exists()
    # The clean restart proceeds normally on the rolled-back installation.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert _store_settings(env.root) == 1024


def test_startup_finalises_receipt_once_adopted_installation_reaches_head(tmp_path):
    """B.1/section 3.1: the receipt written at RESTORE_COMMITTED with
    ``post_adoption_migration="required"`` is finalised by the startup
    coordinator on the first startup that observes the adopted installation
    at the application head.  Finalisation is a receipt-record update only."""
    package_path, receipt = _capture_package(
        tmp_path, "older-source", 2222, revision=PRIOR_HEAD
    )
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    env = SimpleNamespace(
        root=_make_root(tmp_path, "older-target", 1024),
        package=portable,
        backup_id=str(receipt["backup_id"]),
        artifact_sha256=str(receipt["artifact_sha256"]),
    )
    authority = _new_authority(env.root)
    try:
        written = RestoreService(authority).restore(env.package)
    finally:
        authority.close()
    assert written["post_adoption_migration"] == "required"
    assert written["target_revision_at_commit"] == PRIOR_HEAD
    assert written["target_revision_after_migration"] is None
    receipt_file = env.root / "database" / _RESTORE_RECEIPT

    # Startup 1: the adopted installation is below head, so the coordinator
    # defers finalisation and normal startup migrates it forward (B.1).
    runtime = build_runtime(env.root)
    try:
        pending = json.loads(receipt_file.read_text("utf-8"))
        assert pending["post_adoption_migration"] == "required"
        assert pending["target_revision_after_migration"] is None
    finally:
        _close_runtime(runtime)
    assert _stored_max_output_tokens(env.root) == 2222
    at_head = _live_sha(env.root)

    # Startup 2: the adopted installation is observed at head → finalise.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    finalised = json.loads(receipt_file.read_text("utf-8"))
    assert set(finalised) == RECEIPT_FIELDS
    assert finalised["post_adoption_migration"] == "completed"
    assert finalised["target_revision_after_migration"] == HEAD
    assert finalised["target_revision_at_commit"] == PRIOR_HEAD
    assert finalised["transaction_id"] == written["transaction_id"]
    assert finalised["backup_id"] == env.backup_id
    assert finalised["package_sha256"] == env.artifact_sha256
    assert finalised["outcome"] == "RESTORED"
    assert receipt_file.read_bytes() == canonical_backup_json(finalised)
    # No journal reopen, no lifecycle state, no authoritative mutation.
    database = env.root / "database"
    assert not (database / _RESTORE_JOURNAL).exists()
    assert not list(database.glob(".bots5-restore-*.tmp"))
    assert not list(database.glob(".bots5-restore-*.lock"))
    assert _live_sha(env.root) == at_head

    # Startup 3: finalisation is terminal and idempotent.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert json.loads(receipt_file.read_text("utf-8")) == finalised


def test_startup_leaves_not_required_receipt_untouched(tmp_path, restore_environment):
    """A ``not_required`` receipt is already terminal at commit; the
    coordinator never rewrites it and never records a migration revision."""
    env = restore_environment
    authority = _new_authority(env.root)
    try:
        written = RestoreService(authority).restore(env.package)
    finally:
        authority.close()
    assert written["post_adoption_migration"] == "not_required"
    receipt_file = env.root / "database" / _RESTORE_RECEIPT
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert json.loads(receipt_file.read_text("utf-8")) == written
    assert receipt_file.read_bytes() == canonical_backup_json(written)
    assert _store_settings(env.root) == 2222


def test_destructive_override_surface_is_explicit_and_off_by_default():
    """D-D=D.2 surface: the override is a keyword-only build_runtime
    authorization that defaults OFF, is never silently implied, and is never
    consulted by the normal restore path."""
    parameter = inspect.signature(build_runtime).parameters[
        "destructive_restore_override"
    ]
    assert parameter.default is False
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    restore_parameters = inspect.signature(RestoreService.restore).parameters
    assert not any("override" in name for name in restore_parameters)
    assert not any("destructive" in name for name in restore_parameters)
    # The coordinator refuses anything but an explicit bool authorization.
    assert RestoreStartupCoordinator(object()).destructive_override_authorized is False
    with pytest.raises(TypeError, match="explicit bool"):
        RestoreStartupCoordinator(object(), destructive_override=1)


@pytest.mark.parametrize(
    "condition",
    [
        "operator_authorization",
        "source_verification",
        "rollback_capable",
        "target_identity",
    ],
)
def test_destructive_override_refused_when_any_condition_unmet(
    tmp_path, restore_environment, condition
):
    """I15: the override is refused unless ALL FOUR validity conditions
    hold; refusals mutate nothing, and authority/target-identity uncertainty
    fails closed even when the override is supplied."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    if condition == "rollback_capable":
        _run_restore_child(env.root, env.package, _PRESERVED_CRASH, 65)
    else:
        _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    override = condition != "operator_authorization"
    if condition == "source_verification":
        # The Backup v1 source no longer verifies: condition 2 is unmet.
        env.package.unlink()
    if condition == "target_identity":
        # The root topology legitimately changed between the transaction and
        # this startup: a new top-level directory appeared under the data
        # root, so the recorded root identity no longer matches the
        # acquiring authority — condition 1 is unmet.  This must fail closed
        # even though the operator override is supplied.
        (env.root / "unattributable-topology").mkdir(mode=0o700)

    authority = _new_authority(env.root)
    try:
        coordinator = RestoreStartupCoordinator(
            authority, destructive_override=override
        )
        if condition == "target_identity":
            with pytest.raises(BackupUnclassifiedState, match="no longer matches"):
                coordinator.evaluate_destructive_override()
            assert authority.poison_pending
        else:
            with pytest.raises(BackupError, match="destructive override refused"):
                coordinator.evaluate_destructive_override()
            assert not authority.poison_pending
    finally:
        _close_tolerant(authority)
    # A refusal never mutates the recorded transaction.
    assert journal.exists()
    assert json.loads(journal.read_text("utf-8")) == record


def test_destructive_override_granted_only_when_all_conditions_hold(
    tmp_path, restore_environment
):
    """With the target identity proven, the source independently verified,
    no durable rollback source in existence (journal at PRESERVING) and the
    explicit operator authorization supplied, the gate grants — as read-only
    evidence that the M5 destructive path must re-prove under its lock."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    authority = _new_authority(env.root)
    try:
        denied = RestoreStartupCoordinator(authority)
        with pytest.raises(
            BackupError, match="not explicitly authorized"
        ):
            denied.evaluate_destructive_override()
        assert not authority.poison_pending
        granted = RestoreStartupCoordinator(
            authority, destructive_override=True
        ).evaluate_destructive_override()
        assert granted["granted"] is True
        assert granted["journal_phase"] == "PRESERVING"
        assert granted["conditions"] == {
            "target_identity_proven": True,
            "source_verified": True,
            "not_rollback_capable": True,
            "operator_authorized": True,
        }
        assert granted["backup_id"] == env.backup_id
        assert granted["package_sha256"] == env.artifact_sha256
        assert not authority.poison_pending
    finally:
        authority.close()
    # Evaluation is read-only: the transaction stays exactly as recorded.
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "PRESERVING"
    assert record["preservation_record"] is None


def test_first_ever_startup_creates_data_root_owner_only(tmp_path: Path) -> None:
    """A brand-new data root must be created 0o700, never with umask defaults.

    Regression found during M3 verification: ``build_runtime`` prepares the XDG
    application directories under the data root *before* the authority acquires
    it (so the root topology is stable for journal identity attribution).  On a
    genuine first run that made ``mkdir(parents=True)`` create the root itself
    with umask-derived permissions, and acquisition then failed closed with
    "data-root capability has unsafe owner or permissions" -- i.e. B.O.T.S.
    could not start at all on a fresh installation.

    The wider suite did not catch this because every existing fixture
    pre-creates the root with ``mkdir(mode=0o700)``.
    """
    root = tmp_path / "brand-new-root"
    assert not root.exists()
    runtime = build_runtime(root, backend="fake")
    try:
        assert root.is_dir()
        assert (os.stat(root).st_mode & 0o777) == 0o700
    finally:
        _close_runtime(runtime)


# ---------------------------------------------------------------------------
# M4: the adjudicated D-B=B.1 adopt-then-migrate handoff and the
# failed_rolled_back receipt state (RESTORE_STATE_MACHINE.md section 3.1).
# ---------------------------------------------------------------------------


def _canonical_revision(root: Path) -> str:
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    try:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    return str(rows[0][0])


def test_out_of_chain_backup_refused_before_any_live_mutation(tmp_path):
    """D-B=B.1 compatibility gate: a backup whose revision is outside
    ``_MIGRATION_CHAIN`` (newer than the application head) is refused with
    ``BackupUnsupported`` before any live mutation happens."""
    newer = "0013_phase10_future_revision"
    source_root = _make_root(tmp_path, "newer-source", 777)
    connection = sqlite3.connect(source_root / "database" / "state.sqlite3")
    try:
        connection.execute("UPDATE alembic_version SET version_num = ?", (newer,))
        connection.commit()
    finally:
        connection.close()
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    package = outside / "newer.botsbackup"
    authority = _new_authority(source_root)
    try:
        authority._claim_database()
        capture = RootedBackupCaptureAdapter(
            authority,
            None,
            resolve_app_paths(source_root),
            BackupZipPackageAdapter(),
            data_root_is_override=True,
        )
        staged = capture.capture_under_fence(
            progress=BackupProgress(BackupProgressState.ACQUIRING_FENCE, "op"),
            destination=package,
            staging_path=outside / ".newer.staging",
        ).source
        BackupFilePublicationAdapter().publish(
            staging_path=outside / ".newer.staging",
            destination=package,
            overwrite=False,
            backup_id=str(staged.manifest.backup_id),
        )
    finally:
        authority.close()
    assert package.exists()

    target = _make_root(tmp_path, "out-of-chain-target", 1024)
    before = _live_sha(target)
    authority = _new_authority(target)
    try:
        with pytest.raises(BackupUnsupported):
            RestoreService(authority).restore(package)
        database = target / "database"
        assert not (database / _RESTORE_JOURNAL).exists()
        assert not list(database.glob(".bots5-restore-*"))
        assert list((target / "retained-installations").iterdir()) == []
        assert not authority.poison_pending
    finally:
        authority.close()
    assert _live_sha(target) == before
    assert _stored_max_output_tokens(target) == 1024


def test_adopt_then_migrate_handoff_is_proven_end_to_end(tmp_path):
    """D-B=B.1 (sealed section 6): a supported OLDER backup is adopted at
    exactly its captured revision, a SUBSEQUENT normal startup migrates it
    forward through the existing migration machinery, and a LATER startup
    finalises the receipt to ``completed``.  The D-A preserved installation
    remains untouched throughout."""
    package_path, receipt = _capture_package(
        tmp_path, "older-source", 2222, revision=PRIOR_HEAD
    )
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    root = _make_root(tmp_path, "adopt-then-migrate-target", 1024)
    pre_restore = _live_sha(root)
    authority = _new_authority(root)
    try:
        written = RestoreService(authority).restore(portable)
    finally:
        authority.close()
    assert written["post_adoption_migration"] == "required"
    assert written["target_revision_at_commit"] == PRIOR_HEAD
    assert written["target_revision_after_migration"] is None
    assert written["migration_recovery_reference"] is None
    # Adoption is exact: the canonical database IS the captured state at its
    # captured revision.
    assert _canonical_revision(root) == PRIOR_HEAD
    assert _stored_max_output_tokens(root) == 2222
    artefact = (
        root
        / "retained-installations"
        / f"{written['transaction_id']}-{receipt['backup_id']}.sqlite3"
    )
    assert artefact.exists()
    preserved = hashlib.sha256(artefact.read_bytes()).hexdigest()
    assert preserved == pre_restore

    # Subsequent startup: the adopted installation is migrated forward
    # through the normal machinery; the receipt stays pending; the preserved
    # installation is untouched.
    runtime = build_runtime(root)
    _close_runtime(runtime)
    assert _canonical_revision(root) == HEAD
    assert _stored_max_output_tokens(root) == 2222
    pending = json.loads(
        (root / "database" / _RESTORE_RECEIPT).read_text("utf-8")
    )
    assert pending["post_adoption_migration"] == "required"
    assert pending["target_revision_after_migration"] is None
    assert not (root / "database" / _RESTORE_JOURNAL).exists()
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []
    assert hashlib.sha256(artefact.read_bytes()).hexdigest() == preserved

    # Later startup: the coordinator finalises the receipt.
    runtime = build_runtime(root)
    _close_runtime(runtime)
    receipt_file = root / "database" / _RESTORE_RECEIPT
    finalised = json.loads(receipt_file.read_text("utf-8"))
    assert set(finalised) == RECEIPT_FIELDS
    assert finalised["post_adoption_migration"] == "completed"
    assert finalised["target_revision_after_migration"] == HEAD
    assert finalised["target_revision_at_commit"] == PRIOR_HEAD
    assert finalised["migration_recovery_reference"] is None
    assert finalised["transaction_id"] == written["transaction_id"]
    assert finalised["backup_id"] == str(receipt["backup_id"])
    assert finalised["package_sha256"] == receipt["artifact_sha256"]
    assert receipt_file.read_bytes() == canonical_backup_json(finalised)
    assert hashlib.sha256(artefact.read_bytes()).hexdigest() == preserved


def test_failed_post_adoption_migration_records_failed_rolled_back(tmp_path):
    """RESTORE_STATE_MACHINE.md section 3.1: when the deferred post-adoption
    forward migration (D-B=B.1) fails and rolls back, a subsequent startup
    attributes the failure from the durable migration recovery journal and
    records ``post_adoption_migration="failed_rolled_back"`` together with
    the migration recovery reference.  The terminal state is never rewritten
    by a later successful migration."""
    package_path, receipt = _capture_package(
        tmp_path, "older-source", 2222, revision=PRIOR_HEAD
    )
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    root = _make_root(tmp_path, "migrate-failure-target", 1024)
    authority = _new_authority(root)
    try:
        written = RestoreService(authority).restore(portable)
    finally:
        authority.close()
    assert written["post_adoption_migration"] == "required"
    artefact = (
        root
        / "retained-installations"
        / f"{written['transaction_id']}-{receipt['backup_id']}.sqlite3"
    )
    preserved = hashlib.sha256(artefact.read_bytes()).hexdigest()

    # The subsequent startup's post-adoption migration fails and rolls back
    # through the normal migration machine, then dies before the terminal
    # cleanup removes the durable migration recovery journal.
    _prepare_desktop_root(root)
    _run_restore_child(root, portable, _MIGRATION_ROLLBACK_CRASH, 66)
    journal_path = root / _MIGRATION_JOURNAL
    record = json.loads(journal_path.read_text("utf-8"))
    assert record["phase"] == "ROLLED_BACK"
    assert record["restored"] is True
    assert record["source_kind"] == "EXISTING"
    assert record["target_revision"] == HEAD
    assert record["expected_start_revision"] == PRIOR_HEAD
    assert record["validation_failure_class"]
    migration_txid = str(record["transaction_id"])
    assert _canonical_revision(root) == PRIOR_HEAD
    receipt_file = root / "database" / _RESTORE_RECEIPT
    assert json.loads(receipt_file.read_text("utf-8"))[
        "post_adoption_migration"
    ] == "required"

    # The next startup attributes the failure and records the terminal
    # failed_rolled_back state; the migration machine then converges its own
    # journal and requires a clean restart.
    with pytest.raises(RuntimeError, match="restart is required"):
        build_runtime(root)
    failed = json.loads(receipt_file.read_text("utf-8"))
    assert set(failed) == RECEIPT_FIELDS
    assert failed["post_adoption_migration"] == "failed_rolled_back"
    assert failed["migration_recovery_reference"] == migration_txid
    assert failed["target_revision_after_migration"] is None
    assert failed["target_revision_at_commit"] == PRIOR_HEAD
    assert failed["transaction_id"] == written["transaction_id"]
    assert failed["backup_id"] == str(receipt["backup_id"])
    assert receipt_file.read_bytes() == canonical_backup_json(failed)
    # The migration machine converged its own journal; the failure evidence
    # lives on in the receipt record only.
    assert not journal_path.exists()
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []
    assert _canonical_revision(root) == PRIOR_HEAD
    assert hashlib.sha256(artefact.read_bytes()).hexdigest() == preserved

    # A later startup retries the migration through the normal machinery;
    # the terminal failed_rolled_back receipt is never rewritten.
    runtime = build_runtime(root)
    _close_runtime(runtime)
    assert json.loads(receipt_file.read_text("utf-8")) == failed
    assert _canonical_revision(root) == HEAD
    assert _stored_max_output_tokens(root) == 2222


def test_ordinary_failed_post_adoption_migration_records_failed_rolled_back(tmp_path):
    """The ORDINARY (non-crash) failed post-adoption forward migration
    (D-B=B.1): the failed run completes its rollback and, under the approved
    migration_runner terminal-cleanup deferral, leaves the durable
    ``ROLLED_BACK`` journal plus the rolled-back private candidate and
    recovery leaves in place.  The next startup attributes the failure to
    this receipt — ``post_adoption_migration="failed_rolled_back"`` with the
    migration recovery reference — the migration machine then converges its
    own journal and requires a clean restart, and the receipt is never left
    permanently ``required``.  The D-A preserved installation is untouched
    throughout."""
    package_path, receipt = _capture_package(
        tmp_path, "older-source", 2222, revision=PRIOR_HEAD
    )
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    root = _make_root(tmp_path, "ordinary-failure-target", 1024)
    authority = _new_authority(root)
    try:
        written = RestoreService(authority).restore(portable)
    finally:
        authority.close()
    assert written["post_adoption_migration"] == "required"
    artefact = (
        root
        / "retained-installations"
        / f"{written['transaction_id']}-{receipt['backup_id']}.sqlite3"
    )
    preserved = hashlib.sha256(artefact.read_bytes()).hexdigest()

    # The subsequent startup's post-adoption migration fails and rolls back
    # through the normal migration machinery and the run ENDS ORDINARILY
    # (no crash, no injected death): the deferred cleanup keeps the durable
    # failure evidence in place.
    _prepare_desktop_root(root)
    _run_restore_child(root, portable, _ORDINARY_MIGRATION_FAILURE, 81)
    journal_path = root / _MIGRATION_JOURNAL
    record = json.loads(journal_path.read_text("utf-8"))
    assert record["phase"] == "ROLLED_BACK"
    assert record["restored"] is True
    assert record["source_kind"] == "EXISTING"
    assert record["target_revision"] == HEAD
    assert record["expected_start_revision"] == PRIOR_HEAD
    assert record["validation_failure_class"]
    migration_txid = str(record["transaction_id"])
    assert _canonical_revision(root) == PRIOR_HEAD
    # The deferral: the rolled-back private candidate and the recovery leaves
    # are still present for the next startup's convergence to attribute and
    # remove.
    assert str(record["candidate_leaf"]) in [
        entry.name for entry in (root / "database" / "migration").iterdir()
    ]
    assert list((root / "recovery").iterdir())
    receipt_file = root / "database" / _RESTORE_RECEIPT
    assert json.loads(receipt_file.read_text("utf-8"))[
        "post_adoption_migration"
    ] == "required"

    # The next startup finalises the receipt to the terminal failure state
    # before the existing ROLLED_BACK convergence performs the deferred
    # terminal cleanup and requires a clean restart.
    with pytest.raises(RuntimeError, match="restart is required"):
        build_runtime(root)
    failed = json.loads(receipt_file.read_text("utf-8"))
    assert set(failed) == RECEIPT_FIELDS
    assert failed["post_adoption_migration"] == "failed_rolled_back"
    assert failed["migration_recovery_reference"] == migration_txid
    assert failed["target_revision_after_migration"] is None
    assert failed["target_revision_at_commit"] == PRIOR_HEAD
    assert failed["transaction_id"] == written["transaction_id"]
    assert failed["backup_id"] == str(receipt["backup_id"])
    assert receipt_file.read_bytes() == canonical_backup_json(failed)
    # The convergence attributed and removed every deferred private leaf and
    # the journal; the failure evidence lives on in the receipt record only.
    assert not journal_path.exists()
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []
    assert _canonical_revision(root) == PRIOR_HEAD
    assert hashlib.sha256(artefact.read_bytes()).hexdigest() == preserved

    # A later startup retries the migration through the normal machinery;
    # the terminal failed_rolled_back receipt is never rewritten.
    runtime = build_runtime(root)
    _close_runtime(runtime)
    assert json.loads(receipt_file.read_text("utf-8")) == failed
    assert receipt_file.read_bytes() == canonical_backup_json(failed)
    assert _canonical_revision(root) == HEAD
    assert _stored_max_output_tokens(root) == 2222
    assert hashlib.sha256(artefact.read_bytes()).hexdigest() == preserved


@pytest.mark.parametrize("tamper", ["journal_source", "canonical_database"])
def test_unattributable_rollback_evidence_fails_closed_and_leaves_receipt(
    tmp_path, tamper
):
    """Fail-closed on ambiguity: a rolled-back migration journal whose
    evidence does not attribute the failure to THIS receipt's
    post-adoption migration never produces a ``failed_rolled_back`` record.
    Finalisation raises fail-closed and the receipt stays byte-identical."""
    package_path, receipt = _capture_package(
        tmp_path, "older-source", 2222, revision=PRIOR_HEAD
    )
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    root = _make_root(tmp_path, f"ambiguous-target-{tamper}", 1024)
    authority = _new_authority(root)
    try:
        written = RestoreService(authority).restore(portable)
    finally:
        authority.close()
    _prepare_desktop_root(root)
    _run_restore_child(root, portable, _MIGRATION_ROLLBACK_CRASH, 66)
    journal_path = root / _MIGRATION_JOURNAL
    record = json.loads(journal_path.read_text("utf-8"))
    assert record["phase"] == "ROLLED_BACK"
    if tamper == "journal_source":
        # The journal is mechanically consistent but describes a migration
        # that started from a different revision than the one this receipt
        # adopted: the failure cannot be attributed to this receipt.
        record["expected_start_revision"] = "0010_phase7_search_navigation"
        record["recovered_revision"] = "0010_phase7_search_navigation"
        journal_path.write_bytes(canonical_backup_json(record))
    else:
        # The canonical database is no longer the journal's recorded
        # pre-migration source: the rollback evidence is unattributable.
        replacement = _seed_database(
            tmp_path, f"replacement-{tamper}", 9999, revision=PRIOR_HEAD
        )
        os.chmod(replacement, 0o600)
        os.replace(replacement, root / "database" / "state.sqlite3")

    receipt_file = root / "database" / _RESTORE_RECEIPT
    receipt_bytes = receipt_file.read_bytes()
    journal_bytes = journal_path.read_bytes()
    with pytest.raises(BackupUnclassifiedState, match="cannot be attributed"):
        build_runtime(root)
    assert receipt_file.read_bytes() == receipt_bytes
    unchanged = json.loads(receipt_file.read_text("utf-8"))
    assert unchanged["post_adoption_migration"] == "required"
    assert unchanged["migration_recovery_reference"] is None
    assert unchanged["transaction_id"] == written["transaction_id"]
    # The ambiguous evidence itself is preserved untouched.
    assert journal_path.read_bytes() == journal_bytes
    # The failure is deterministic: the next startup fails closed again.
    with pytest.raises(BackupUnclassifiedState, match="cannot be attributed"):
        build_runtime(root)
    assert receipt_file.read_bytes() == receipt_bytes


def test_unconverged_migration_journal_defers_completed_finalisation(tmp_path):
    """Anti-fabrication (section 3.1): with an unconverged migration journal
    present (crash after promotion, before promoted validation), the
    observed head revision is the unvalidated candidate — not a
    machine-validated completion.  The coordinator defers finalisation and
    the receipt stays ``required``; the migration machine converges in
    ``open_store()``; a later startup finalises ``completed``."""
    package_path, receipt = _capture_package(
        tmp_path, "older-source", 2222, revision=PRIOR_HEAD
    )
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    root = _make_root(tmp_path, "unconverged-target", 1024)
    authority = _new_authority(root)
    try:
        written = RestoreService(authority).restore(portable)
    finally:
        authority.close()
    assert written["post_adoption_migration"] == "required"

    _prepare_desktop_root(root)
    _run_restore_child(root, portable, _MIGRATION_PROMOTED_CRASH, 68)
    journal_path = root / _MIGRATION_JOURNAL
    record = json.loads(journal_path.read_text("utf-8"))
    assert record["phase"] == "PROMOTED"
    assert _canonical_revision(root) == HEAD
    receipt_file = root / "database" / _RESTORE_RECEIPT
    assert json.loads(receipt_file.read_text("utf-8"))[
        "post_adoption_migration"
    ] == "required"

    # Startup 2: the coordinator defers (never records "completed" from the
    # unvalidated candidate) and the migration machine converges forward.
    runtime = build_runtime(root)
    _close_runtime(runtime)
    assert json.loads(receipt_file.read_text("utf-8"))[
        "post_adoption_migration"
    ] == "required"
    assert json.loads(receipt_file.read_text("utf-8"))[
        "migration_recovery_reference"
    ] is None
    assert not journal_path.exists()
    assert list((root / "database" / "migration").iterdir()) == []
    assert _canonical_revision(root) == HEAD

    # Startup 3: the machine is converged; the receipt is finalised.
    runtime = build_runtime(root)
    _close_runtime(runtime)
    finalised = json.loads(receipt_file.read_text("utf-8"))
    assert finalised["post_adoption_migration"] == "completed"
    assert finalised["target_revision_after_migration"] == HEAD
    assert finalised["target_revision_at_commit"] == PRIOR_HEAD
    assert finalised["transaction_id"] == written["transaction_id"]
    assert _stored_max_output_tokens(root) == 2222


# ---------------------------------------------------------------------------
# M5: D-F=F.2 restored-GC precedence, D-D=D.2 destructive-override execution,
# D-C=C.2 topology portability, D-A=A.2 retention operator consequence.
# ---------------------------------------------------------------------------


_M5_TIMESTAMP = "2026-09-26T00:00:00.000Z"


def _install_blob_row(
    root: Path,
    payload: bytes,
    *,
    state: str,
    gc_id: str | None = None,
    operation_id: str | None = None,
) -> str:
    """Insert one attachment_blobs row through the guarded state transitions."""
    assert state in {"staging", "ready", "deleting"}
    digest = hashlib.sha256(payload).digest()
    operation = operation_id or Uuid7Factory().new()
    record = SimpleNamespace(info={})
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    try:
        install_transition_guard(connection, record)
        arm_phase6_blob_transition(
            record,
            digest,
            "",
            "staging",
            operation_id=operation,
            stage_name=operation,
            byte_size=len(payload),
        )
        connection.execute(
            "INSERT INTO attachment_blobs "
            "(digest, byte_size, state, operation_id, stage_name, gc_id, created_at) "
            "VALUES (?, ?, 'staging', ?, ?, NULL, ?)",
            (digest, len(payload), operation, operation, _M5_TIMESTAMP),
        )
        if state == "staging":
            connection.commit()
            return digest.hex()
        arm_phase6_blob_transition(
            record, digest, "staging", "ready", byte_size=len(payload)
        )
        connection.execute(
            "UPDATE attachment_blobs SET state='ready', operation_id=NULL, "
            "stage_name=NULL WHERE digest=?",
            (digest,),
        )
        if state == "deleting":
            assert gc_id is not None
            arm_phase6_blob_transition(
                record,
                digest,
                "ready",
                "deleting",
                gc_id=gc_id,
                byte_size=len(payload),
            )
            connection.execute(
                "UPDATE attachment_blobs SET state='deleting', gc_id=? WHERE digest=?",
                (gc_id, digest),
            )
        connection.commit()
    finally:
        connection.close()
    return digest.hex()


def _install_object(root: Path, payload: bytes) -> str:
    """Install one canonical object payload into the root's CAS."""
    digest_hex = hashlib.sha256(payload).hexdigest()
    leaf = root / "attachments" / "objects" / digest_hex
    leaf.write_bytes(payload)
    os.chmod(leaf, 0o600)
    return digest_hex


def _install_gc_artifact(root: Path, gc_id: str, content: bytes) -> None:
    leaf = root / "attachments" / "gc" / gc_id
    leaf.write_bytes(content)
    os.chmod(leaf, 0o600)


def _capture_from_source(root: Path) -> tuple[Path, dict[str, object]]:
    """Capture one real Backup v1 from an already-prepared live root."""
    authority = _new_authority(root)
    try:
        authority._claim_database()
        whole = create_migration_recovery_point(authority, Uuid7Factory().new())
    finally:
        authority.close()
    receipt = whole["verification_receipt"]
    return root / "recovery" / str(whole["published_leaf"]), receipt


def _portable_copy(tmp_path: Path, package_path: Path, name: str) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    portable = outside / name
    shutil.copyfile(package_path, portable)
    return portable


def _deleting_blob_source_root(
    tmp_path: Path,
    name: str,
    payload: bytes,
    *,
    gc_artifact: bytes | None,
    gc_id: str | None = None,
) -> tuple[Path, str, str]:
    """A live root whose attachment lifecycle is mid-GC: deleting row + payload.

    ``gc_artifact`` is the exact bytes placed at ``attachments/gc/<gc_id>``
    (a valid tombstone, garbage, or ``None`` for no artefact at all).
    """
    root = _make_root(tmp_path, name, 2222)
    gc_id = gc_id or Uuid7Factory().new()
    digest_hex = _install_object(root, payload)
    if gc_artifact is not None:
        _install_gc_artifact(root, gc_id, gc_artifact)
    _install_blob_row(root, payload, state="deleting", gc_id=gc_id)
    return root, gc_id, digest_hex


def _blob_row_state(root: Path, digest_hex: str) -> str | None:
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    try:
        row = connection.execute(
            "SELECT state FROM attachment_blobs WHERE digest=?",
            (bytes.fromhex(digest_hex),),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


# ----------------------------- D-F = F.2 -----------------------------------


@pytest.mark.parametrize("tombstone_in_source", [True, False])
def test_restored_deleting_state_wins_over_target_payload_through_existing_gc_recovery(
    tmp_path, tombstone_in_source
):
    """D-F=F.2: the restored durable `deleting` state is authoritative for a
    payload that already exists in the target CAS.  The restore itself never
    deletes; the next startup completes the restored state through the
    EXISTING Phase 6 GC recovery, which removes the payload and the row."""
    payload = b"restored-gc-precedence-payload" + bytes([tombstone_in_source]) * 24
    gc_id = Uuid7Factory().new()
    source, gc_id, digest_hex = _deleting_blob_source_root(
        tmp_path,
        f"gc-source-{tombstone_in_source}",
        payload,
        gc_id=gc_id,
        gc_artifact=(
            _AttachmentFS.tombstone_bytes(gc_id, bytes.fromhex(hashlib.sha256(payload).hexdigest()))
            if tombstone_in_source
            else None
        ),
    )
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "gc-restore.botsbackup")

    target = _make_root(tmp_path, f"gc-target-{tombstone_in_source}", 1024)
    _install_object(target, payload)
    _install_blob_row(target, payload, state="ready")
    authority = _new_authority(target)
    try:
        RestoreService(authority).restore(portable)
        # The restore transaction itself deletes nothing: the payload is
        # content the restored authority has durably marked for deletion, and
        # that deletion completes through the existing Phase 6 machinery.
        assert (target / "attachments" / "objects" / digest_hex).exists()
        assert _blob_row_state(target, digest_hex) == "deleting"
    finally:
        authority.close()

    runtime = build_runtime(target)
    _close_runtime(runtime)
    # The existing Phase 6 GC recovery completed the restored deleting state:
    # the CAS payload is gone, the gc namespace is drained, and the row is
    # deleted through the guarded T9 transaction.
    assert not (target / "attachments" / "objects" / digest_hex).exists()
    assert list((target / "attachments" / "gc").iterdir()) == []
    assert list((target / "attachments" / "staging").iterdir()) == []
    assert list((target / "attachments" / "captures").iterdir()) == []
    assert _blob_row_state(target, digest_hex) is None
    assert _store_settings(target) == 2222


def test_restored_staging_state_completes_through_existing_startup_recovery(tmp_path):
    """D-F=F.2 scope: restored lifecycle state (a staging recovery artefact) is
    restored into the target namespace and completes through the existing
    Phase 6 T8 startup recovery — publication to the CAS.

    The source is a post-``capture_to_stage`` mid-publication state (the
    capture namespace is already drained); a state carrying both staging and
    captures artefacts cannot be captured at all because the capture writes
    its ZIP lifecycle entries in an order its own verifier rejects — a
    capture-side quirk outside this write set, noted in the M5 report."""
    payload = b"restored-staging-state-payload" + b"y" * 24
    source = _make_root(tmp_path, "staging-source", 2222)
    operation_id = Uuid7Factory().new()
    (source / "attachments" / "staging" / operation_id).write_bytes(payload)
    os.chmod(source / "attachments" / "staging" / operation_id, 0o600)
    digest_hex = _install_blob_row(
        source, payload, state="staging", operation_id=operation_id
    )
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "staging-restore.botsbackup")

    target = _make_root(tmp_path, "staging-target", 1024)
    authority = _new_authority(target)
    try:
        RestoreService(authority).restore(portable)
        # The artefact was restored into the target's staging namespace and
        # the restore itself did not publish it (the existing T8 recovery
        # owns publication).
        assert (target / "attachments" / "staging" / operation_id).exists()
        assert _blob_row_state(target, digest_hex) == "staging"
    finally:
        authority.close()
    runtime = build_runtime(target)
    _close_runtime(runtime)
    assert (target / "attachments" / "objects" / digest_hex).exists()
    assert _blob_row_state(target, digest_hex) == "ready"
    assert list((target / "attachments" / "staging").iterdir()) == []
    assert list((target / "attachments" / "captures").iterdir()) == []
    assert list((target / "attachments" / "gc").iterdir()) == []
    assert _store_settings(target) == 2222


def test_non_attributable_package_gc_artifact_fails_closed_without_deletion(tmp_path):
    """D-F=F.2 negative: a package GC artefact that is neither an attributable
    tombstone nor the attributable payload of the restored deleting state is
    ambiguous evidence — the restore fails closed and deletes nothing."""
    payload = b"gc-ambiguous-package-artefact" + b"z" * 24
    source, gc_id, digest_hex = _deleting_blob_source_root(
        tmp_path, "gc-garbage-source", payload, gc_artifact=b"garbage-not-a-tombstone"
    )
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "gc-garbage.botsbackup")
    target = _make_root(tmp_path, "gc-garbage-target", 1024)
    _install_object(target, payload)
    _install_blob_row(target, payload, state="ready")
    before = _live_sha(target)

    authority = _new_authority(target)
    try:
        with pytest.raises(BackupUnclassifiedState, match="neither an attributable"):
            RestoreService(authority).restore(portable)
        # The target state is untouched and consistent: no poison, no deletion.
        assert not authority.poison_pending
    finally:
        _close_tolerant(authority)
    assert _live_sha(target) == before
    assert (target / "attachments" / "objects" / digest_hex).exists()
    assert list((target / "attachments" / "gc").iterdir()) == []
    assert _blob_row_state(target, digest_hex) == "ready"
    # The failed transaction is attributable scratch and reconciles cleanly.
    record = json.loads((target / "database" / _RESTORE_JOURNAL).read_text("utf-8"))
    assert record["phase"] == "STAGING"
    authority = _new_authority(target)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None and summary["action"] == "aborted"
        assert not authority.poison_pending
    finally:
        authority.close()
    assert not (target / "database" / _RESTORE_JOURNAL).exists()
    # Still no deletion on ambiguous evidence.
    assert (target / "attachments" / "objects" / digest_hex).exists()
    assert _store_settings(target) == 1024


def test_content_mismatch_for_deleting_payload_fails_closed_without_deletion(tmp_path):
    """D-F=F.2 negative: a target payload whose bytes do not match the restored
    authoritative deleting state (digest, expected size, payload identity) is
    an I5 integrity ambiguity — poison, fail closed, never delete."""
    payload = b"gc-content-mismatch-payload" + b"w" * 24
    source, _gc_id, digest_hex = _deleting_blob_source_root(
        tmp_path, "gc-mismatch-source", payload, gc_artifact=None
    )
    package_path, _receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "gc-mismatch.botsbackup")
    target = _make_root(tmp_path, "gc-mismatch-target", 1024)
    wrong = payload + b"corrupted"
    leaf = target / "attachments" / "objects" / digest_hex
    leaf.write_bytes(wrong)
    os.chmod(leaf, 0o600)
    authority = _new_authority(target)
    try:
        with pytest.raises(BackupUnclassifiedState, match="does not match"):
            RestoreService(authority).restore(portable)
        assert authority.poison_pending
    finally:
        _close_tolerant(authority)
    assert leaf.read_bytes() == wrong


def test_unattributable_target_object_fails_closed_before_adoption(
    tmp_path, restore_environment
):
    """D-F=F.2 negative: an object the restored authoritative database does not
    attribute is never silently adopted around — the restore fails closed
    before any live mutation and the orphan object is not deleted."""
    env = restore_environment
    orphan_digest = hashlib.sha256(b"orphan-payload").hexdigest()
    orphan = env.root / "attachments" / "objects" / orphan_digest
    orphan.write_bytes(b"orphan-payload")
    os.chmod(orphan, 0o600)
    before = _live_sha(env.root)
    authority = _new_authority(env.root)
    try:
        with pytest.raises(
            BackupUnclassifiedState, match="unattributable canonical payload object"
        ):
            RestoreService(authority).restore(env.package)
        assert authority.poison_pending
    finally:
        _close_tolerant(authority)
    assert orphan.exists()
    assert _live_sha(env.root) == before


def test_unattributable_target_gc_artifact_fails_closed(tmp_path, restore_environment):
    """D-F=F.2 negative: a GC namespace leaf the restored authoritative
    database does not attribute is ambiguous evidence — fail closed, and the
    ambiguous leaf is never deleted."""
    env = restore_environment
    foreign = env.root / "attachments" / "gc" / Uuid7Factory().new()
    foreign.write_bytes(b"foreign-lifecycle-artefact")
    os.chmod(foreign, 0o600)
    authority = _new_authority(env.root)
    try:
        with pytest.raises(
            BackupUnclassifiedState, match="unattributable GC lifecycle artefact"
        ):
            RestoreService(authority).restore(env.package)
        assert authority.poison_pending
    finally:
        _close_tolerant(authority)
    assert foreign.exists()


# ----------------------------- D-D = D.2 -----------------------------------


_DESTRUCTIVE_ADOPT_INTENT_CRASH = r'''
import os, sys
from pathlib import Path
from bots5.bootstrap.desktop import build_runtime
from bots5.infrastructure import restore_service

def die(point):
    if point == "after-journal-write-ADOPT_INTENT":
        os._exit(71)

restore_service._TEST_FAULT_HOOK = die
build_runtime(Path(sys.argv[1]), destructive_restore_override=True)
os._exit(93)
'''

_DESTRUCTIVE_MID_EXCHANGE_CRASH = r'''
import os, sys
from pathlib import Path
from bots5.bootstrap.desktop import build_runtime
from bots5.infrastructure import restore_service

def die(point):
    if point == "after-adopt-exchange":
        os._exit(72)

restore_service._TEST_FAULT_HOOK = die
build_runtime(Path(sys.argv[1]), destructive_restore_override=True)
os._exit(94)
'''


def test_destructive_override_executes_without_preservation_or_rollback_source(
    tmp_path, restore_environment
):
    """D-D=D.2 execution: with all four validity conditions holding, the
    override executes the halted restore destructively — no preservation is
    created, the displaced installation is destroyed so it is truthfully not
    rollback-capable, and the receipt records the destructive consequence."""
    env = restore_environment
    before = _live_sha(env.root)
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "PRESERVING"
    assert record["preservation_record"] is None
    txid = str(record["restore_transaction_id"])

    runtime = build_runtime(env.root, destructive_restore_override=True)
    _close_runtime(runtime)
    receipt = json.loads(
        (env.root / "database" / _RESTORE_RECEIPT).read_text("utf-8")
    )
    assert set(receipt) == RECEIPT_FIELDS
    assert receipt["outcome"] == "RESTORED_DESTRUCTIVE"
    assert receipt["transaction_id"] == txid
    assert receipt["backup_id"] == env.backup_id
    assert receipt["package_sha256"] == env.artifact_sha256
    assert not journal.exists()
    assert not list((env.root / "database").glob(".bots5-restore-*.lock"))
    assert not list((env.root / "database").glob(".bots5-restore-*.tmp"))
    # The operator-authorized destructive consequence is real: no preservation
    # artefact exists and the displaced pre-restore installation is destroyed.
    assert list((env.root / "retained-installations").iterdir()) == []
    assert not (env.root / "database" / f".bots5-restore-{txid}.candidate.sqlite3").exists()
    assert _live_sha(env.root) != before
    assert _stored_max_output_tokens(env.root) == 2222
    assert _store_settings(env.root) == 2222


def test_destructive_override_crash_before_exchange_aborts_without_live_mutation(
    tmp_path, restore_environment
):
    """W4 for a destructive transaction: a crash after the fsynced ADOPT_INTENT
    but before the exchange is attributed by the exchange's atomicity — the
    canonical leaf is not the candidate, so no mutation occurred and the next
    startup aborts cleanly, leaving the current installation intact."""
    env = restore_environment
    before = _live_sha(env.root)
    _prepare_desktop_root(env.root)
    # First create the halted PRESERVING transaction the override continues.
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    # Then a startup with the authorization crashes at the fsynced ADOPT_INTENT
    # boundary, before the exchange.
    _run_restore_child(env.root, env.package, _DESTRUCTIVE_ADOPT_INTENT_CRASH, 71)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    assert record["preservation_record"] is None
    txid = str(record["restore_transaction_id"])

    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert not journal.exists()
    assert not (env.root / "database" / f".bots5-restore-{txid}.candidate.sqlite3").exists()
    assert not (env.root / "attachments" / "staging" / f"restore-{txid}").exists()
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024


def test_destructive_override_mid_exchange_crash_converges_forward_and_destroys(
    tmp_path, restore_environment
):
    """W5/W6 for a destructive transaction: the exchange is atomic, so a crash
    right after it leaves the candidate canonical; the next startup converges
    forward destructively and destroys the displaced installation."""
    env = restore_environment
    before = _live_sha(env.root)
    _prepare_desktop_root(env.root)
    # First create the halted PRESERVING transaction the override continues.
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    # Then a startup with the authorization crashes immediately after the one
    # RENAME_EXCHANGE, before the PROMOTED journal write.
    _run_restore_child(env.root, env.package, _DESTRUCTIVE_MID_EXCHANGE_CRASH, 72)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    assert record["preservation_record"] is None
    txid = str(record["restore_transaction_id"])

    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    receipt = json.loads(
        (env.root / "database" / _RESTORE_RECEIPT).read_text("utf-8")
    )
    assert receipt["outcome"] == "RESTORED_DESTRUCTIVE"
    assert receipt["transaction_id"] == txid
    assert not journal.exists()
    # The displaced installation was destroyed on convergence.
    assert not (env.root / "database" / f".bots5-restore-{txid}.candidate.sqlite3").exists()
    assert list((env.root / "retained-installations").iterdir()) == []
    assert _live_sha(env.root) != before
    assert _stored_max_output_tokens(env.root) == 2222


@pytest.mark.parametrize(
    "condition",
    ["rollback_capable", "source_verification", "target_identity", "no_transaction"],
)
def test_destructive_override_execution_refused_when_condition_unmet(
    tmp_path, restore_environment, condition
):
    """D-D=D.2 execution negatives: the destructive step never runs with any
    validity condition unmet, never falls out of a normal restore, and never
    waives authority/target-identity uncertainty.  A refused authorization is
    fail-closed and loud, never silently discarded."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    if condition == "rollback_capable":
        _run_restore_child(env.root, env.package, _PRESERVED_CRASH, 65)
    elif condition == "no_transaction":
        pass  # a healthy root with no restore journal at all
    else:
        _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    if condition == "source_verification":
        env.package.unlink()
    if condition == "target_identity":
        # The root topology changed between the transaction and this startup,
        # so the recorded root identity no longer matches: uncertainty, which
        # the override may never waive.
        (env.root / "unattributable-topology").mkdir(mode=0o700)
    before = _live_sha(env.root)

    if condition == "rollback_capable":
        with pytest.raises(BackupError, match="rollback-capable"):
            build_runtime(env.root, destructive_restore_override=True)
    elif condition == "source_verification":
        with pytest.raises(BackupError, match="did not independently verify"):
            build_runtime(env.root, destructive_restore_override=True)
    elif condition == "target_identity":
        with pytest.raises(BackupUnclassifiedState, match="no longer matches"):
            build_runtime(env.root, destructive_restore_override=True)
    else:
        with pytest.raises(BackupError, match="no pending restore transaction"):
            build_runtime(env.root, destructive_restore_override=True)

    # Nothing was destroyed and no receipt was fabricated.
    assert not (env.root / "database" / _RESTORE_RECEIPT).exists()
    assert _live_sha(env.root) == before
    if condition != "no_transaction":
        journal = env.root / "database" / _RESTORE_JOURNAL
        assert json.loads(journal.read_text("utf-8"))["phase"] in {
            "PRESERVING",
            "PRESERVED",
        }
        assert list((env.root / "retained-installations").iterdir()) in ([], None) or (
            env.root / "retained-installations"
        ).exists()
    assert _stored_max_output_tokens(env.root) == 1024


def test_normal_restore_path_never_falls_back_to_destructive_execution(
    tmp_path, restore_environment
):
    """I15: without the explicit operator authorization the exact same halted
    transaction is reconciled as scratch and the current installation is
    preserved — the destructive continuation is unreachable as a fallback."""
    env = restore_environment
    before = _live_sha(env.root)
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert not (env.root / "database" / _RESTORE_JOURNAL).exists()
    assert not (env.root / "database" / _RESTORE_RECEIPT).exists()
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024
    assert _store_settings(env.root) == 1024


# ----------------------------- D-A = A.2 -----------------------------------


def test_retained_installation_cleanup_is_explicit_operator_consequence(
    tmp_path, restore_environment
):
    """D-A=A.2: retention is indefinite by default — restore, reconciliation
    and startups never remove the artefact — and removal is a separate,
    deliberate operator consequence requiring an explicit confirmation."""
    env = restore_environment
    authority = _new_authority(env.root)
    try:
        written = RestoreService(authority).restore(env.package)
    finally:
        authority.close()
    leaf = f"{written['transaction_id']}-{env.backup_id}.sqlite3"
    artefact = env.root / "retained-installations" / leaf
    assert artefact.exists()

    # Cleanup is NOT invoked by restore or normal startup.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert artefact.exists()

    authority = _new_authority(env.root)
    try:
        with pytest.raises(BackupError, match="explicit operator confirmation"):
            RestoreService(authority).remove_retained_installation(
                leaf, operator_confirmed=False
            )
        with pytest.raises(TypeError, match="explicitly supplied bool"):
            RestoreService(authority).remove_retained_installation(
                leaf, operator_confirmed=1
            )
        with pytest.raises(BackupError, match="naming contract"):
            RestoreService(authority).remove_retained_installation(
                "not-a-preservation-leaf", operator_confirmed=True
            )
        removed = RestoreService(authority).remove_retained_installation(
            leaf, operator_confirmed=True
        )
        assert removed["removed"] == f"retained-installations/{leaf}"
    finally:
        authority.close()
    assert not artefact.exists()
    assert list((env.root / "retained-installations").iterdir()) == []
    # The removal is the retention consequence only: the installation runs on.
    assert _store_settings(env.root) == 2222


def test_retained_installation_cleanup_refused_while_restore_journal_present(
    tmp_path, restore_environment
):
    """D-A=A.2: while any restore journal exists the retention surface stays
    closed — the machine may still need the preserved installation."""
    env = restore_environment
    authority = _new_authority(env.root)
    try:
        written = RestoreService(authority).restore(env.package)
    finally:
        authority.close()
    leaf = f"{written['transaction_id']}-{env.backup_id}.sqlite3"
    artefact = env.root / "retained-installations" / leaf

    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    assert (env.root / "database" / _RESTORE_JOURNAL).exists()

    authority = _new_authority(env.root)
    try:
        with pytest.raises(BackupError, match="restore machine is not idle"):
            RestoreService(authority).remove_retained_installation(
                leaf, operator_confirmed=True
            )
        assert not authority.poison_pending
    finally:
        authority.close()
    assert artefact.exists()
    assert (env.root / "database" / _RESTORE_JOURNAL).exists()


# ----------------------------- D-C = C.2 -----------------------------------


def test_restore_is_data_root_centric_across_topologies(tmp_path):
    """D-C=C.2: a backup captured under one topology restores into a
    differently-rooted, differently-shaped target; the captured installation
    does not even need to exist any more, and the target must merely prove
    its own ownership/filesystem/identity invariants."""
    source = _make_root(tmp_path, "portable-source", 2222)
    _prepare_desktop_root(source)  # the source carries XDG application dirs
    package_path, receipt = _capture_from_source(source)
    portable = _portable_copy(tmp_path, package_path, "portable.botsbackup")
    # The captured installation is moved away: the package cannot depend on
    # the source's absolute paths.
    os.rename(source, tmp_path / "portable-source-moved-away")

    deep = tmp_path / "deeply" / "nested" / "elsewhere"
    deep.mkdir(mode=0o700, parents=True)
    target = deep / "target"
    target.mkdir(mode=0o700)
    authority = _new_authority(target)
    authority.close()
    seed = _seed_database(tmp_path, "portable-target", 1024)
    os.rename(seed, target / "database" / "state.sqlite3")
    os.chmod(target / "database" / "state.sqlite3", 0o600)

    authority = _new_authority(target)
    try:
        written = RestoreService(authority).restore(portable)
        assert written["backup_id"] == receipt["backup_id"]
        assert written["source_revision"] == HEAD
    finally:
        authority.close()
    assert _stored_max_output_tokens(target) == 2222
    assert _store_settings(target) == 2222
    artefact = (
        target
        / "retained-installations"
        / f"{written['transaction_id']}-{receipt['backup_id']}.sqlite3"
    )
    assert artefact.exists()
    # The target is at a different absolute path, depth and XDG topology than
    # the source ever was, and the restored state is complete.
    assert target != tmp_path / "portable-source"


def test_unprovable_target_topology_fails_closed(tmp_path, restore_environment):
    """D-C=C.2: portability never waives filesystem safety — a target whose
    ownership/filesystem/identity invariants cannot be proven fails closed
    and no restore can run against it."""
    env = restore_environment
    loose = tmp_path / "loose-target"
    loose.mkdir(mode=0o755)
    with pytest.raises(AuthorityError, match="unsafe owner or permissions"):
        _new_authority(loose)
    assert not (loose / "database").exists()

    broken = tmp_path / "broken-target"
    broken.mkdir(mode=0o700)
    (broken / "database").write_text("not a directory")
    with pytest.raises(Exception):
        _new_authority(broken)
    assert (broken / "database").read_text() == "not a directory"
    assert not (broken / "database" / _RESTORE_JOURNAL).exists()
    # The verified package is untouched by the refusals.
    assert env.package.exists()


# ---------------------------------------------------------------------------
# Authorized non-UI restore initiation entry point (Slice D).  The production
# surface is `bots5-desktop --restore-from PACKAGE` (src/bots5/bootstrap/
# desktop.py `main`); every child below invokes that real entry point.
# ---------------------------------------------------------------------------


#: The real operator entry point as one child process.  argv[3] is an
#: optional --expected-backup-id pin ("-" means none).
_ENTRY_POINT_CHILD = r'''
import sys

from bots5.bootstrap.desktop import main

argv = ["--restore-from", sys.argv[2], "--data-root", sys.argv[1]]
if len(sys.argv) > 3 and sys.argv[3] != "-":
    argv += ["--expected-backup-id", sys.argv[3]]
sys.exit(main(argv))
'''

#: The same entry point with PySide6/qasync made entirely unimportable: the
#: whole path — the module import included — must run without Qt and must
#: never pull it in.
_ENTRY_POINT_NO_QT_CHILD = r'''
import sys

class _QtUnavailable:
    """Import hook that refuses to load the Qt machinery."""

    def find_spec(self, name, path=None, target=None):
        if name == "PySide6" or name.startswith("PySide6.") or name == "qasync":
            raise ImportError(f"{name} is unavailable in this environment")
        return None

sys.meta_path.insert(0, _QtUnavailable())

from bots5.bootstrap.desktop import main

code = main(["--restore-from", sys.argv[2], "--data-root", sys.argv[1]])
assert "PySide6" not in sys.modules, "restore path imported PySide6"
assert "qasync" not in sys.modules, "restore path imported qasync"
sys.exit(code)
'''

#: The entry point with post-adoption validation forced to fail: the typed
#: rollback outcome must be reported truthfully and the live installation
#: must be converged back to the preserved pre-restore state.
_ENTRY_POINT_ROLLBACK_CHILD = r'''
import sys

from bots5.bootstrap.desktop import main
from bots5.infrastructure import restore_service

def fail(point):
    if point == "post-adoption-validation":
        raise RuntimeError("forced post-adoption validation failure")

restore_service._TEST_FAULT_HOOK = fail
sys.exit(main(["--restore-from", sys.argv[2], "--data-root", sys.argv[1]]))
'''

#: The no-argument desktop path with build_runtime replaced by a sentinel
#: failure: proves main() without the restore flag still composes the desktop
#: exactly as before (Qt import, QApplication, build_runtime) and never takes
#: the restore path.
_DESKTOP_DEFAULT_PATH_CHILD = r'''
import sys

import bots5.bootstrap.desktop as desktop

def forbidden(*args, **kwargs):
    raise RuntimeError("desktop-path sentinel")

desktop.build_runtime = forbidden
sys.exit(desktop.main(["--data-root", sys.argv[1]]))
'''


def _run_entry_point_child(
    source: str, root: Path, package: Path, pin: str = "-"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), os.fspath(package), pin],
        cwd=REPO,
        env={
            **os.environ,
            "PYTHONPATH": os.fspath(REPO / "src"),
            "QT_QPA_PLATFORM": "offscreen",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _assert_idle_database(root: Path) -> None:
    """No restore journal, transaction lock or temp leaf exists."""
    database = root / "database"
    assert not (database / _RESTORE_JOURNAL).exists()
    assert not list(database.glob(".bots5-restore-*.lock"))
    assert not list(database.glob(".bots5-restore-*.tmp"))


def _assert_no_receipt(root: Path) -> None:
    """No restore receipt was fabricated."""
    assert not (root / "database" / _RESTORE_RECEIPT).exists()


def test_restore_entry_point_parser_leaves_desktop_defaults_unchanged():
    """The restore initiation is strictly additive: the no-argument parser
    defaults that drive the desktop are untouched, and the destructive
    override flag does not exist on this surface."""
    args = _parser().parse_args([])
    assert args.restore_from is None
    assert args.expected_backup_id is None
    assert args.backend == "fake"
    assert args.reasoning_effort is None
    with pytest.raises(SystemExit):
        _parser().parse_args(
            ["--restore-from", "package.botsbackup", "--destructive-restore-override"]
        )


def test_restore_entry_point_initiates_and_adopts_end_to_end(
    tmp_path, restore_environment
):
    """The authorized entry point runs one real restore transaction through
    the existing service: the restored state is actually adopted, the typed
    receipt is the process output byte-identical to the durable receipt, and
    a normal startup then opens the adopted installation."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)
    completed = _run_entry_point_child(
        _ENTRY_POINT_CHILD, env.root, env.package, env.backup_id
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    receipt = json.loads(completed.stdout)
    assert set(receipt) == RECEIPT_FIELDS
    assert receipt["outcome"] == "RESTORED"
    assert receipt["backup_id"] == env.backup_id
    assert receipt["package_sha256"] == env.artifact_sha256
    assert receipt["source_revision"] == HEAD
    assert receipt["post_adoption_migration"] == "not_required"
    # stdout is byte-identical to the durable receipt: the existing typed
    # outcome is reported truthfully, with no second reporting channel.
    durable = env.root / "database" / _RESTORE_RECEIPT
    assert durable.read_bytes() == completed.stdout.encode("utf-8")
    # The restored state was adopted, not merely invoked.
    assert _live_sha(env.root) != before
    assert _stored_max_output_tokens(env.root) == 2222
    assert _store_settings(env.root) == 2222
    _assert_idle_database(env.root)
    # The displaced installation is retained recovery state (D-A=A.2).
    preserved = list((env.root / "retained-installations").iterdir())
    assert [artefact.name for artefact in preserved] == [
        f"{receipt['transaction_id']}-{env.backup_id}.sqlite3"
    ]
    # The adopted installation boots through the normal startup afterwards.
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert json.loads(durable.read_text("utf-8")) == receipt


def test_restore_entry_point_runs_with_qt_unavailable(tmp_path, restore_environment):
    """PySide6 and qasync are made entirely unimportable: the entry point
    still imports, reconciles and restores end to end, and never imports
    Qt."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    completed = _run_entry_point_child(_ENTRY_POINT_NO_QT_CHILD, env.root, env.package)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout)["outcome"] == "RESTORED"
    assert _stored_max_output_tokens(env.root) == 2222
    _assert_idle_database(env.root)


def test_restore_entry_point_never_opens_the_store(
    tmp_path, restore_environment, monkeypatch
):
    """The restore initiation sits before store opening in the accepted
    ordering: if any code on the path opened a store, the guard fails the
    test."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)

    def forbidden(self):
        raise AssertionError("restore initiation opened the store")

    monkeypatch.setattr(DataRootAuthority, "open_store", forbidden)
    try:
        code = desktop_main(
            [
                "--restore-from",
                os.fspath(env.package),
                "--data-root",
                os.fspath(env.root),
            ]
        )
    finally:
        monkeypatch.undo()
    assert code == 0
    # The restore really adopted while the store stayed closed throughout.
    assert _live_sha(env.root) != before
    assert _stored_max_output_tokens(env.root) == 2222


def test_restore_entry_point_refusal_is_truthful_and_non_mutating(
    tmp_path, restore_environment
):
    """A verification refusal reports the typed outcome with a non-zero
    status and mutates no live state."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    broken = tmp_path / "outside" / "broken.botsbackup"
    broken.write_bytes(env.package.read_bytes()[:-9])
    before = _live_sha(env.root)
    completed = _run_entry_point_child(_ENTRY_POINT_CHILD, env.root, broken)
    assert completed.returncode == 2, (completed.stdout, completed.stderr)
    assert "restore not committed" in completed.stderr
    assert "bots5.core.errors.BackupArchiveInvalid" in completed.stderr
    # Live state untouched; no transaction artefacts were fabricated.
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024
    _assert_idle_database(env.root)
    _assert_no_receipt(env.root)
    assert list((env.root / "retained-installations").iterdir()) == []


def test_restore_entry_point_expected_backup_id_pin_refuses_mismatch(
    tmp_path, restore_environment
):
    """--expected-backup-id pins independent verification: a mismatch is a
    typed refusal before any live mutation."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)
    completed = _run_entry_point_child(
        _ENTRY_POINT_CHILD, env.root, env.package, "not-the-captured-backup-id"
    )
    assert completed.returncode == 2, (completed.stdout, completed.stderr)
    assert "restore not committed" in completed.stderr
    assert "BackupArchiveInvalid" in completed.stderr
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024
    _assert_idle_database(env.root)
    _assert_no_receipt(env.root)


def test_restore_entry_point_rolls_back_and_requires_restart(
    tmp_path, restore_environment
):
    """A post-adoption validation failure is the typed rollback outcome:
    reported truthfully with a non-zero status, the live installation
    converged back to the preserved pre-restore state, and the durable
    journal left for the restart acknowledgment."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)
    journal = env.root / "database" / _RESTORE_JOURNAL
    completed = _run_entry_point_child(
        _ENTRY_POINT_ROLLBACK_CHILD, env.root, env.package
    )
    assert completed.returncode == 2, (completed.stdout, completed.stderr)
    assert "restore not committed" in completed.stderr
    assert "bots5.core.errors.BackupError" in completed.stderr
    assert (
        "restore rolled back to the preserved installation; restart required"
        in completed.stderr
    )
    # The rollback converged the live installation to the preserved state.
    assert _live_sha(env.root) == before
    assert _stored_max_output_tokens(env.root) == 1024
    assert json.loads(journal.read_text("utf-8"))["phase"] == "ROLLED_BACK"
    assert not (env.root / "database" / _RESTORE_RECEIPT).exists()
    # The next startup completes the acknowledgment and still refuses; the
    # one after is clean (W9).
    with pytest.raises(BackupError, match="restart required"):
        build_runtime(env.root)
    assert not journal.exists()
    runtime = build_runtime(env.root)
    _close_runtime(runtime)
    assert _store_settings(env.root) == 1024


def test_restore_entry_point_reconciles_interrupted_restore_before_initiating(
    tmp_path, restore_environment
):
    """Accepted ordering: the entry point runs the accepted coordinator
    interception (reconcile any interrupted restore) BEFORE it initiates the
    new transaction — an interrupted ADOPT_INTENT transaction is attributed
    and aborted first, then a fresh restore commits."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    halted = json.loads(journal.read_text("utf-8"))
    assert halted["phase"] == "ADOPT_INTENT"
    completed = _run_entry_point_child(_ENTRY_POINT_CHILD, env.root, env.package)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    receipt = json.loads(completed.stdout)
    assert receipt["outcome"] == "RESTORED"
    # The interrupted transaction was reconciled first: the committed
    # transaction is a fresh one, not the halted journal's.
    assert receipt["transaction_id"] != str(halted["restore_transaction_id"])
    assert not journal.exists()
    assert _live_sha(env.root) != before
    assert _stored_max_output_tokens(env.root) == 2222
    _assert_idle_database(env.root)


def test_restore_entry_point_fails_closed_on_torn_journal(
    tmp_path, restore_environment
):
    """W10 through the entry point: unattributable evidence fails closed
    with a non-zero status and a typed outcome, preserves the journal bytes
    and mutates nothing else."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    _run_restore_child(env.root, env.package, _ADOPT_INTENT_CRASH, 61)
    journal = env.root / "database" / _RESTORE_JOURNAL
    corrupted = journal.read_bytes()[:-2]
    journal.write_bytes(corrupted)
    before = _live_sha(env.root)
    completed = _run_entry_point_child(_ENTRY_POINT_CHILD, env.root, env.package)
    assert completed.returncode == 3, (completed.stdout, completed.stderr)
    assert "restore failed closed" in completed.stderr
    assert "BackupUnclassifiedState" in completed.stderr
    assert "torn or non-canonical" in completed.stderr
    # The ambiguous bytes are preserved, never rewritten or guessed at.
    assert journal.read_bytes() == corrupted
    assert _live_sha(env.root) == before
    assert not (env.root / "database" / _RESTORE_RECEIPT).exists()


def test_restore_entry_point_exposes_no_destructive_override(
    tmp_path, restore_environment
):
    """The D-D=D.2 destructive surface stays off this path: a halted
    PRESERVING transaction with no preservation record is reconciled as
    scratch, after which the entry point initiates a fresh
    preservation-capable restore — nothing is destructively executed."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)
    _run_restore_child(env.root, env.package, _PRESERVING_CRASH, 64)
    halted = json.loads(
        (env.root / "database" / _RESTORE_JOURNAL).read_text("utf-8")
    )
    assert halted["phase"] == "PRESERVING"
    assert halted["preservation_record"] is None
    completed = _run_entry_point_child(_ENTRY_POINT_CHILD, env.root, env.package)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    receipt = json.loads(completed.stdout)
    assert receipt["outcome"] == "RESTORED"
    assert receipt["transaction_id"] != str(halted["restore_transaction_id"])
    # The fresh transaction preserved: the destructive continuation never ran.
    preserved = list((env.root / "retained-installations").iterdir())
    assert [artefact.name for artefact in preserved] == [
        f"{receipt['transaction_id']}-{env.backup_id}.sqlite3"
    ]
    assert _live_sha(env.root) != before
    assert _stored_max_output_tokens(env.root) == 2222


def test_restore_entry_point_refused_while_root_is_owned(
    tmp_path, restore_environment
):
    """Single-owner law: while another live authority owns the data root
    (the normal desktop), the entry point refuses with a non-zero status and
    mutates nothing."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    before = _live_sha(env.root)
    holder = _new_authority(env.root)
    try:
        completed = _run_entry_point_child(_ENTRY_POINT_CHILD, env.root, env.package)
    finally:
        _close_tolerant(holder)
    assert completed.returncode == 2, (completed.stdout, completed.stderr)
    assert "restore not committed" in completed.stderr
    assert "AuthorityError" in completed.stderr
    assert _live_sha(env.root) == before
    _assert_idle_database(env.root)
    _assert_no_receipt(env.root)


def test_desktop_no_argument_path_is_unchanged(tmp_path, restore_environment):
    """bots5-desktop without the restore flag still composes the desktop
    exactly as before: Qt is imported, QApplication is constructed and
    build_runtime is called — the sentinel proves the desktop branch ran and
    the restore path did not."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    completed = _run_entry_point_child(
        _DESKTOP_DEFAULT_PATH_CHILD, env.root, env.package
    )
    assert completed.returncode == 1, (completed.stdout, completed.stderr)
    assert "error: desktop-path sentinel" in completed.stderr
    # The desktop path created no restore artefacts.
    _assert_idle_database(env.root)


# ---------------------------------------------------------------------------
# Slice E M5 — the restore handoff (additive section; fixtures local to this
# module).  The live desktop never calls whole-installation restore: the
# handoff closes the production window set, runtime.close() releases the
# authority LAST, and only then does the real serve() post-close consumer run
# the pre-store bootstrap child (invariant I2).  All data roots here are the
# mechanically disposable restore_environment roots — never the operator's
# real data root.
# ---------------------------------------------------------------------------


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio as _asyncio  # noqa: E402

from PySide6.QtCore import QTimer as _QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication as _QApplication,
    QDialog as _QDialog,
    QMessageBox as _QMessageBox,
)
from qasync import QEventLoop  # noqa: E402

from bots5.bootstrap import desktop as _bootstrap_desktop  # noqa: E402
from bots5.bootstrap.desktop import (  # noqa: E402
    DesktopRuntime as _DesktopRuntime,
    RestoreHandoffRequest as _RestoreHandoffRequest,
    serve as _serve,
)
from bots5.desktop import phase9_dialogs as _phase9_dialogs  # noqa: E402


class _HandoffFakeChild:
    """Stand-in for the waited pre-store bootstrap child process."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _handoff_patch_result_dialog(monkeypatch) -> list:
    """Recording REAL S8 result dialog that dismisses itself headlessly."""

    created: list = []
    base = _phase9_dialogs.RestoreHandoffResultDialog

    class _AutoDismissResultDialog(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def show(self):
            super().show()
            _QTimer.singleShot(
                0, lambda: self.done(_QDialog.DialogCode.Accepted)
            )

    monkeypatch.setattr(
        _phase9_dialogs, "RestoreHandoffResultDialog", _AutoDismissResultDialog
    )
    return created


def _handoff_accept_consequence(monkeypatch) -> None:
    def accepting_exec(box):
        box.done(_QDialog.DialogCode.Accepted)

    monkeypatch.setattr(_QMessageBox, "exec", accepting_exec)


def _handoff_forbid_live_restore(monkeypatch) -> list:
    """Proof 9 guards: any in-process restore call fails the test at once."""

    calls: list = []

    def forbidden_initiate(*args, **kwargs):
        calls.append(("_initiate_restore", args, kwargs))
        raise AssertionError("_initiate_restore ran in the live desktop process")

    def forbidden_restore(self, package, **kwargs):
        calls.append(("RestoreService.restore", package, kwargs))
        raise AssertionError(
            "RestoreService.restore ran in the live desktop process"
        )

    monkeypatch.setattr(
        _bootstrap_desktop, "_initiate_restore", forbidden_initiate
    )
    monkeypatch.setattr(RestoreService, "restore", forbidden_restore)
    return calls


class _HandoffResultRecording:
    """Pure-Python stand-in for the S8 result dialog (no Qt needed)."""

    def __init__(self, record: list):
        self._record = record
        self._slots = []

    class _Signal:
        def __init__(self, owner):
            self._owner = owner
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def disconnect(self, slot):
            self._slots.remove(slot)

        def emit(self, *args):
            for slot in tuple(self._slots):
                slot(*args)

    def __call__(self, *args, **kwargs):
        return self._build(*args, **kwargs)

    def _build(self, parent=None, *, data_root, argv, status, receipt_text="", refusal_text=""):
        instance = SimpleNamespace(
            data_root=data_root,
            argv=tuple(argv),
            status_code=int(status),
            raw_receipt_text=receipt_text,
            raw_refusal_text=refusal_text,
        )
        instance.finished = self._Signal(instance)

        def _show():
            self._record.append(instance)
            instance.finished.emit(1)  # auto-dismiss

        instance.show = _show
        return instance


def _handoff_run_qasync(qt_application, operation) -> None:
    qt_application.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(qt_application)
    _asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _handoff_wait_until(predicate, *, timeout: float = 30.0, what: str = "state") -> None:
    deadline = _asyncio.get_running_loop().time() + timeout
    while not predicate():
        if _asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await _asyncio.sleep(0.01)


async def _handoff_finish_serve(serve_task, *, timeout: float = 120.0) -> None:
    if serve_task is not None and not serve_task.done():
        await _asyncio.wait_for(serve_task, timeout=timeout)


def _handoff_scenario(qt_application, scenario) -> None:
    _handoff_run_qasync(qt_application, scenario())


def test_restore_handoff_never_invokes_restore_while_live_authority_is_open(
    tmp_path, restore_environment, monkeypatch
):
    """Proof 9: from the real Tools action through the orderly close, no
    whole-installation restore is invoked while the live authority/store is
    open; the post-close child can only run after runtime.close() returned
    and authority.release() ran.  A mocked child keeps the root untouched."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    qt_application = _QApplication.instance() or _QApplication([])

    async def scenario() -> None:
        runtime = None
        serve_task = None
        try:
            runtime = build_runtime(env.root)
            live_restore_calls = _handoff_forbid_live_restore(monkeypatch)

            released: list[bool] = []
            original_release = runtime.authority.release

            def spy_release():
                released.append(True)
                return original_release()

            monkeypatch.setattr(runtime.authority, "release", spy_release)
            close_returned: list[bool] = []
            original_close = _DesktopRuntime.close

            async def spy_close(self):
                await original_close(self)
                close_returned.append(True)

            monkeypatch.setattr(_DesktopRuntime, "close", spy_close)

            # While live, the authority owns the root and the store is open.
            assert runtime.authority.acquired
            assert not runtime.application._store.closed

            observed: dict = {}

            async def fake_child(argv):
                observed["argv"] = list(argv)
                observed["authority_released"] = bool(released)
                observed["close_returned"] = bool(close_returned)
                observed["store_closed"] = runtime.application._store.closed
                return _HandoffFakeChild(0, b'{"outcome": "RESTORED"}', b"")

            monkeypatch.setattr(
                _bootstrap_desktop, "_create_restore_child", fake_child
            )
            result_dialogs = _handoff_patch_result_dialog(monkeypatch)
            _handoff_accept_consequence(monkeypatch)

            serve_task = _asyncio.create_task(_serve(runtime))
            await _handoff_wait_until(
                lambda: bool(runtime.windows), what="the composed window"
            )
            window = runtime.windows[0]
            window.restore_from_backup_action.trigger()
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            dialog.package_edit.setText(str(env.package))
            dialog.confirm()

            await _handoff_wait_until(
                lambda: "argv" in observed,
                what="the post-close consumer to spawn the child",
            )
            await _handoff_wait_until(
                lambda: bool(result_dialogs) and not result_dialogs[0].isVisible(),
                what="the result dialog to be presented and dismissed",
            )
            await _handoff_finish_serve(serve_task)

            # The child ran only after the store was closed, the runtime had
            # closed, and the authority had been released.
            assert observed["store_closed"] is True
            assert observed["close_returned"] is True
            assert observed["authority_released"] is True
            assert live_restore_calls == []
            assert runtime.restore_exit_code == 0
            # The mocked child means the disposable root is untouched: the
            # handoff alone never mutated the live installation.
            assert _stored_max_output_tokens(env.root) == 1024
            _assert_idle_database(env.root)
            _assert_no_receipt(env.root)
        finally:
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _handoff_scenario(qt_application, scenario)


def test_restore_handoff_real_child_commits_on_disposable_root(
    tmp_path, restore_environment, monkeypatch
):
    """Proof 10 (real child): the full real route — Tools action, Proceed,
    production window close, successful runtime.close()/authority release,
    the real serve() post-close consumer, and the REAL pre-store bootstrap
    child on a mechanically disposable data root — commits with the Slice D
    semantics intact: exit 0, the raw canonical receipt, the adopted
    installation and the retained pre-restore installation."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    qt_application = _QApplication.instance() or _QApplication([])

    async def scenario() -> None:
        runtime = None
        serve_task = None
        try:
            runtime = build_runtime(env.root)
            live_restore_calls = _handoff_forbid_live_restore(monkeypatch)
            result_dialogs = _handoff_patch_result_dialog(monkeypatch)
            _handoff_accept_consequence(monkeypatch)

            released: list[bool] = []
            original_release = runtime.authority.release

            def spy_release():
                released.append(True)
                return original_release()

            monkeypatch.setattr(runtime.authority, "release", spy_release)

            serve_task = _asyncio.create_task(_serve(runtime))
            await _handoff_wait_until(
                lambda: bool(runtime.windows), what="the composed window"
            )
            window = runtime.windows[0]
            window.restore_from_backup_action.trigger()
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            dialog.package_edit.setText(str(env.package))
            dialog.confirm()

            await _handoff_wait_until(
                lambda: bool(result_dialogs),
                what="the real child to commit and the result to be presented",
            )
            await _handoff_wait_until(
                lambda: not result_dialogs[0].isVisible(),
                what="the result dialog to be dismissed",
            )
            await _handoff_finish_serve(serve_task)

            # The real child's exact exit code is preserved (never collapsed).
            assert runtime.restore_exit_code == 0
            assert released, "the authority was not released before the child"
            assert live_restore_calls == []

            # The GUI-visible surface showed the RAW canonical receipt.
            result_dialog = result_dialogs[0]
            assert result_dialog.status_code == 0
            receipt = json.loads(result_dialog.raw_receipt_text)
            assert receipt["outcome"] == "RESTORED"
            assert receipt["backup_id"] == env.backup_id
            assert receipt["package_sha256"] == env.artifact_sha256
            # stdout was byte-identical to the durable receipt (Slice D).
            durable = env.root / "database" / _RESTORE_RECEIPT
            assert durable.read_bytes() == result_dialog.raw_receipt_text.encode(
                "utf-8"
            )

            # The restore really adopted on the disposable root.
            assert _live_sha(env.root) != _live_sha_before[0]
            assert _stored_max_output_tokens(env.root) == 2222
            assert _store_settings(env.root) == 2222
            _assert_idle_database(env.root)
            preserved = list((env.root / "retained-installations").iterdir())
            assert [artefact.name for artefact in preserved] == [
                f"{receipt['transaction_id']}-{env.backup_id}.sqlite3"
            ]
        finally:
            if serve_task is not None and not serve_task.done():
                serve_task.cancel()
                try:
                    await serve_task
                except BaseException:
                    pass

    _live_sha_before = [_live_sha(env.root)]
    _handoff_scenario(qt_application, scenario)


def test_restore_handoff_real_child_refusal_and_fail_closed_preserve_root(
    tmp_path, restore_environment, monkeypatch
):
    """Proof 10 (real child, typed refusals): through the REAL post-close
    consumer and the REAL bootstrap child, a pinned-identity mismatch keeps
    exit status 2 and mutates nothing, and a torn journal keeps exit status 3
    (fail closed) — the 0/1/2/3 statuses are never collapsed and the
    disposable root is preserved."""
    env = restore_environment
    _prepare_desktop_root(env.root)
    presented: list = []
    patch = _HandoffResultRecording(presented)
    monkeypatch.setattr(
        _phase9_dialogs, "RestoreHandoffResultDialog", patch
    )

    async def commit_refusal() -> None:
        runtime = build_runtime(env.root)
        # Seam-level request injection: this proof targets the real child's
        # typed outcomes, not the reachability route (the mismatch is
        # refused earlier by the live verification, which the S2 abort test
        # covers separately).
        runtime.handoff.register(
            _RestoreHandoffRequest(
                package=env.package, expected_backup_id="deliberately-wrong"
            )
        )
        await runtime.close()
        await _bootstrap_desktop._run_restore_handoff_post_close(runtime)
        assert runtime.restore_exit_code == 2

    _asyncio.run(commit_refusal())

    refusal = presented[0]
    assert refusal.status_code == 2
    assert "restore not committed" in refusal.raw_refusal_text
    assert "BackupArchiveInvalid" in refusal.raw_refusal_text
    # The disposable root is preserved: no adoption, no receipt.
    assert _stored_max_output_tokens(env.root) == 1024
    _assert_idle_database(env.root)
    _assert_no_receipt(env.root)
    assert list((env.root / "retained-installations").iterdir()) == []

    async def commit_fail_closed() -> None:
        runtime = build_runtime(env.root)
        # A torn journal is unattributable evidence: the child must fail
        # closed (exit 3) and preserve the bytes.  The journal is written
        # with the same owner-only mode the landed restore machinery uses —
        # an unsafe mode would be refused at authority acquisition instead.
        journal = env.root / "database" / _RESTORE_JOURNAL
        torn = b'{"journal_version": 1, "restore_tr'
        journal.write_bytes(torn)
        os.chmod(journal, 0o600)
        runtime.handoff.register(
            _RestoreHandoffRequest(package=env.package, expected_backup_id="pinned")
        )
        await runtime.close()
        await _bootstrap_desktop._run_restore_handoff_post_close(runtime)
        assert runtime.restore_exit_code == 3
        assert journal.read_bytes() == torn

    _asyncio.run(commit_fail_closed())

    fail_closed = presented[1]
    assert fail_closed.status_code == 3
    assert "restore failed closed" in fail_closed.raw_refusal_text
    assert "BackupUnclassifiedState" in fail_closed.raw_refusal_text
    assert _stored_max_output_tokens(env.root) == 1024
    # Fail-closed keeps the ambiguous journal bytes for human inspection
    # (never rewritten or guessed away); no receipt was fabricated.
    _assert_no_receipt(env.root)
