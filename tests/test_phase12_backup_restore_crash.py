"""Phase 12 torture: backup/restore crash, corruption, missing-file, stale-lock
and simulated-resource families (subject S — source/runtime only).

Campaign: bots5-linux-v0.1-phase12-torture-20261006-01, workstream
phase12-recovery-persistence, worker W02.  Every case id is ``P12-RP-BKR-<nnn>``
and is recorded in the module docstring of its test, in the workstream evidence
dispatch (REPORT.md / CASES.json) and carries its exact deterministic fault
configuration.  Crash windows use real subprocess death (the established
``tests/test_phase9_restore.py`` pattern) and cold-restart oracles in the
parent process; every killed child is reaped and its exit code is the
reachability witness alongside the child's own ``FAULT_AT`` marker line.

Exit codes reserved by this module: 51-57 (backup family), 71-75 (restore
family), 3 (bounded resource-failure child), 0 (clean child).
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bots5.core.backup import BackupService
from bots5.core.errors import (
    BackupArchiveInvalid,
    BackupError,
    BackupPublicationFailed,
)
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.backup_capture import create_migration_recovery_point
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
    BackupPackageError,
    verify_backup_package,
)
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.restore_service import RestoreService
from tests.test_phase9_restore import (
    HEAD,
    _capture_package,
    _live_sha,
    _make_root,
    _new_authority,
    _stored_max_output_tokens,
)

REPO = Path(__file__).resolve().parents[1]

# Deterministic configuration seeds shared by every case: the target marker is
# 1024, the captured source marker is 2222 (the established Phase 9 seeds).
TARGET_MARKER = 1024
SOURCE_MARKER = 2222

#: The restore transaction journal leaf inside ``database/``.
_RESTORE_JOURNAL = ".bots5-restore-journal.json"
#: The restore receipt leaf inside ``database/``.
_RESTORE_RECEIPT = ".bots5-restore-receipt.json"

#: The private capture-snapshot leaf prefix the capture adapter creates inside
#: the authority-owned ``database/temp`` namespace.
_SNAPSHOT_PREFIX = ".bots5-backup-snapshot-"


# ---------------------------------------------------------------------------
# Child scripts.  Each child is a fresh interpreter so the parent process is a
# genuine cold restart relative to every induced death.  Children print a
# marker line (flushed) immediately before os._exit so the retained log holds
# the exact fault point that killed the process.
# ---------------------------------------------------------------------------

_P12_BACKUP_CHILD = r'''
import json, os, sys
from pathlib import Path
from bots5.core.backup import BackupService
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
)
from bots5.infrastructure.data_root_authority import DataRootAuthority

FAULT = os.environ["P12_FAULT_POINT"]
CODE = int(os.environ["P12_EXIT_CODE"])
HOOKED = os.environ["P12_HOOKED"]
OVERWRITE = os.environ.get("P12_OVERWRITE") == "1"

def die(point):
    if point == FAULT:
        print("FAULT_AT " + point, flush=True)
        os._exit(CODE)

root = Path(sys.argv[1])
dest = Path(sys.argv[2])
authority = DataRootAuthority(root.absolute()).acquire()
try:
    store = authority.open_store()
    try:
        paths = resolve_app_paths(root)
        paths.ensure_non_authoritative()
        package = (
            BackupZipPackageAdapter(fault_hook=die)
            if HOOKED == "package"
            else BackupZipPackageAdapter()
        )
        publisher = (
            BackupFilePublicationAdapter(fault_hook=die)
            if HOOKED == "publication"
            else BackupFilePublicationAdapter()
        )
        capture = RootedBackupCaptureAdapter(
            authority, store, paths, package, data_root_is_override=True
        )
        service = BackupService(
            capture, BackupZipPackageAdapter(), publisher, Uuid7Factory()
        )
        result = service.create_backup(dest, overwrite=OVERWRITE)
        print("BACKUP_OK " + json.dumps({
            "backup_id": result.backup_id,
            "sha256": result.receipt.artifact_sha256,
            "size": result.receipt.artifact_size,
        }), flush=True)
    finally:
        store.close()
finally:
    try:
        authority.close()
    except BaseException:
        pass
os._exit(0)
'''

_P12_RESTORE_CHILD = r'''
import os, sys
from pathlib import Path
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure import restore_service

FAULT = os.environ["P12_FAULT_POINT"]
CODE = int(os.environ["P12_EXIT_CODE"])
VALIDATION_FAILURE = os.environ.get("P12_VALIDATION_FAILURE") == "1"

def die(point):
    if FAULT and point == FAULT:
        print("FAULT_AT " + point, flush=True)
        os._exit(CODE)
    if VALIDATION_FAILURE and point == "post-adoption-validation":
        raise RuntimeError("forced post-adoption validation failure")

restore_service._TEST_FAULT_HOOK = die
authority = DataRootAuthority(Path(sys.argv[1]).absolute()).acquire()
try:
    receipt = restore_service.RestoreService(authority).restore(Path(sys.argv[2]))
    print("RESTORE_OK " + receipt["outcome"], flush=True)
finally:
    try:
        authority.close()
    except BaseException:
        pass
os._exit(CODE)
'''

#: Bounded simulated disk-full: the child ignores SIGXFSZ (so the write fails
#: with EFBIG instead of dying by signal), pins RLIMIT_FSIZE to a deterministic
#: value (database size + 1024 bytes), and classifies its own outcome.
_P12_RLIMIT_CHILD = r'''
import errno as _errno
import json, os, resource, signal, sys
from pathlib import Path
from bots5.core.backup import BackupService
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
)
from bots5.infrastructure.data_root_authority import DataRootAuthority

LIMIT = int(os.environ["P12_FSIZE_LIMIT"])
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(
    resource.RLIMIT_FSIZE,
    (LIMIT, hard if hard != resource.RLIM_INFINITY else LIMIT),
)
points = []

def hook(point):
    points.append(point)

root = Path(sys.argv[1])
dest = Path(sys.argv[2])
authority = DataRootAuthority(root.absolute()).acquire()
try:
    store = authority.open_store()
    try:
        paths = resolve_app_paths(root)
        paths.ensure_non_authoritative()
        capture = RootedBackupCaptureAdapter(
            authority, store, paths, BackupZipPackageAdapter(fault_hook=hook),
            data_root_is_override=True,
        )
        service = BackupService(
            capture, BackupZipPackageAdapter(), BackupFilePublicationAdapter(),
            Uuid7Factory(),
        )
        try:
            service.create_backup(dest)
        except BaseException as exc:
            cause = getattr(exc, "__cause__", None)
            print("FAILED_AT " + json.dumps({
                "type": type(exc).__name__,
                "message": str(exc)[:200],
                "cause_type": type(cause).__name__ if cause is not None else None,
                "cause_errno": getattr(cause, "errno", None),
                "package_points_seen": points,
            }), flush=True)
            print("STAGING_LEFT " + json.dumps(
                sorted(p.name for p in dest.parent.glob(".bots5-backup-*.staging"))
            ), flush=True)
            print("DEST_EXISTS " + str(dest.exists()), flush=True)
            os._exit(3)
        print("BACKUP_OK", flush=True)
        os._exit(0)
    finally:
        store.close()
finally:
    try:
        authority.close()
    except BaseException:
        pass
'''

#: Trivial lock child: takes the flock on one restore transaction lock leaf,
#: reports it, and releases on stdin close.  No product imports.
_P12_LOCK_CHILD = r'''
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("LOCKED", flush=True)
sys.stdin.readline()
os.close(fd)
os._exit(0)
'''


# ---------------------------------------------------------------------------
# Parent-side helpers (every child is reaped; every handle is closed).
# ---------------------------------------------------------------------------


def _run_p12_child(script: str, *argv: Path, env_extra: dict[str, str] | None = None,
                   timeout: int = 300) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": os.fspath(REPO / "src"),
        "QT_QPA_PLATFORM": "offscreen",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", script, *(os.fspath(a) for a in argv)],
        cwd=REPO,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _backup_child(root: Path, dest: Path, fault: str, code: int, hooked: str,
                  overwrite: bool = False) -> subprocess.CompletedProcess:
    return _run_p12_child(
        _P12_BACKUP_CHILD, root, dest,
        env_extra={
            "P12_FAULT_POINT": fault,
            "P12_EXIT_CODE": str(code),
            "P12_HOOKED": hooked,
            "P12_OVERWRITE": "1" if overwrite else "0",
        },
    )


def _restore_child(root: Path, package: Path, fault: str, code: int,
                   validation_failure: bool = False) -> subprocess.CompletedProcess:
    return _run_p12_child(
        _P12_RESTORE_CHILD, root, package,
        env_extra={
            "P12_FAULT_POINT": fault,
            "P12_EXIT_CODE": str(code),
            "P12_VALIDATION_FAILURE": "1" if validation_failure else "0",
        },
    )


def _assert_crash_witness(done: subprocess.CompletedProcess, fault: str, code: int) -> None:
    """Reachability witness: the child died at the named point, by our hand."""
    assert done.returncode == code, (done.returncode, done.stdout, done.stderr[-2000:])
    assert f"FAULT_AT {fault}" in done.stdout, done.stdout


def _temp_leaves(root: Path) -> list[str]:
    temp = root / "database" / "temp"
    return sorted(p.name for p in temp.iterdir()) if temp.exists() else []


def _cold_acquire_outcome(root: Path) -> str:
    try:
        authority = DataRootAuthority(root.absolute()).acquire()
    except BaseException as exc:  # noqa: BLE001 - the refusal is the observation
        return f"{type(exc).__name__}: {exc}"
    try:
        authority.close()
    except BaseException:
        pass
    return "ACQUIRED"


def _remediate_capture_snapshot(root: Path) -> list[str]:
    """Manual remediation of the stranded capture snapshot: only leaves the
    capture adapter itself names are removed (disposable test state)."""
    removed: list[str] = []
    for name in _temp_leaves(root):
        assert name.startswith(_SNAPSHOT_PREFIX), name
        (root / "database" / "temp" / name).unlink()
        removed.append(name)
    return removed


def _cold_usability(root: Path) -> tuple[int, str]:
    """Cold-process oracle: a fresh authority serves the installation and a
    fresh authoritative internal recovery point publishes successfully."""
    first = DataRootAuthority(root.absolute()).acquire()
    try:
        store = first.open_store()
        try:
            marker = int(store.get_application_generation_settings().max_output_tokens)
        finally:
            store.close()  # closing the store closes its authority
    except BaseException:
        try:
            first.close()
        except BaseException:
            pass
        raise
    second = DataRootAuthority(root.absolute()).acquire()
    try:
        second._claim_database()
        whole = create_migration_recovery_point(second, Uuid7Factory().new())
        return marker, str(whole["published_leaf"])
    finally:
        try:
            second.close()
        except BaseException:
            pass


def _rebuild_package(
    source: Path,
    destination: Path,
    *,
    manifest_fn=None,
    write_completed: bool = True,
    include_database: bool = True,
    duplicate_database: bool = False,
    flip_database_offset: int | None = None,
) -> None:
    """Deterministically rebuild a Backup v1 ZIP with one exact tamper.

    The manifest is re-canonicalised (``canonical_backup_json``) and the
    logical content digest recomputed unless ``manifest_fn`` re-applies a tamper
    after canonicalisation, so each case differs from a valid package by
    exactly the injected corruption.
    """
    from bots5.domain.backup import canonical_backup_json, logical_content_digest_from_rows

    with zipfile.ZipFile(source, "r") as package:
        files = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(files["manifest.json"])
    files.pop("manifest.json")
    completed = files.pop("COMPLETED", b"")
    if flip_database_offset is not None:
        database = bytearray(files["database/state.sqlite3"])
        database[flip_database_offset] ^= 0xFF
        files["database/state.sqlite3"] = bytes(database)
    if manifest_fn is None:
        for row in manifest["entry_inventory"]:
            path = str(row["path"])
            if path in files:
                row["uncompressed_size"] = len(files[path])
                row["sha256"] = hashlib.sha256(files[path]).hexdigest()
    else:
        manifest = manifest_fn(manifest)
    if include_database is False:
        files.pop("database/state.sqlite3", None)
    manifest["declared_total_uncompressed_bytes"] = sum(
        row["uncompressed_size"] for row in manifest["entry_inventory"]
    )
    manifest["logical_content_digest"] = logical_content_digest_from_rows(
        manifest["entry_inventory"]
    )
    with zipfile.ZipFile(destination, "w", allowZip64=True) as package:
        package.writestr("manifest.json", canonical_backup_json(manifest))
        names = sorted(files)
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            package.writestr(info, files[name])
            if duplicate_database and name == "database/state.sqlite3":
                package.writestr(info, files[name])
        if write_completed:
            package.writestr("COMPLETED", completed)


class _StagingDisappearingPublisher(BackupFilePublicationAdapter):
    """Simulates the staged leaf vanishing between verification and rename.

    The staged leaf name is only known once ``create_backup`` has generated the
    operation id, so the disappearance targets the ``staging_path`` handed to
    ``publish`` (the exact leaf that verification just accepted).
    """

    def publish(self, *, staging_path: Path, destination: Path, overwrite: bool,
                backup_id: str, cancellation=None) -> Path:
        os.unlink(staging_path)
        return super().publish(
            staging_path=staging_path,
            destination=destination,
            overwrite=overwrite,
            backup_id=backup_id,
            cancellation=cancellation,
        )


def _p12_backup_service(authority, store, paths, *, publisher=None) -> BackupService:
    """The established in-process backup service wiring (subject S)."""
    from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter

    capture = RootedBackupCaptureAdapter(
        authority, store, paths, BackupZipPackageAdapter(), data_root_is_override=True
    )
    return BackupService(
        capture,
        BackupZipPackageAdapter(),
        publisher if publisher is not None else BackupFilePublicationAdapter(),
        Uuid7Factory(),
    )


def _open_store_marker(root: Path) -> int:
    """Fresh authority + product store marker query (authority closed after)."""
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        store = authority.open_store()
        try:
            return int(store.get_application_generation_settings().max_output_tokens)
        finally:
            store.close()  # closing the store closes its authority
    except BaseException:
        try:
            authority.close()
        except BaseException:
            pass
        raise


def _raw_marker(root: Path) -> int:
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    try:
        row = connection.execute(
            "SELECT max_output_tokens FROM application_generation_config WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _integrity_check(root: Path) -> str:
    connection = sqlite3.connect(root / "database" / "state.sqlite3")
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
def p12_restore_env(tmp_path: Path) -> SimpleNamespace:
    """One captured, independently verified package plus a live target root."""
    package_path, receipt = _capture_package(tmp_path, "source", SOURCE_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    portable = outside / "restore.botsbackup"
    shutil.copyfile(package_path, portable)
    target_root = _make_root(tmp_path, "target", TARGET_MARKER)
    return SimpleNamespace(
        root=target_root,
        package=portable,
        backup_id=str(receipt["backup_id"]),
        artifact_sha256=str(receipt["artifact_sha256"]),
    )


# ---------------------------------------------------------------------------
# Family 1: crash/kill around BACKUP (subprocess death at named fault points,
# cold-process oracle).  Subject S.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        pytest.param("after-entry:database/state.sqlite3", 51, id="P12-RP-BKR-001"),
        pytest.param("after-completion-marker", 52, id="P12-RP-BKR-002"),
        pytest.param("after-staging-file-fsync", 53, id="P12-RP-BKR-003"),
    ],
)
def test_backup_capture_stage_death_strands_private_snapshot_and_next_acquire_fails_closed(
    tmp_path, fault, code
):
    """P12-RP-BKR-001/002/003 (subject S; subsystem backup capture, lifecycle
    point: package staging write inside the recovery fence).

    Fault: the child process is hard-killed (os._exit(code)) by the
    ``BackupZipPackageAdapter`` fault hook at the named ``write_backup_package``
    point, after ``_snapshot_database`` created the private snapshot leaf in
    the authority-owned ``database/temp`` namespace, so no cleanup runs.
    Deterministic configuration: marker-1024 root at HEAD (0020), destination
    ``<tmp>/outside/probe.botsbackup`` (outside the data root), fault point and
    exit code exactly as parametrized above.

    Witness: child exit code == code and its flushed ``FAULT_AT`` line; the
    stranded snapshot leaf observed in ``database/temp``; destination absent
    (no partial publication); exactly one ``.bots5-backup-*.staging`` debris
    leaf in the destination directory.

    Durable postcondition observed (finding P12-RP-FINDING-W02-01): the next
    authority acquire FAILS CLOSED with ``AuthorityError: database temporary
    directory contains unexplained state`` — the data root is not usable until
    an operator removes the stranded leaf.  After removing only the
    ``.bots5-backup-snapshot-*`` leaf, a fresh authority serves the installation
    (marker 1024) and a fresh internal recovery point publishes, proving the
    live database was never corrupted.
    """
    root = _make_root(tmp_path, "capture-death", TARGET_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    done = _backup_child(root, dest, fault, code, "package")
    _assert_crash_witness(done, fault, code)
    stranded = _temp_leaves(root)
    assert stranded, "capture-stage death must have stranded the private snapshot leaf"
    assert all(name.startswith(_SNAPSHOT_PREFIX) for name in stranded), stranded
    assert not dest.exists(), "no authoritative publication may exist"
    staging = sorted(p.name for p in outside.glob(".bots5-backup-*.staging"))
    assert len(staging) == 1, staging  # the killed transaction's own staging leaf
    # Cold restart without remediation: fail-closed refusal, never silent
    # progress, and the refusal names the temporary directory.
    outcome = _cold_acquire_outcome(root)
    assert outcome.startswith("AuthorityError:"), outcome
    assert "database temporary directory contains unexplained state" in outcome
    # Manual remediation (disposable state): remove only the capture's own
    # stranded leaf, then the root must serve again and publish afresh.
    removed = _remediate_capture_snapshot(root)
    assert removed == stranded
    marker, published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER
    assert published_leaf.startswith("bots5-backup-")
    assert _raw_marker(root) == TARGET_MARKER


def test_backup_publication_before_rename_new_destination_publishes_nothing(
    tmp_path,
):
    """P12-RP-BKR-004 (subject S; subsystem backup publication, lifecycle
    point: same-directory renameat2, new-destination path, pre-rename).

    Fault: subprocess death at the ``BackupFilePublicationAdapter`` fault point
    ``before-rename`` (overwrite=False), exit code 54, on a marker-1024 HEAD
    root with destination ``<tmp>/outside/probe.botsbackup``.

    Witness: child exit code 54 + ``FAULT_AT before-rename``; destination
    absent; ``database/temp`` empty (capture had already completed and cleaned
    up); exactly one staging debris leaf.

    Durable postcondition: no authoritative partial publication exists; a
    fresh authority serves marker 1024 and publishes a fresh internal recovery
    point — the data root is intact and still usable.
    """
    root = _make_root(tmp_path, "pub-before-rename", TARGET_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    done = _backup_child(root, dest, "before-rename", 54, "publication")
    _assert_crash_witness(done, "before-rename", 54)
    assert _temp_leaves(root) == []
    assert not dest.exists()
    assert len(list(outside.glob(".bots5-backup-*.staging"))) == 1
    marker, published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER
    assert published_leaf.startswith("bots5-backup-")


def test_backup_publication_after_rename_new_destination_is_complete_and_verified(
    tmp_path,
):
    """P12-RP-BKR-005 (subject S; subsystem backup publication, lifecycle
    point: post-rename, new-destination path).

    Fault: subprocess death at the ``BackupFilePublicationAdapter`` fault point
    ``after-rename`` (overwrite=False), exit code 55, after the atomic
    same-directory renameat2 landed the staged bytes at the destination.

    Witness: child exit code 55 + ``FAULT_AT after-rename``.

    Durable postcondition: the destination exists and a fresh, artifact-only
    ``verify_backup_package`` in the parent process accepts it (complete +
    verified — the rename boundary guarantees no partial artifact can ever be
    observed under the published name), and the root stays usable.
    """
    root = _make_root(tmp_path, "pub-after-rename", TARGET_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    done = _backup_child(root, dest, "after-rename", 55, "publication")
    _assert_crash_witness(done, "after-rename", 55)
    assert _temp_leaves(root) == []
    assert dest.exists()
    receipt = verify_backup_package(dest)
    assert receipt.artifact_size == dest.stat().st_size
    assert receipt.artifact_sha256 == hashlib.sha256(dest.read_bytes()).hexdigest()
    marker, _published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER


def test_backup_overwrite_crash_before_exchange_preserves_old_backup(tmp_path):
    """P12-RP-BKR-006 (subject S; subsystem backup publication, lifecycle
    point: overwrite path, pre-exchange).

    Fault: after seeding a real published backup (clean child, exit 0), a
    second child is killed at ``before-rename`` on the overwrite path (exit 56)
    before the renameat2 exchange.  Deterministic configuration: destination
    ``<tmp>/outside/probe.botsbackup``; the seeded artifact sha256 is recorded
    from the child's ``BACKUP_OK`` JSON line.

    Witness: faulted child exit code 56 + ``FAULT_AT before-rename``.

    Durable postcondition: the OLD backup survives byte-identical at the
    destination (sha256 equal to the seeded artifact) and still verifies; the
    replacement never partially landed; the root stays usable.
    """
    root = _make_root(tmp_path, "pub-overwrite-before", TARGET_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    seed = _backup_child(root, dest, "NONE", 0, "package", overwrite=False)
    assert seed.returncode == 0, seed.stderr[-2000:]
    old_sha = str(
        json.loads(seed.stdout.strip().splitlines()[-1].removeprefix("BACKUP_OK "))["sha256"]
    )
    done = _backup_child(root, dest, "before-rename", 56, "publication", overwrite=True)
    _assert_crash_witness(done, "before-rename", 56)
    assert _temp_leaves(root) == []
    assert dest.exists()
    receipt = verify_backup_package(dest)
    assert receipt.artifact_sha256 == old_sha
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == old_sha
    assert not list(outside.glob(".bots5-backup-replaced-*"))
    marker, _published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER


def test_backup_overwrite_crash_after_exchange_lands_verified_replacement(tmp_path):
    """P12-RP-BKR-007 (subject S; subsystem backup publication, lifecycle
    point: overwrite path, post-exchange).

    Fault: identical to P12-RP-BKR-006 but the child dies at ``after-rename``
    (exit code 57), immediately after the renameat2 exchange made the
    destination the new artifact.

    Witness: faulted child exit code 57 + ``FAULT_AT after-rename``.

    Durable postcondition: the destination is the NEW package and verifies;
    the displaced old package survives in the transaction's staging leaf and
    itself still verifies (exact exchange semantics); no ``replaced`` leaf was
    created yet; the root stays usable.
    """
    root = _make_root(tmp_path, "pub-overwrite-after", TARGET_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    seed = _backup_child(root, dest, "NONE", 0, "package", overwrite=False)
    assert seed.returncode == 0, seed.stderr[-2000:]
    old_sha = str(
        json.loads(seed.stdout.strip().splitlines()[-1].removeprefix("BACKUP_OK "))["sha256"]
    )
    done = _backup_child(root, dest, "after-rename", 57, "publication", overwrite=True)
    _assert_crash_witness(done, "after-rename", 57)
    assert _temp_leaves(root) == []
    assert dest.exists()
    new_receipt = verify_backup_package(dest)
    assert new_receipt.artifact_sha256 != old_sha
    staging = list(outside.glob(".bots5-backup-*.staging"))
    assert len(staging) == 1
    displaced = verify_backup_package(staging[0])
    assert displaced.artifact_sha256 == old_sha
    assert not list(outside.glob(".bots5-backup-replaced-*"))
    marker, _published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER


# ---------------------------------------------------------------------------
# Family 2: crash/kill around RESTORE (subprocess death at journal and
# adopt/exchange fault points; cold reconcile oracle per the established
# adopt-intent pattern).  Subject S.
# ---------------------------------------------------------------------------


def test_restore_death_before_adopt_intent_journal_write_aborts_as_scratch(
    tmp_path, p12_restore_env
):
    """P12-RP-BKR-008 (subject S; subsystem restore transaction, lifecycle
    point: journal write before the ADOPT_INTENT phase record).

    Fault: subprocess death (os._exit(71)) at the restore fault point
    ``before-journal-write-ADOPT_INTENT`` — the STAGED_VALIDATED journal is the
    last durable write; the adoption exchange never ran.

    Witness: child exit code 71 + ``FAULT_AT`` line; durable journal phase is
    STAGED_VALIDATED.

    Cold postcondition: reconcile attributes a scratch transaction and aborts
    (no receipt); journal, candidate leaf and private payload staging are
    withdrawn in the same reconcile; the transaction lock leaf is withdrawn by
    the NEXT idle reconcile (the W1 orphan-lock sweep — a deferred, two-step
    deterministic convergence, unlike the ADOPT_INTENT abort path which unlinks
    the lock in the same reconcile); the live database is byte-identical to the
    pre-crash installation and still serves marker 1024; no authority poison.
    """
    env = p12_restore_env
    before = _live_sha(env.root)
    done = _restore_child(env.root, env.package, "before-journal-write-ADOPT_INTENT", 71)
    _assert_crash_witness(done, "before-journal-write-ADOPT_INTENT", 71)
    journal = env.root / "database" / _RESTORE_JOURNAL
    assert journal.exists()
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "STAGED_VALIDATED"
    txid = str(record["restore_transaction_id"])
    lock_leaf = env.root / "database" / f".bots5-restore-{txid}.lock"
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert summary["restart_required"] is False
        assert summary["receipt"] is None
        assert summary["transaction_id"] == txid
        database = env.root / "database"
        assert not journal.exists()
        assert not (database / f".bots5-restore-{txid}.candidate.sqlite3").exists()
        assert not (env.root / "attachments" / "staging" / f"restore-{txid}").exists()
        assert not authority.poison_pending
        # Deferred lock-leaf withdrawal (W1 orphan sweep on the idle path).
        assert lock_leaf.exists()
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    authority = _new_authority(env.root)
    try:
        assert RestoreService(authority).reconcile() is None
        assert not lock_leaf.exists()
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert _live_sha(env.root) == before
    assert _raw_marker(env.root) == TARGET_MARKER
    assert _open_store_marker(env.root) == TARGET_MARKER


def test_restore_death_before_adopt_exchange_aborts_without_live_mutation(
    tmp_path, p12_restore_env
):
    """P12-RP-BKR-009 (subject S; subsystem restore transaction, lifecycle
    point: immediately before the adopt renameat2 exchange).

    Fault: subprocess death (os._exit(72)) at ``before-adopt-exchange`` — the
    journal is ADOPT_INTENT but the exchange itself never happened.

    Witness: child exit code 72 + ``FAULT_AT`` line; durable journal phase is
    ADOPT_INTENT; the live database is still byte-identical to the pre-crash
    installation (proving the fault point precedes any live mutation).

    Cold postcondition: reconcile attributes the live installation to the
    recorded preservation identity and aborts cleanly; no receipt; journal,
    candidate, lock and staging withdrawn; marker 1024 still served; no poison.
    """
    env = p12_restore_env
    before = _live_sha(env.root)
    done = _restore_child(env.root, env.package, "before-adopt-exchange", 72)
    _assert_crash_witness(done, "before-adopt-exchange", 72)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "ADOPT_INTENT"
    assert record["preservation_record"] is not None
    assert _live_sha(env.root) == before
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "aborted"
        assert summary["restart_required"] is False
        assert summary["receipt"] is None
        database = env.root / "database"
        assert not journal.exists()
        assert not list(database.glob(".bots5-restore-*.lock"))
        assert not list(database.glob(".bots5-restore-*.candidate.sqlite3"))
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert _live_sha(env.root) == before
    assert _open_store_marker(env.root) == TARGET_MARKER


@pytest.mark.parametrize(
    ("fault", "code", "phase"),
    [
        pytest.param("after-journal-write-PROMOTED", 73, "PROMOTED", id="P12-RP-BKR-010"),
        pytest.param(
            "after-journal-write-POST_ADOPTION_VALIDATED",
            74,
            "POST_ADOPTION_VALIDATED",
            id="P12-RP-BKR-011",
        ),
    ],
)
def test_restore_death_after_adoption_completes_forward_on_restart(
    tmp_path, p12_restore_env, fault, code, phase
):
    """P12-RP-BKR-010/011 (subject S; subsystem restore transaction, lifecycle
    points: after the PROMOTED / POST_ADOPTION_VALIDATED journal writes).

    Fault: subprocess death (exit 73/74) after the named journal write — the
    adoption exchange has atomically made the candidate the canonical main, so
    the durable journal plus the live bytes are past the point of no return.

    Witness: child exit code + ``FAULT_AT`` line; durable journal phase as
    parametrized; the live canonical database is byte-different from the
    pre-crash installation and already serves marker 2222.

    Cold postcondition: reconcile converges forward (action ``completed``) with
    a RESTORED receipt naming the captured backup id; the journal and lock
    leaves are withdrawn while the displaced pre-restore installation remains
    byte-preserved at the candidate leaf and in the retained-installations
    artefact (D-A=A.2: the displaced installation is never auto-removed); a
    fresh authority serves marker 2222; no poison; no half-adopted residue.
    """
    env = p12_restore_env
    before = _live_sha(env.root)
    done = _restore_child(env.root, env.package, fault, code)
    _assert_crash_witness(done, fault, code)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == phase
    txid = str(record["restore_transaction_id"])
    assert _live_sha(env.root) != before
    assert _raw_marker(env.root) == SOURCE_MARKER
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "completed"
        assert summary["restart_required"] is False
        assert summary["receipt"] is not None
        assert summary["receipt"]["backup_id"] == env.backup_id
        assert summary["receipt"]["outcome"] == "RESTORED"
        database = env.root / "database"
        assert not journal.exists()
        assert not list(database.glob(".bots5-restore-*.lock"))
        # D-A=A.2: the displaced pre-restore installation survives commit as
        # attributable evidence, byte-identical, at the candidate leaf and in
        # the retained-installations preservation artefact.
        displaced = database / str(record["candidate_leaf"])
        assert displaced.exists()
        assert hashlib.sha256(displaced.read_bytes()).hexdigest() == before
        artefact = env.root / "retained-installations" / f"{txid}-{env.backup_id}.sqlite3"
        assert artefact.exists()
        assert hashlib.sha256(artefact.read_bytes()).hexdigest() == before
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert _live_sha(env.root) != before
    assert _open_store_marker(env.root) == SOURCE_MARKER


def test_restore_death_after_rollback_exchange_awaits_restart_on_original(
    tmp_path, p12_restore_env
):
    """P12-RP-BKR-012 (subject S; subsystem restore rollback machine, lifecycle
    point: immediately after the rollback re-exchange, before the terminal
    ROLLED_BACK journal write).

    Fault: the forced post-adoption validation failure arms the rollback
    machine (journal RESTORE_ROLLBACK_INTENT), the re-exchange restores the
    preserved installation, and the child dies (os._exit(75)) at
    ``after-rollback-exchange``.

    Witness: child exit code 75 + ``FAULT_AT`` line; durable journal phase is
    RESTORE_ROLLBACK_INTENT; the live database is already byte-identical to the
    pre-crash installation (the re-exchange completed).

    Cold postcondition: reconcile completes or repeats the rollback
    deterministically (W7): first reconcile revalidates against the recorded
    preservation identity, writes ROLLED_BACK and reports awaiting-restart;
    the second reconcile withdraws the journal; the live installation still
    serves marker 1024 with unchanged bytes; no poison.
    """
    env = p12_restore_env
    before = _live_sha(env.root)
    done = _restore_child(
        env.root, env.package, "after-rollback-exchange", 75, validation_failure=True
    )
    _assert_crash_witness(done, "after-rollback-exchange", 75)
    journal = env.root / "database" / _RESTORE_JOURNAL
    record = json.loads(journal.read_text("utf-8"))
    assert record["phase"] == "RESTORE_ROLLBACK_INTENT"
    txid = str(record["restore_transaction_id"])
    assert _live_sha(env.root) == before
    assert _raw_marker(env.root) == TARGET_MARKER
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["action"] == "awaiting-restart"
        assert summary["restart_required"] is True
        assert json.loads(journal.read_text("utf-8"))["phase"] == "ROLLED_BACK"
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    authority = _new_authority(env.root)
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is not None
        assert summary["restart_required"] is True
        assert not journal.exists()
        assert not (env.root / "database" / f".bots5-restore-{txid}.lock").exists()
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert _live_sha(env.root) == before
    assert _open_store_marker(env.root) == TARGET_MARKER


def test_cold_process_post_restore_oracle_proves_durability(tmp_path, p12_restore_env):
    """P12-RP-BKR-013 (subject S; subsystem restore commit, lifecycle point:
    successful whole-installation restore, durability oracle).

    Fault: none — the child performs one complete restore and exits cleanly
    (exit code 0, ``RESTORE_OK RESTORED``).

    Witness: child exit code 0 + ``RESTORE_OK RESTORED`` line.

    Cold-process oracle (family 6): a fresh process re-acquires the authority
    and the restored state SERVES: the product store answers marker 2222, a raw
    SQLite ``PRAGMA integrity_check`` on the canonical main returns ``ok``, the
    durable receipt records outcome RESTORED with the captured backup id, and
    no journal or lock leaves remain while the displaced pre-restore
    installation stays byte-preserved at the candidate leaf (D-A=A.2).
    """
    env = p12_restore_env
    before = _live_sha(env.root)
    done = _restore_child(env.root, env.package, "NONE", 0)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr[-2000:])
    assert "RESTORE_OK RESTORED" in done.stdout
    database = env.root / "database"
    assert not (database / _RESTORE_JOURNAL).exists()
    assert not list(database.glob(".bots5-restore-*.lock"))
    receipt = json.loads((database / _RESTORE_RECEIPT).read_text("utf-8"))
    assert receipt["outcome"] == "RESTORED"
    assert receipt["backup_id"] == env.backup_id
    # D-A=A.2: the displaced installation survives commit, byte-preserved.
    displaced = database / f".bots5-restore-{receipt['transaction_id']}.candidate.sqlite3"
    assert displaced.exists()
    assert hashlib.sha256(displaced.read_bytes()).hexdigest() == before
    artefact = (
        env.root / "retained-installations"
        / f"{receipt['transaction_id']}-{env.backup_id}.sqlite3"
    )
    assert artefact.exists()
    assert hashlib.sha256(artefact.read_bytes()).hexdigest() == before
    assert _live_sha(env.root) != before
    assert _open_store_marker(env.root) == SOURCE_MARKER
    assert _raw_marker(env.root) == SOURCE_MARKER
    assert _integrity_check(env.root) == "ok"


# ---------------------------------------------------------------------------
# Family 3: corrupt hashes/manifests/payloads on a completed backup package —
# verify/restore must refuse fail-closed before any authoritative mutation.
# Subject S.
# ---------------------------------------------------------------------------


def _corrupted_package(tmp_path: Path, name: str, **kwargs) -> Path:
    package_path, _receipt = _capture_package(tmp_path, f"source-{name}", SOURCE_MARKER)
    portable = tmp_path / "outside" / f"{name}.botsbackup"
    portable.parent.mkdir(exist_ok=True)
    shutil.copyfile(package_path, portable)
    target = tmp_path / "outside" / f"{name}-corrupt.botsbackup"
    _rebuild_package(portable, target, **kwargs)
    return target


def test_tampered_manifest_entry_digest_is_refused_at_verification(tmp_path):
    """P12-RP-BKR-014 (subject S; subsystem backup verification, boundary:
    manifest entry inventory digest vs packaged bytes).

    Tamper recipe: rebuild the package with ``entry_inventory`` row sha256 for
    ``database/state.sqlite3`` set to 64 zeroes, everything else re-canonical
    (logical digest recomputed over the tampered row).

    Witness: ``BackupPackageError: backup entry digest or size is inconsistent``
    raised by ``verify_backup_package`` at the verification boundary.
    """

    def tamper(manifest):
        for row in manifest["entry_inventory"]:
            if str(row["path"]) == "database/state.sqlite3":
                row["sha256"] = "0" * 64
        return manifest

    target = _corrupted_package(tmp_path, "manifest-entry-digest", manifest_fn=tamper)
    with pytest.raises(BackupPackageError, match="backup entry digest or size is inconsistent"):
        verify_backup_package(target)


def test_tampered_capture_database_digest_is_refused_at_verification(tmp_path):
    """P12-RP-BKR-015 (subject S; subsystem backup verification, boundary:
    capture-level packaged SQLite digest).

    Tamper recipe: rebuild with ``capture.source_database_sha256`` set to 64
    ones and per-entry rows recomputed (so only the capture-level digest lies).

    Witness: ``BackupPackageError: packaged SQLite digest or size is
    inconsistent`` raised by ``verify_backup_package``.
    """

    def tamper(manifest):
        manifest["capture"]["source_database_sha256"] = "1" * 64
        return manifest

    target = _corrupted_package(tmp_path, "capture-digest", manifest_fn=tamper)
    with pytest.raises(BackupPackageError, match="packaged SQLite digest or size is inconsistent"):
        verify_backup_package(target)


def test_flipped_payload_byte_refuses_verification_and_restore_without_live_mutation(
    tmp_path, p12_restore_env
):
    """P12-RP-BKR-016 (subject S; subsystem backup verification + whole-
    installation restore, boundaries: artifact digest then restore gate).

    Tamper recipe A: flip byte 4096 of the packaged ``database/state.sqlite3``
    member (XOR 0xFF) and recompute the manifest entry row digest to match the
    corrupted bytes, so only the capture-level database digest still binds the
    original bytes.

    Witness A: ``verify_backup_package`` refuses with ``BackupPackageError:
    packaged SQLite digest or size is inconsistent`` (the capture-level
    boundary).

    Tamper recipe B: flip the same byte but keep the manifest bytes exactly as
    captured, so the declared entry inventory no longer matches the packaged
    bytes.

    Witness B: ``verify_backup_package`` refuses with ``BackupPackageError:
    backup entry digest or size is inconsistent`` (the entry-inventory
    boundary).

    Witness C (restore-level, fail-closed before any authoritative mutation):
    ``RestoreService.restore`` refuses recipe B with ``BackupArchiveInvalid``
    (the same message); afterwards the target root has no restore journal, no
    transaction lock, no candidate leaf, no receipt, an empty
    ``retained-installations`` namespace, no authority poison, a byte-identical
    live database and marker 1024 still served.
    """
    env = p12_restore_env
    before = _live_sha(env.root)
    # Recipe A: corrupted bytes with a maliciously recomputed row digest.
    recipe_a = env.package.parent / "flipped-recomputed.botsbackup"
    _rebuild_package(env.package, recipe_a, flip_database_offset=4096)
    with pytest.raises(
        BackupPackageError, match="packaged SQLite digest or size is inconsistent"
    ):
        verify_backup_package(recipe_a)
    # Recipe B: corrupted bytes, manifest bytes left exactly as captured.
    recipe_b = env.package.parent / "flipped.botsbackup"
    with zipfile.ZipFile(env.package, "r") as package:
        members = {name: package.read(name) for name in package.namelist()}
    database = bytearray(members["database/state.sqlite3"])
    database[4096] ^= 0xFF
    members["database/state.sqlite3"] = bytes(database)
    with zipfile.ZipFile(recipe_b, "w", allowZip64=True) as package:
        for name in ("manifest.json", "database/state.sqlite3", "COMPLETED"):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            package.writestr(info, members[name])
    with pytest.raises(
        BackupPackageError, match="backup entry digest or size is inconsistent"
    ):
        verify_backup_package(recipe_b)
    authority = _new_authority(env.root)
    try:
        with pytest.raises(
            BackupArchiveInvalid, match="backup entry digest or size is inconsistent"
        ):
            RestoreService(authority).restore(recipe_b)
        database = env.root / "database"
        assert not (database / _RESTORE_JOURNAL).exists()
        assert not (database / _RESTORE_RECEIPT).exists()
        assert not list(database.glob(".bots5-restore-*.lock"))
        assert not list(database.glob(".bots5-restore-*.tmp"))
        assert not list(database.glob(".bots5-restore-*.candidate.sqlite3"))
        assert list((env.root / "retained-installations").iterdir()) == []
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert _live_sha(env.root) == before
    assert _raw_marker(env.root) == TARGET_MARKER


def test_truncated_central_directory_is_refused(tmp_path):
    """P12-RP-BKR-017 (subject S; subsystem backup verification, boundary: ZIP
    central directory integrity).

    Tamper recipe: drop the final 512 bytes of the completed package
    (``dest.write_bytes(raw[:-512])``).

    Witness: ``BackupPackageError: backup ZIP central directory is truncated``.
    """
    package_path, _receipt = _capture_package(tmp_path, "source-trunc", SOURCE_MARKER)
    portable = tmp_path / "outside" / "trunc.botsbackup"
    portable.parent.mkdir(exist_ok=True)
    shutil.copyfile(package_path, portable)
    target = tmp_path / "outside" / "trunc-corrupt.botsbackup"
    target.write_bytes(portable.read_bytes()[:-512])
    with pytest.raises(BackupPackageError, match="backup ZIP central directory is truncated"):
        verify_backup_package(target)


def test_removed_completed_member_is_refused(tmp_path):
    """P12-RP-BKR-018 (subject S; subsystem backup verification, boundary:
    closed ZIP member set / entry order).

    Tamper recipe: rebuild the package without the ``COMPLETED`` member while
    the manifest still declares it.

    Witness: ``BackupPackageError: backup ZIP entry order is invalid``.
    """
    target = _corrupted_package(tmp_path, "no-completed", write_completed=False)
    with pytest.raises(BackupPackageError, match="backup ZIP entry order is invalid"):
        verify_backup_package(target)


def test_removed_database_member_is_refused(tmp_path):
    """P12-RP-BKR-019 (subject S; subsystem backup verification, boundary:
    declared-database presence).

    Tamper recipe: rebuild the package without the ``database/state.sqlite3``
    member; the manifest still declares it.

    Witness: ``BackupPackageError: backup ZIP entry order is invalid``.
    """
    target = _corrupted_package(tmp_path, "no-database", include_database=False)
    with pytest.raises(BackupPackageError, match="backup ZIP entry order is invalid"):
        verify_backup_package(target)


def test_duplicated_zip_entry_is_refused(tmp_path):
    """P12-RP-BKR-020 (subject S; subsystem backup verification, boundary:
    duplicate central-directory entries).

    Tamper recipe: rebuild the package writing ``database/state.sqlite3``
    twice under one canonical manifest.

    Witness: ``BackupPackageError: backup ZIP contains duplicate entries``.
    """
    target = _corrupted_package(tmp_path, "duplicate-entry", duplicate_database=True)
    with pytest.raises(BackupPackageError, match="backup ZIP contains duplicate entries"):
        verify_backup_package(target)


# ---------------------------------------------------------------------------
# Family 4: missing files and stale locks — refuse or reconcile per design,
# never silently proceed.  Subject S.
# ---------------------------------------------------------------------------


def test_staged_leaf_deleted_before_publication_rename_refuses(tmp_path):
    """P12-RP-BKR-021 (subject S; subsystem backup publication, lifecycle
    point: between artifact verification and the publication rename).

    Fault: the staged leaf is removed after verification and immediately before
    ``BackupFilePublicationAdapter.publish`` (simulated external disappearance
    of the staged file), on a marker-1024 HEAD root with destination
    ``<tmp>/outside/probe.botsbackup``.

    Witness: ``BackupPublicationFailed: backup publication rename failed``
    raised at the publication rename boundary.

    Durable postcondition: the destination does not exist, no staging leaf
    remains (the owned-staging cleanup is a no-op on the vanished leaf),
    ``database/temp`` is empty, and a fresh authority serves marker 1024 and
    publishes a fresh internal recovery point.
    """
    root = _make_root(tmp_path, "staged-vanish", TARGET_MARKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        store = authority.open_store()
        try:
            paths = resolve_app_paths(root)
            paths.ensure_non_authoritative()
            service = _p12_backup_service(
                authority, store, paths,
                publisher=_StagingDisappearingPublisher(),
            )
            with pytest.raises(BackupPublicationFailed, match="backup publication rename failed"):
                service.create_backup(dest)
        finally:
            store.close()
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert not dest.exists()
    assert list(outside.glob(".bots5-backup-*.staging")) == []
    assert _temp_leaves(root) == []
    marker, published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER
    assert published_leaf.startswith("bots5-backup-")


def test_held_restore_transaction_lock_refuses_and_orphan_is_swept(tmp_path):
    """P12-RP-BKR-022 (subject S; subsystem restore transaction lock, lifecycle
    points: live-held lock and unheld orphan lock leaf from a killed run).

    Fault: a live foreign process holds ``flock(LOCK_EX)`` on
    ``database/.bots5-restore-<txid>.lock`` (a real uuid7 transaction id);
    afterwards the process exits, leaving the unheld leaf (the durable state a
    killed restorer leaves behind).

    Witness 1 (held): ``RestoreService.reconcile`` refuses with ``BackupError:
    restore transaction lock is held; a restore transaction may be live`` —
    never silent progress.  Witness 2 (orphan): after the holder exits (child
    reaped, exit 0), reconcile returns None (idle) and the orphan leaf is
    swept; the authority stays unpoisoned.
    """
    root = _make_root(tmp_path, "lock-root", TARGET_MARKER)
    txid = str(Uuid7Factory().new())
    lock_leaf = root / "database" / f".bots5-restore-{txid}.lock"
    child = subprocess.Popen(
        [sys.executable, "-c", _P12_LOCK_CHILD, os.fspath(lock_leaf)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        assert child.stdout.readline().strip() == "LOCKED"
        authority = DataRootAuthority(root.absolute()).acquire()
        try:
            with pytest.raises(BackupError, match="restore transaction lock is held"):
                RestoreService(authority).reconcile()
            assert not authority.poison_pending
        finally:
            try:
                authority.close()
            except BaseException:
                pass
    finally:
        # Release the lock and reap the child on every path (no zombies).
        try:
            child.communicate(input="release\n", timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=60)
            raise
    assert child.returncode == 0, child.returncode
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        summary = RestoreService(authority).reconcile()
        assert summary is None
        assert not lock_leaf.exists()
        assert not authority.poison_pending
    finally:
        try:
            authority.close()
        except BaseException:
            pass


def test_missing_artifact_is_refused_at_the_verification_boundary(tmp_path):
    """P12-RP-BKR-023 (subject S; subsystem backup verification, boundary:
    missing artifact file).

    Fault: ``verify_backup_package`` is pointed at a pathname that does not
    exist (deleted publication leaf).

    Witness: the product adapter boundary (``BackupZipPackageAdapter.verify``)
    refuses with ``BackupArchiveInvalid: ... No such file or directory``.
    """
    missing = tmp_path / "outside" / "deleted.botsbackup"
    missing.parent.mkdir(exist_ok=True)
    with pytest.raises(BackupArchiveInvalid, match="No such file or directory"):
        BackupZipPackageAdapter().verify(missing)


# ---------------------------------------------------------------------------
# Family 5: safe simulated resource failures — bounded failure, no
# authoritative partial mutation, cleanup attempted.  Subject S.
# ---------------------------------------------------------------------------


def test_staging_write_enospc_via_rlimit_fsize_is_bounded_and_cleaned(tmp_path):
    """P12-RP-BKR-024 (subject S; subsystem backup capture/package, boundary:
    staging write against a full device).

    Fault: the child pins ``RLIMIT_FSIZE`` to database_size + 1024 bytes
    (deterministic; hard limit mirrored from the inherited limit) and ignores
    SIGXFSZ so the write fails with ``EFBIG`` (errno 27) instead of dying by
    signal.  The failure lands inside the packaged-database entry copy of
    ``write_backup_package`` (the database is entries[0]).

    Witness (child-classified, exit code 3): ``BackupArchiveInvalid`` whose
    ``__cause__`` is ``OSError`` with errno 27, zero package fault points seen,
    staging leaf cleaned, destination absent.

    Durable postcondition: the capture's own snapshot cleanup ran
    (``database/temp`` empty), and a fresh authority serves marker 1024 and
    publishes a fresh internal recovery point — bounded failure, no
    authoritative partial mutation.
    """
    root = _make_root(tmp_path, "enospc", TARGET_MARKER)
    db_size = (root / "database" / "state.sqlite3").stat().st_size
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "probe.botsbackup"
    done = _run_p12_child(
        _P12_RLIMIT_CHILD, root, dest,
        env_extra={"P12_FSIZE_LIMIT": str(db_size + 1024)},
    )
    assert done.returncode == 3, (done.returncode, done.stdout, done.stderr[-2000:])
    lines = dict(
        line.split(" ", 1) for line in done.stdout.strip().splitlines()
    )
    failed = json.loads(lines["FAILED_AT"])
    assert failed["type"] == "BackupArchiveInvalid"
    assert "File too large" in failed["message"]
    assert failed["cause_type"] == "OSError"
    assert failed["cause_errno"] == errno.EFBIG
    assert failed["package_points_seen"] == []
    assert json.loads(lines["STAGING_LEFT"]) == []
    assert lines["DEST_EXISTS"] == "False"
    assert _temp_leaves(root) == []
    marker, published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER
    assert published_leaf.startswith("bots5-backup-")


def test_read_only_destination_directory_refuses_and_cleans(tmp_path):
    """P12-RP-BKR-025 (subject S; subsystem backup capture/package, boundary:
    staging leaf creation on a read-only destination directory).

    Fault: the destination directory is chmod 0o500 (r-x) before an in-process
    backup to ``<ro-dir>/probe.botsbackup``.  Requires euid != 0 (asserted
    loudly — the simulation is meaningless for root).

    Witness: ``BackupArchiveInvalid: backup staging leaf cannot be created
    safely`` raised at the staging-leaf creation boundary.

    Durable postcondition: destination absent, no staging leaf, the capture's
    snapshot cleanup ran (``database/temp`` empty), and a fresh authority
    serves marker 1024 and publishes a fresh internal recovery point.
    """
    assert os.geteuid() != 0, "read-only destination simulation requires euid != 0"
    root = _make_root(tmp_path, "read-only", TARGET_MARKER)
    ro_dir = tmp_path / "ro-destination"
    ro_dir.mkdir(mode=0o700)
    dest = ro_dir / "probe.botsbackup"
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        store = authority.open_store()
        try:
            paths = resolve_app_paths(root)
            paths.ensure_non_authoritative()
            service = _p12_backup_service(authority, store, paths)
            os.chmod(ro_dir, 0o500)
            try:
                with pytest.raises(
                    BackupArchiveInvalid,
                    match="backup staging leaf cannot be created safely",
                ):
                    service.create_backup(dest)
            finally:
                os.chmod(ro_dir, 0o700)
        finally:
            store.close()
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert not dest.exists()
    assert list(ro_dir.glob(".bots5-backup-*.staging")) == []
    assert _temp_leaves(root) == []
    marker, published_leaf = _cold_usability(root)
    assert marker == TARGET_MARKER
    assert published_leaf.startswith("bots5-backup-")


def test_destination_inside_recovery_root_refuses_before_any_staging(tmp_path):
    """P12-RP-BKR-026 (subject S; subsystem backup capture, boundary:
    destination inside a forbidden (source recovery) root).

    Fault: backup destinations inside the authority-owned recovery namespace
    (``<data_root>/recovery/inside.botsbackup``) and directly inside the data
    root (``<data_root>/inside.botsbackup``).

    Witness: ``BackupError: backup destination is inside a source recovery
    root`` raised pre-fence by
    ``RootedBackupCaptureAdapter._reject_destination_inside_recovery_roots`` —
    before any staging leaf or snapshot exists.

    Durable postcondition: no destination file, the ``recovery`` namespace
    stays empty, and a fresh authority serves marker 1024.
    """
    root = _make_root(tmp_path, "forbidden-root", TARGET_MARKER)
    authority = DataRootAuthority(root.absolute()).acquire()
    try:
        store = authority.open_store()
        try:
            paths = resolve_app_paths(root)
            paths.ensure_non_authoritative()
            service = _p12_backup_service(authority, store, paths)
            for destination in (
                paths.data_root / "recovery" / "inside.botsbackup",
                paths.data_root / "inside.botsbackup",
            ):
                with pytest.raises(
                    BackupError,
                    match="backup destination is inside a source recovery root",
                ):
                    service.create_backup(destination)
                assert not destination.exists()
            recovery = root / "recovery"
            assert list(recovery.iterdir()) == []
        finally:
            store.close()
    finally:
        try:
            authority.close()
        except BaseException:
            pass
    assert _open_store_marker(root) == TARGET_MARKER
