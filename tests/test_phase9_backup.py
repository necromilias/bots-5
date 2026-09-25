from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import stat
import sqlite3
import threading
import zipfile
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bots5.core.backup import (
    BackupProgress,
    BackupProgressState,
    BackupService,
)
from bots5.core.application import BotsApplication
from bots5.core.errors import (
    BackupDestinationExists,
    BackupArchiveInvalid,
    BackupDestinationInvalid,
    BackupError,
    BackupResolutionCancelled,
    BackupResourceLimit,
    StateError,
    BackupUnclassifiedState,
    BackupUnsupported,
    BackupUncertainPublication,
)
from bots5.domain.backup import (
    BACKUP_FORMAT,
    BackupClassification,
    BackupEntryRole,
    BackupReceipt,
    canonical_backup_json,
    default_backup_scope,
    LOGICAL_ROLES,
    logical_content_digest_from_rows,
    validate_entry_path,
)
from bots5.domain.ids import Uuid7Factory
from bots5.domain.provider import GenerationSettings
from bots5.domain.models import WorkspaceWindowState
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.authority_lock import AuthorityLock
from bots5.infrastructure.backup_capture import RootedBackupCaptureAdapter
from bots5.infrastructure.backup_package import (
    BackupFilePublicationAdapter,
    BackupZipPackageAdapter,
    _validate_zip_names,
    verify_backup_package,
)
from bots5.infrastructure.data_root_authority import _FIXED_DESCENDANTS
from bots5.infrastructure.persistence.archive_import_store import (
    JournalIntent,
    begin_staging,
    claim,
    fail_known_pregraph,
    queue_item,
)
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase6_blob_transition,
    clear_phase6,
    require_phase6_consumed,
)
from tests.test_phase9_archive_v2 import missing_external_archive


@pytest.fixture
def authority_store(tmp_path):
    root = tmp_path / "data"
    authority = AuthorityLock(root).acquire()
    paths = resolve_app_paths(root)
    paths.ensure_non_authoritative()
    (tmp_path / "outside").mkdir()
    (tmp_path / "backup").mkdir()
    store = authority.open_store()
    try:
        yield authority, store, paths, tmp_path
    finally:
        store.close()
        authority.release()


def _service(authority, store, paths, root: Path) -> BackupService:
    capture = RootedBackupCaptureAdapter(
        authority,
        store,
        paths,
        BackupZipPackageAdapter(),
        data_root_is_override=True,
    )
    return BackupService(
        capture,
        BackupZipPackageAdapter(),
        BackupFilePublicationAdapter(),
        Uuid7Factory(),
    )


def _prehead_recovery_point(tmp_path: Path) -> tuple[object, dict[str, object]]:
    """Build a supported 0008 source and capture its real recovery point."""
    from tests._authority_test_support import upgrade_to

    revision = "0008_catalogue_refresh_outcomes"
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    seed = tmp_path / "seed.sqlite3"
    upgrade_to(seed, revision)
    initializer = AuthorityLock(root).acquire()
    initializer.close()
    os.replace(seed, root / "database" / "state.sqlite3")
    os.chmod(root / "database" / "state.sqlite3", 0o600)
    authority = AuthorityLock(root).acquire()
    try:
        authority._claim_database()
        from bots5.infrastructure.backup_capture import create_migration_recovery_point

        whole = create_migration_recovery_point(
            authority, Uuid7Factory().new()
        )
        receipt = whole["verification_receipt"]
        assert receipt["source_db_migration_revision"] == revision
    except BaseException:
        authority.close()
        raise
    return authority, whole


def _rewrite_backup(
    source: Path,
    destination: Path,
    transform_manifest,
    *,
    extra_files: dict[str, bytes] | None = None,
    transform_files=None,
) -> None:
    with zipfile.ZipFile(source, "r") as package:
        files = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(files.pop("manifest.json"))
    if extra_files:
        files.update(extra_files)
    if transform_files is not None:
        files = transform_files(files)
    if transform_manifest is not None:
        manifest = transform_manifest(manifest)
    inventory = manifest["entry_inventory"]
    for row in inventory:
        path = str(row["path"])
        if path in files:
            data = files[path]
            row["uncompressed_size"] = len(data)
            row["sha256"] = hashlib.sha256(data).hexdigest()
    database = files.get("database/state.sqlite3")
    if database is not None:
        manifest["capture"]["source_database_size"] = len(database)
        manifest["capture"]["source_database_sha256"] = hashlib.sha256(
            database
        ).hexdigest()
    manifest["declared_total_uncompressed_bytes"] = sum(
        row["uncompressed_size"] for row in inventory
    )
    manifest["logical_content_digest"] = logical_content_digest_from_rows(inventory)
    manifest_bytes = canonical_backup_json(manifest)
    middle = sorted(path for path in files if path != "COMPLETED")
    with zipfile.ZipFile(destination, "w", allowZip64=True) as package:
        package.writestr("manifest.json", manifest_bytes)
        for path in middle:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            package.writestr(info, files[path])
        package.writestr("COMPLETED", b"")


def test_capture_and_independent_verification_round_trip(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    result = service.create_backup(root / "backup" / "complete.botsbackup")
    assert result.destination.exists()
    assert result.receipt.artifact_sha256
    verified = service.verify_backup(
        result.destination, expected_backup_id=result.backup_id
    )
    assert verified.receipt.artifact_size == result.receipt.artifact_size
    assert verified.receipt.artifact_sha256 == result.receipt.artifact_sha256
    assert verified.receipt.verification_id != result.receipt.verification_id
    assert verified.receipt.backup_logical_content_digest == (
        result.receipt.backup_logical_content_digest
    )
    assert result.manifest.entry_inventory[-1].path == "COMPLETED"
    assert result.manifest.entry_inventory[-1].uncompressed_size == 0


def test_zero_reference_ready_payload_is_included_and_deduplicated(authority_store):
    authority, store, paths, root = authority_store
    payload = root / "payload.bin"
    payload.write_bytes(b"zero-reference payload")
    first = store.ingest_attachment(payload)
    second = store.ingest_attachment(payload)
    assert first.id != second.id
    service = _service(authority, store, paths, root)
    result = service.create_backup(root / "outside" / "payload.botsbackup")
    digest = first.blob_digest
    path = f"payloads/sha256/{digest}"
    assert [item.path for item in result.manifest.entry_inventory].count(path) == 1
    assert result.manifest.capture.required_payload_count == 1
    assert result.manifest.capture.ready_blob_count == 1


def test_create_new_rejects_collision_and_explicit_overwrite_preserves_old(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    destination = root / "outside" / "backup.botsbackup"
    first = service.create_backup(destination)
    with pytest.raises(BackupDestinationExists):
        service.create_backup(destination)
    second = service.create_backup(destination, overwrite=True)
    assert second.backup_id != first.backup_id
    replaced = destination.parent / (
        f".bots5-backup-replaced-{second.backup_id}"
    )
    assert replaced.exists()
    assert replaced.read_bytes()[:4] == b"PK\x03\x04"


def test_service_cleans_exact_owned_staging_after_prepublication_fault(
    authority_store,
):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)

    def fault(point):
        if point == "before-rename":
            raise KeyboardInterrupt

    service._publisher = BackupFilePublicationAdapter(fault_hook=fault)
    with pytest.raises(KeyboardInterrupt):
        service.create_backup(root / "outside" / "faulted.botsbackup")
    destination = root / "outside" / "faulted.botsbackup"
    assert not destination.exists()
    assert not list(destination.parent.glob(".bots5-backup-*.staging"))


def test_rename_before_parent_fsync_is_uncertain_and_not_cleaned(
    authority_store,
):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)

    def fault(point):
        if point == "after-rename":
            raise KeyboardInterrupt

    service._publisher = BackupFilePublicationAdapter(fault_hook=fault)
    with pytest.raises(KeyboardInterrupt):
        service.create_backup(root / "outside" / "uncertain.botsbackup")
    assert (root / "outside" / "uncertain.botsbackup").exists()


def test_truncated_partial_package_cannot_verify(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    result = service.create_backup(root / "outside" / "partial.botsbackup")
    raw = result.destination.read_bytes()
    result.destination.write_bytes(raw[:-1])
    with pytest.raises(Exception, match="central directory"):
        verify_backup_package(result.destination)


def test_closed_paths_reject_traversal_and_unicode_collision():
    with pytest.raises(Exception):
        validate_entry_path("../database")
    with pytest.raises(Exception):
        _validate_zip_names(["config/a", "config/a\u0301"])


def test_capture_rejects_a_destination_inside_any_recovery_root(authority_store):
    authority, store, paths, root = authority_store
    capture = RootedBackupCaptureAdapter(
        authority, store, paths, BackupZipPackageAdapter(), data_root_is_override=True
    )
    with pytest.raises(BackupError):
        capture.capture_under_fence(
            progress=BackupProgress(BackupProgressState.ACQUIRING_FENCE, "op"),
            destination=paths.data_root / "inside.botsbackup",
            staging_path=root / "stage",
        )


def test_destination_preflight_rejects_low_space_before_staging(
    authority_store, monkeypatch
):
    authority, store, paths, root = authority_store
    destination_root = root / "outside"

    class Empty:
        f_bavail = 1
        f_frsize = 4096
        f_favail = 10

    monkeypatch.setattr(
        "bots5.infrastructure.backup_package.os.statvfs", lambda _: Empty()
    )
    service = _service(authority, store, paths, root)
    with pytest.raises(BackupResourceLimit):
        service.create_backup(destination_root / "no-space.botsbackup")
    assert not list(destination_root.glob(".bots5-backup-*.staging"))


def test_phase5_writer_waits_and_later_snapshot_sees_pre_cut_commit(authority_store):
    authority, store, paths, root = authority_store
    capture_started = threading.Event()
    release_capture = threading.Event()

    class CaptureFault:
        def __init__(self):
            self.fired = False

        def __call__(self, point):
            if point == "after-staging-file-fsync" and not self.fired:
                self.fired = True
                capture_started.set()
                assert release_capture.wait(timeout=5)
                raise BackupError("release controlled capture")

    package = BackupZipPackageAdapter(fault_hook=CaptureFault())
    capture = RootedBackupCaptureAdapter(
        authority, store, paths, package, data_root_is_override=True
    )
    service = BackupService(
        capture,
        BackupZipPackageAdapter(),
        BackupFilePublicationAdapter(),
        Uuid7Factory(),
    )
    writer_done = threading.Event()

    def mutate():
        store.set_application_generation_settings(
            GenerationSettings(max_output_tokens=1234)
        )
        writer_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(
            service.create_backup, root / "outside" / "fence.botsbackup"
        )
        assert capture_started.wait(timeout=5)
        writer = pool.submit(mutate)
        assert not writer_done.wait(timeout=0.1)
        release_capture.set()
        writer.result(timeout=5)
        assert writer_done.wait(timeout=5)
        with pytest.raises(BackupError, match="release controlled capture"):
            capture_future.result(timeout=5)

    result = service.create_backup(root / "outside" / "after-cut.botsbackup")
    with zipfile.ZipFile(result.destination, "r") as package:
        database = package.read("database/state.sqlite3")
    scratch = root / "outside" / "snapshot.sqlite3"
    scratch.write_bytes(database)
    with sqlite3.connect(scratch) as connection:
        assert connection.execute(
            "SELECT max_output_tokens FROM application_generation_config WHERE id=1"
        ).fetchall() == [(1234,)]


def test_capture_rejects_orphan_payload_object(authority_store):
    authority, store, paths, root = authority_store
    (Path(authority.root) / "attachments" / "objects" / ("0" * 64)).write_bytes(
        b"orphan"
    )
    service = _service(authority, store, paths, root)
    with pytest.raises(BackupUnclassifiedState, match="unattributable attachment payload"):
        service.create_backup(root / "outside" / "orphan.botsbackup")


def test_capture_rejects_foreign_data_root_file_and_namespace(authority_store):
    for case in ("foreign.file", "foreign-namespace"):
        authority, store, paths, root = authority_store
        path = Path(authority.root) / case
        if case.endswith("namespace"):
            path.mkdir()
        else:
            path.write_text("foreign")
        service = _service(authority, store, paths, root)
        with pytest.raises(BackupUnclassifiedState, match="data-root top level"):
            service.create_backup(root / "outside" / f"{case}.botsbackup")


def test_capture_observes_live_durability_facts(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    result = service.create_backup(root / "outside" / "facts.botsbackup")
    with authority.operation():
        with store._engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
            synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert result.manifest.capture.journal_mode == journal_mode
    assert result.manifest.capture.synchronous == synchronous
    assert result.manifest.capture.foreign_keys == bool(foreign_keys)


def test_capture_rejects_fifo_symlink_and_missing_payload_quickly(authority_store):
    authority, store, paths, root = authority_store
    payload = root / "blocked.bin"
    payload.write_bytes(b"payload")
    first = store.ingest_attachment(payload)
    object_path = Path(authority.root) / "attachments" / "objects" / first.blob_digest
    service = _service(authority, store, paths, root)

    object_path.unlink()
    os.mkfifo(object_path)
    with pytest.raises(BackupUnclassifiedState, match="unsafe identity"):
        service.create_backup(root / "outside" / "fifo.botsbackup")

    object_path.unlink()
    object_path.symlink_to(payload)
    with pytest.raises(BackupUnclassifiedState, match="unsafe identity"):
        service.create_backup(root / "outside" / "symlink.botsbackup")

    object_path.unlink()
    with pytest.raises(BackupUnclassifiedState, match="required source file is missing"):
        service.create_backup(root / "outside" / "missing.botsbackup")


def test_explicit_overwrite_creates_a_missing_destination(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    destination = root / "outside" / "missing-destination.botsbackup"
    result = service.create_backup(destination, overwrite=True)
    assert destination.is_file()
    assert result.destination == destination


def test_explicit_overwrite_directory_is_typed(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    destination = root / "outside" / "destination-directory.botsbackup"
    destination.mkdir()
    with pytest.raises(BackupDestinationInvalid, match="unsafe identity"):
        service.create_backup(destination, overwrite=True)


def test_core_verify_translates_malformed_archive_and_version(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    artifact = root / "outside" / "typed.botsbackup"
    result = service.create_backup(artifact)
    raw = artifact.read_bytes()
    artifact.write_bytes(raw[:-9])
    with pytest.raises(BackupArchiveInvalid, match="central directory"):
        service.verify_backup(artifact)

    artifact.write_bytes(raw)
    version_artifact = root / "outside" / "version2.botsbackup"
    _rewrite_backup(
        artifact,
        version_artifact,
        lambda manifest: manifest | {"backup_version": 2},
    )
    with pytest.raises(BackupUnsupported, match="backup version is unsupported"):
        service.verify_backup(version_artifact)


def test_core_verify_translates_missing_alembic_table(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    artifact = root / "outside" / "alembic-source.botsbackup"
    service.create_backup(artifact)
    rewritten = root / "outside" / "alembic-missing.botsbackup"

    def strip_revision(files):
        database = root / "outside" / "stripped.sqlite3"
        database.write_bytes(files["database/state.sqlite3"])
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE alembic_version")
        files["database/state.sqlite3"] = database.read_bytes()
        return files

    _rewrite_backup(artifact, rewritten, None, transform_files=strip_revision)
    with pytest.raises(BackupArchiveInvalid, match="independently read"):
        service.verify_backup(rewritten)


def test_core_verify_rejects_inventory_scope_contradiction(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    artifact = root / "outside" / "scope.botsbackup"
    service.create_backup(artifact)
    rewritten = root / "outside" / "scope-logs.botsbackup"

    def mutate(manifest):
        row = {
            "path": "logs/notes.txt",
            "media_type": "text/plain",
            "logical_role": "log",
            "required": True,
            "uncompressed_size": 5,
            "sha256": hashlib.sha256(b"notes").hexdigest(),
        }
        return manifest | {
            "entry_inventory": [
                *manifest["entry_inventory"][:-1],
                row,
                manifest["entry_inventory"][-1],
            ]
        }

    _rewrite_backup(
        artifact,
        rewritten,
        mutate,
        extra_files={"logs/notes.txt": b"notes"},
    )
    with pytest.raises(BackupArchiveInvalid, match="scope contradicts"):
        service.verify_backup(rewritten)


class _Cancellation:
    def __init__(self) -> None:
        self.cancel = False

    def __call__(self) -> bool:
        return self.cancel


def test_cancellation_before_capture_is_typed(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    destination = root / "outside" / "cancelled-before.botsbackup"
    cancellation = _Cancellation()
    cancellation.cancel = True
    with pytest.raises(BackupResolutionCancelled, match="before the recovery cut"):
        service.create_backup(destination, cancellation=cancellation)
    assert not destination.exists()
    assert not list(destination.parent.glob(".bots5-backup-*.staging"))


def test_cancellation_during_capture_is_typed(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    destination = root / "outside" / "cancelled-cut.botsbackup"
    cancellation = _Cancellation()
    observed = []
    def cancel():
        observed.append(True)
        return len(observed) == 2
    with pytest.raises(BackupResolutionCancelled, match="after the recovery cut"):
        service.create_backup(destination, cancellation=cancel)
    assert not destination.exists()
    assert not list(destination.parent.glob(".bots5-backup-*.staging"))


def test_cancellation_after_rename_is_deferred(authority_store, monkeypatch):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    destination = root / "outside" / "cancelled-publish.botsbackup"
    cancellation = _Cancellation()
    real_rename = BackupFilePublicationAdapter.__module__
    import bots5.infrastructure.backup_package as backup_package

    original = backup_package._rename_noreplace

    def rename(*args, **kwargs):
        result = original(*args, **kwargs)
        cancellation.cancel = True
        return result

    monkeypatch.setattr(backup_package, "_rename_noreplace", rename)
    result = service.create_backup(destination, cancellation=cancellation)
    assert result.destination == destination
    assert destination.is_file()


_RECEIPT_FIELDS = {
    "format", "receipt_version", "verification_id", "verified_at",
    "backup_id", "backup_logical_content_digest", "artifact_size",
    "artifact_sha256", "source_db_migration_revision",
    "verifier_application_version", "sqlite_runtime_version", "outcome",
    "passed_checks", "failed_check_ids", "reason_code",
}


def test_verification_receipt_is_exact_canonical_json():
    receipt = BackupReceipt(
        format=BACKUP_FORMAT,
        receipt_version=1,
        verification_id="018f0000-0000-7000-8000-000000000001",
        verified_at="2026-09-25T00:00:00.000000Z",
        backup_id="018f0000-0000-7000-8000-000000000002",
        backup_logical_content_digest="a" * 64,
        artifact_size=7,
        artifact_sha256="b" * 64,
        source_db_migration_revision="0012_phase9_archive_import",
        verifier_application_version="bots5-0.1.0",
        sqlite_runtime_version=sqlite3.sqlite_version,
        outcome="VALID",
        passed_checks=("artifact-sha256",),
        failed_check_ids=(),
        reason_code=None,
    )
    assert receipt.canonical_object().keys() == _RECEIPT_FIELDS
    assert receipt.canonical_bytes() == canonical_backup_json(receipt.canonical_object())


def test_receipt_sink_is_created_not_replaced(authority_store, tmp_path):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    artifact = root / "outside" / "receipted.botsbackup"
    sink = root / "outside" / "receipt.json"
    service.create_backup(artifact, receipt_sink=sink)
    first = json.loads(sink.read_text(encoding="utf-8"))
    assert set(first) == _RECEIPT_FIELDS
    assert first["artifact_sha256"] == service.verify_backup(artifact).receipt.artifact_sha256
    with pytest.raises(BackupDestinationExists):
        service.verify_backup(artifact, receipt_sink=sink)


def test_backup_topology_classification_and_pinned_sources_are_closed():
    assert {role.value for role in BackupEntryRole} == LOGICAL_ROLES
    assert {row.component for row in default_backup_scope(True)} == {
        "database", "attachment-payloads", "attachment-recovery-artifacts",
        "config", "state", "logs", "cache", "secrets",
        "campaign-repository-evidence", "unrelated-filesystem",
    }
    assert {row.component for row in default_backup_scope(True) if row.included} == {
        "database", "attachment-payloads", "attachment-recovery-artifacts"
    }
    assert _FIXED_DESCENDANTS == (
        "database", "attachments", "attachments/objects", "attachments/staging",
        "attachments/captures", "attachments/gc", "database/migration",
        "database/temp", "recovery",
    )
    repo = _REPO_ROOT
    assert hashlib.sha256((repo / "src/bots5/infrastructure/data_root_authority.py").read_bytes()).hexdigest() == "84f575ae9d4914e5a6a4740956d67564981d030ec1137cc701148413d2db1c01"
    assert hashlib.sha256((repo / "src/bots5/infrastructure/app_paths.py").read_bytes()).hexdigest() == "234a3597e3575fabcc3da6cb8d16a2365c902c5e37f94c8db012573d8dce8916"
    assert hashlib.sha256((repo / "src/bots5/infrastructure/persistence/migrations/versions/0012_phase9_archive_import.py").read_bytes()).hexdigest() == "f92493f0b9751e7760bcfcd4142a8fb152622f854bef090ab4fb5af968b3422b"
    with pytest.raises(Exception):
        validate_entry_path("../database")


def test_verify_rejects_malformed_zip_manifest_and_marker(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    valid = root / "outside" / "valid.botsbackup"
    service.create_backup(valid)

    malformed = root / "outside" / "malformed-zip.botsbackup"
    malformed.write_bytes(b"PK\x05\x06not-a-closed-backup")
    with pytest.raises(BackupArchiveInvalid):
        service.verify_backup(malformed)

    with zipfile.ZipFile(valid, "r") as package:
        files = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(files["manifest.json"])
    noncanonical = root / "outside" / "noncanonical-manifest.botsbackup"
    _write_repack(noncanonical, manifest, files, manifest_bytes=json.dumps(manifest, indent=2).encode())
    with pytest.raises(BackupArchiveInvalid, match="canonical"):
        service.verify_backup(noncanonical)

    marker = root / "outside" / "nonzero-marker.botsbackup"
    manifest["entry_inventory"][-1]["uncompressed_size"] = 1
    manifest["entry_inventory"][-1]["sha256"] = hashlib.sha256(b"x").hexdigest()
    manifest["declared_total_uncompressed_bytes"] = sum(
        row["uncompressed_size"] for row in manifest["entry_inventory"]
    )
    files["COMPLETED"] = b"x"
    manifest["logical_content_digest"] = logical_content_digest_from_rows(
        manifest["entry_inventory"]
    )
    _write_repack(marker, manifest, files)
    with pytest.raises(BackupArchiveInvalid):
        service.verify_backup(marker)


def _write_repack(
    destination: Path,
    manifest: dict[str, object],
    files: dict[str, bytes],
    *,
    manifest_bytes: bytes | None = None,
    method: int = zipfile.ZIP_STORED,
    flag_bits: int = 0,
) -> None:
    data = manifest_bytes or canonical_backup_json(manifest)
    with zipfile.ZipFile(destination, "w", allowZip64=True) as package:
        manifest_info = zipfile.ZipInfo("manifest.json", (1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_STORED
        manifest_info.flag_bits = flag_bits
        package.writestr(manifest_info, data)
        for name in sorted(
            path for path in files if path not in {"COMPLETED", "manifest.json"}
        ):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = method
            info.flag_bits = flag_bits
            package.writestr(info, files[name])
        marker = zipfile.ZipInfo("COMPLETED", (1980, 1, 1, 0, 0, 0))
        marker.compress_type = zipfile.ZIP_STORED
        marker.flag_bits = flag_bits
        package.writestr(marker, files["COMPLETED"])
    if flag_bits & ~0x800:
        # CPython's writer clears forbidden flag bits.  Patch the local header
        # and central directory directly so the closed verifier observes the
        # metadata it is required to reject.
        raw = bytearray(destination.read_bytes())
        with zipfile.ZipFile(destination, "r") as package:
            local_offset = package.getinfo("database/state.sqlite3").header_offset
        current_flags = int.from_bytes(raw[local_offset + 6:local_offset + 8], "little")
        raw[local_offset + 6:local_offset + 8] = (current_flags | flag_bits).to_bytes(2, "little")
        cursor = 0
        signature = b"PK\x01\x02"
        while True:
            cursor = raw.find(signature, cursor)
            if cursor < 0:
                break
            name_length = int.from_bytes(raw[cursor + 28:cursor + 30], "little")
            extra_length = int.from_bytes(raw[cursor + 30:cursor + 32], "little")
            comment_length = int.from_bytes(raw[cursor + 32:cursor + 34], "little")
            name = raw[cursor + 46:cursor + 46 + name_length].decode("utf-8")
            if name == "database/state.sqlite3":
                current_flags = int.from_bytes(raw[cursor + 8:cursor + 10], "little")
                raw[cursor + 8:cursor + 10] = (current_flags | flag_bits).to_bytes(2, "little")
            cursor += 46 + name_length + extra_length + comment_length
        destination.write_bytes(raw)


def test_verify_rejects_compression_encryption_data_descriptor_and_hardlink(
    authority_store,
):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    valid = root / "outside" / "metadata-source.botsbackup"
    service.create_backup(valid)
    with zipfile.ZipFile(valid, "r") as package:
        files = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(files["manifest.json"])
    cases = (
        ("compressed", {"method": zipfile.ZIP_DEFLATED}),
        ("encrypted", {"flag_bits": 1}),
        ("data-descriptor", {"flag_bits": 8}),
    )
    for label, kwargs in cases:
        rewritten = root / "outside" / f"{label}.botsbackup"
        _write_repack(rewritten, manifest, files, **kwargs)
        if "flag_bits" in kwargs:
            with zipfile.ZipFile(rewritten, "r") as observed:
                assert observed.getinfo("database/state.sqlite3").flag_bits & kwargs["flag_bits"]
        with pytest.raises(BackupArchiveInvalid):
            service.verify_backup(rewritten)
    linked = root / "outside" / "hard-linked.botsbackup"
    os.link(valid, linked)
    with pytest.raises(BackupArchiveInvalid, match="unsafe identity"):
        service.verify_backup(linked)


def test_verify_rejects_entry_count_resource_bound(authority_store, monkeypatch):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    valid = root / "outside" / "resource.botsbackup"
    service.create_backup(valid)
    monkeypatch.setattr(
        "bots5.infrastructure.backup_package.MAX_ENTRY_COUNT", 2
    )
    with pytest.raises(BackupResourceLimit):
        service.verify_backup(valid)


def _insert_ready_reservation(
    store, authority, payload: bytes, root: Path
) -> tuple[str, str, str]:
    digest = hashlib.sha256(payload).digest()
    operation_id = Uuid7Factory().new()
    payload_path = root / "outside" / f"reservation-{operation_id}.bin"
    payload_path.write_bytes(payload)
    attachment = store.ingest_attachment(payload_path)
    store.delete_attachment(attachment.id)
    source = (
        root / "outside" / f"reservation-{operation_id}.botsarchive"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"reservation archive")
    timestamp = "2026-09-25T00:00:00.000000Z"
    queued = store.enqueue_archive_import(
        source,
        resolver_roots=(source.parent,),
        now=datetime.now(UTC),
        queue_id=f"backup-reservation-{operation_id}",
    )
    with authority.transition():
        with store._engine.begin() as connection:
            claimed = claim(
                connection,
                queued.id,
                queued.revision,
                now=timestamp,
                owner_epoch=operation_id,
            )
            staged = begin_staging(
                connection,
                queued.id,
                claimed.revision,
                JournalIntent(
                    operation_id,
                    "backup-reservation",
                    1,
                    "0" * 64,
                    "source-chat",
                    0,
                    digest,
                    len(payload),
                    timestamp,
                    timestamp,
                    b"0" * 32,
                    (),
                    ({"digest": digest, "size": len(payload)},),
                ),
            )
    leaf = Path(authority.root) / "attachments" / "objects" / digest.hex()
    assert leaf.is_file()
    return digest.hex(), queued.id, operation_id


def _settle_ready_reservation(
    store, authority, queue_id: str, operation_id: str
) -> None:
    timestamp = "2026-09-25T00:00:01.000000Z"
    with authority.transition():
        with store._engine.begin() as connection:
            item = queue_item(connection, queue_id)
            fail_known_pregraph(
                connection,
                queue_id,
                item.revision,
                operation_id,
                code="RECOVERED_NO_GRAPH",
                now=timestamp,
            )


def test_capture_includes_ready_reservation_and_coherent_deleting_gc_state(
    authority_store,
):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    payload = b"ready reservation payload"
    digest, queue_id, operation_id = _insert_ready_reservation(
        store, authority, payload, root
    )
    first = service.create_backup(root / "backup" / "reservation.botsbackup")
    assert first.manifest.capture.required_payload_count == 1
    assert first.manifest.capture.ready_blob_count == 1
    assert f"payloads/sha256/{digest}" in [
        item.path for item in first.manifest.entry_inventory
    ]
    _settle_ready_reservation(store, authority, queue_id, operation_id)
    assert store.gc_attachments() == (digest,)
    second = service.create_backup(root / "backup" / "after-gc.botsbackup")
    assert second.manifest.capture.required_payload_count == 0
    assert second.manifest.capture.ready_blob_count == 0
    assert all(
        not item.path.startswith("payloads/")
        for item in second.manifest.entry_inventory
    )
    assert service.verify_backup(second.destination).receipt.outcome == "VALID"


def test_capture_records_missing_external_queue_and_continuation_truthfully(
    authority_store,
):
    authority, store, paths, root = authority_store
    intake = root / "intake"
    intake.mkdir()
    source = intake / "missing-external.botsarchive"
    source.write_bytes(missing_external_archive())
    store.enqueue_archive_import(
        source,
        resolver_roots=(intake,),
        now=datetime.now(UTC),
        queue_id="backup-missing-external",
    )
    store.execute_archive_import(
        "backup-missing-external",
        now=datetime.now(UTC),
        operation_id="backup-missing-external-operation",
    )
    service = _service(authority, store, paths, root)
    result = service.create_backup(root / "backup" / "missing-external.botsbackup")
    assert result.manifest.capture.required_payload_count == 0
    assert all(
        not item.path.startswith("payloads/")
        for item in result.manifest.entry_inventory
    )
    with zipfile.ZipFile(result.destination, "r") as package:
        database = package.read("database/state.sqlite3")
    snapshot = root / "backup" / "missing-external-snapshot.sqlite3"
    snapshot.write_bytes(database)
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute(
            "SELECT state FROM archive_import_queue WHERE id='backup-missing-external'"
        ).fetchone() == ("COMPLETED",)
        assert connection.execute(
            "SELECT count(*) FROM archive_import_attachment_refs WHERE "
            "availability='MISSING_EXTERNAL'"
        ).fetchone() == (1,)
        anchors = connection.execute(
            "SELECT base_key,base_message_id,resolution,resolution_evidence "
            "FROM archive_continuation_anchors ORDER BY base_key"
        ).fetchall()
        assert len(anchors) == 2
        head_message_id = connection.execute(
            "SELECT head_message_id FROM chats WHERE id="
            "(SELECT local_chat_id FROM archive_import_operations "
            "WHERE id='backup-missing-external-operation')"
        ).fetchone()[0]
        assert {row[0] for row in anchors} == {head_message_id, "empty"}
        assert all(row[2] == "UNRESOLVED" for row in anchors)
        assert all(json.loads(row[3])["kind"] == "imported-source" for row in anchors)
        assert all(row[1] is None for row in anchors if row[0] == "empty")
        assert all(row[1] is not None for row in anchors if row[0] == head_message_id)
    assert service.verify_backup(result.destination).receipt.outcome == "VALID"


def test_capture_allows_known_non_authoritative_top_level_children(
    authority_store,
):
    authority, store, paths, root = authority_store
    paths.ensure_non_authoritative()
    service = _service(authority, store, paths, root)
    result = service.create_backup(
        root / "outside" / "known-derived-siblings.botsbackup"
    )
    assert result.destination.is_file()


def test_capture_rejects_unknown_top_level_content(authority_store):
    authority, store, paths, root = authority_store
    (paths.data_root / "unknown-state").mkdir()
    service = _service(authority, store, paths, root)
    with pytest.raises(
        BackupUnclassifiedState, match="unknown-state"
    ):
        service.create_backup(root / "outside" / "unknown-sibling.botsbackup")


def test_capture_includes_attributable_staging_recovery_artifact(tmp_path):
    root = Path(tmp_path) / "authority-root"
    root.mkdir(mode=0o700)
    operation_id = Uuid7Factory().new()
    payload = b"staging recovery"
    digest = hashlib.sha256(payload).digest()
    authority = AuthorityLock(root).acquire()
    store = authority.open_store()
    try:
        with authority.operation():
            staging_fd = authority._directory_fd("attachments/staging")
            descriptor = os.open(
                operation_id,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=staging_fd,
            )
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with store._engine.begin() as connection:
                arm_phase6_blob_transition(
                    connection,
                    digest,
                    "",
                    "staging",
                    operation_id=operation_id,
                    stage_name=operation_id,
                    byte_size=len(payload),
                )
                try:
                    connection.exec_driver_sql(
                        "INSERT INTO attachment_blobs"
                        "(digest,byte_size,state,operation_id,stage_name,gc_id,created_at) "
                        "VALUES (?,?,?,?,?,NULL,?)",
                        (digest, len(payload), "staging", operation_id, operation_id,
                         "2026-09-25T00:00:00.000000Z"),
                    )
                    require_phase6_consumed(connection)
                finally:
                    clear_phase6(connection)
            from bots5.infrastructure.backup_capture import (
                create_migration_recovery_point,
            )

            whole = create_migration_recovery_point(authority, operation_id)
    finally:
        store.close()
        authority.release()
    artifact = Path(authority.root) / "recovery" / str(whole["published_leaf"])
    verify_backup_package(
        artifact,
        expected_backup_id=whole["verification_receipt"]["backup_id"],
    )
    with zipfile.ZipFile(artifact, "r") as package:
        manifest = json.loads(package.read("manifest.json"))
    assert any(
        str(item["path"]).startswith("recovery-artifacts/attachments/staging/")
        for item in manifest["entry_inventory"]
    )
    assert manifest["backup_id"] == whole["verification_receipt"]["backup_id"]


def test_capture_waits_for_attachment_and_import_writers_without_loss(authority_store):
    authority, store, paths, root = authority_store
    started = threading.Event()
    release = threading.Event()

    class CaptureFault:
        def __init__(self):
            self.fired = False

        def __call__(self, point):
            if point == "after-staging-file-fsync" and not self.fired:
                self.fired = True
                started.set()
                assert release.wait(timeout=5)
                raise BackupError("release controlled capture")

    package = BackupZipPackageAdapter(fault_hook=CaptureFault())
    capture = RootedBackupCaptureAdapter(
        authority, store, paths, package, data_root_is_override=True
    )
    service = BackupService(
        capture, BackupZipPackageAdapter(), BackupFilePublicationAdapter(), Uuid7Factory()
    )
    payload = root / "writer-payload.bin"
    payload.write_bytes(b"writer payload")
    source = root / "writer-source.botsarchive"
    source.write_bytes(b"source bytes")

    def attachment_writer():
        return store.ingest_attachment(payload)

    def import_writer():
        return store.enqueue_archive_import(
            source,
            resolver_roots=(root,),
            now=datetime.now(UTC),
            queue_id="backup-import-race",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        blocked = pool.submit(service.create_backup, root / "outside" / "race.botsbackup")
        assert started.wait(timeout=5)
        attachment = pool.submit(attachment_writer)
        queued = pool.submit(import_writer)
        assert not attachment.done() and not queued.done()
        release.set()
        with pytest.raises(BackupError, match="release controlled capture"):
            blocked.result(timeout=5)
        attachment.result(timeout=5)
        queued.result(timeout=5)

    result = service.create_backup(root / "outside" / "after-writers.botsbackup")
    with zipfile.ZipFile(result.destination, "r") as package:
        database = package.read("database/state.sqlite3")
    snapshot = root / "outside" / "race-snapshot.sqlite3"
    snapshot.write_bytes(database)
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute(
            "SELECT count(*) FROM attachment_blobs WHERE state='ready'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT id FROM archive_import_queue WHERE id='backup-import-race'"
        ).fetchone() == ("backup-import-race",)


def test_concurrent_attachment_publication_and_deletion_are_serialized(authority_store):
    authority, store, paths, root = authority_store
    first_payload = root / "first.bin"
    second_payload = root / "second.bin"
    first_payload.write_bytes(b"first")
    second_payload.write_bytes(b"second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.ingest_attachment, first_payload)
        second = pool.submit(store.ingest_attachment, second_payload)
        first_attachment = first.result(timeout=5)
        second_attachment = second.result(timeout=5)
    store.delete_attachment(second_attachment.id)
    service = _service(authority, store, paths, root)
    result = service.create_backup(root / "outside" / "concurrent.botsbackup")
    assert result.manifest.capture.ready_blob_count == 2
    assert result.manifest.capture.required_payload_count == 2
    assert f"payloads/sha256/{first_attachment.blob_digest}" in [
        item.path for item in result.manifest.entry_inventory
    ]


def test_migration_recovery_point_is_live_fresh_and_exact(authority_store):
    from bots5.infrastructure.backup_capture import create_migration_recovery_point

    authority, _store, _paths, root = authority_store
    transaction_id = Uuid7Factory().new()
    with _store.command_admission():
        first = create_migration_recovery_point(authority, transaction_id)
    receipt = first["verification_receipt"]
    assert set(receipt) == _RECEIPT_FIELDS
    assert receipt["backup_id"] != transaction_id
    assert first["published_leaf"] == f"bots5-backup-{receipt['backup_id']}.botsbackup"
    assert (Path(authority.root) / "recovery" / first["published_leaf"]).is_file()
    with _store.command_admission():
        second = create_migration_recovery_point(authority, Uuid7Factory().new())
    assert second["verification_receipt"]["verification_id"] != receipt["verification_id"]
    assert second["published_leaf"] != first["published_leaf"]


def test_prejournal_recovery_adoption_rejects_corrupt_package(
    tmp_path, monkeypatch
):
    from bots5.core.errors import MigrationRecoveryStateError
    from bots5.infrastructure.backup_package import BackupZipPackageAdapter
    from bots5.infrastructure.persistence.migration_runner import (
        _adopt_prejournal_recovery_point,
    )

    authority, whole = _prehead_recovery_point(tmp_path)
    leaf = str(whole["published_leaf"])
    artifact = Path(authority.root) / "recovery" / leaf
    original = artifact.read_bytes()
    artifact.write_bytes(original[:-1])
    verification_calls: list[Path] = []
    original_verify = BackupZipPackageAdapter.verify

    def counted_verify(self, path, *, expected_backup_id=None):
        verification_calls.append(Path(path))
        return original_verify(self, path, expected_backup_id=expected_backup_id)

    monkeypatch.setattr(BackupZipPackageAdapter, "verify", counted_verify)
    try:
        with pytest.raises(
            MigrationRecoveryStateError, match="not independently adoptable"
        ):
            _adopt_prejournal_recovery_point(
                authority, leaf, "0008_catalogue_refresh_outcomes"
            )
        assert verification_calls == [artifact]
        assert artifact.read_bytes() == original[:-1]
    finally:
        authority.close()


def test_prejournal_recovery_adoption_rejects_multiple_candidates(authority_store):
    from bots5.core.errors import MigrationRecoveryStateError
    from bots5.infrastructure.backup_capture import create_migration_recovery_point
    from bots5.infrastructure.persistence.migration_runner import (
        _initial_prejournal_recovery_leaf,
    )

    authority, _store, _paths, _root = authority_store
    with _store.command_admission():
        first = create_migration_recovery_point(authority, Uuid7Factory().new())
        second = create_migration_recovery_point(authority, Uuid7Factory().new())
    recovery = Path(authority.root) / "recovery"
    assert (recovery / first["published_leaf"]).is_file()
    assert (recovery / second["published_leaf"]).is_file()
    with pytest.raises(MigrationRecoveryStateError, match="ambiguous"):
        _initial_prejournal_recovery_leaf(authority)
    assert (recovery / first["published_leaf"]).is_file()
    assert (recovery / second["published_leaf"]).is_file()


def test_prejournal_recovery_adoption_rejects_revision_mismatch(
    authority_store, monkeypatch
):
    from bots5.core.errors import MigrationRecoveryStateError
    from bots5.infrastructure.backup_capture import create_migration_recovery_point
    from bots5.infrastructure.persistence import migration_runner

    authority, _store, _paths, _root = authority_store
    with _store.command_admission():
        whole = create_migration_recovery_point(authority, Uuid7Factory().new())
    leaf = str(whole["published_leaf"])
    monkeypatch.setattr(
        migration_runner,
        "_discover_existing",
        lambda _authority: "0008_catalogue_refresh_outcomes",
    )
    with pytest.raises(
        MigrationRecoveryStateError, match="source revision is incompatible"
    ):
        migration_runner._adopt_prejournal_recovery_point(
            authority, leaf, "0008_catalogue_refresh_outcomes"
        )
    assert (Path(authority.root) / "recovery" / leaf).is_file()


def test_prejournal_recovery_adoption_rejects_equivalence_mismatch(
    tmp_path, monkeypatch
):
    from bots5.core.errors import MigrationRecoveryStateError
    from bots5.infrastructure.persistence import migration_runner

    authority, whole = _prehead_recovery_point(tmp_path)
    leaf = str(whole["published_leaf"])
    artifact = Path(authority.root) / "recovery" / leaf
    comparisons: list[object] = []

    def mismatched_comparison(authority_arg):
        comparisons.append(authority_arg)
        return "0" * 64

    monkeypatch.setattr(
        migration_runner,
        "_comparison_snapshot_sha256",
        mismatched_comparison,
    )
    try:
        with pytest.raises(
            MigrationRecoveryStateError,
            match="does not represent current source state",
        ):
            migration_runner._adopt_prejournal_recovery_point(
                authority, leaf, "0008_catalogue_refresh_outcomes"
            )
        assert len(comparisons) == 1
        assert comparisons[0] is authority
        assert artifact.is_file()
    finally:
        authority.close()


def test_verifier_is_source_independent_when_live_root_is_missing(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    artifact = root / "outside" / "independent.botsbackup"
    result = service.create_backup(artifact)
    authority.release()
    Path(authority.root).rename(root / "data-moved")
    verified = service.verify_backup(artifact, expected_backup_id=result.backup_id)
    assert verified.receipt.artifact_sha256 == result.receipt.artifact_sha256


@pytest.mark.parametrize(
    "point",
    ["after-entry:database/state.sqlite3", "after-completion-marker", "after-staging-file-fsync"],
)
def test_staging_fault_sweep_cleans_exact_owned_leaf(authority_store, point):
    authority, store, paths, root = authority_store
    destination = root / "outside" / "staging-fault.botsbackup"

    def fault(observed_point):
        if observed_point == point:
            raise BackupError("staging fault")

    package = BackupZipPackageAdapter(fault_hook=fault)
    capture = RootedBackupCaptureAdapter(
        authority, store, paths, package, data_root_is_override=True
    )
    service = BackupService(
        capture, BackupZipPackageAdapter(), BackupFilePublicationAdapter(), Uuid7Factory()
    )
    with pytest.raises(BackupError, match="staging fault"):
        service.create_backup(destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".bots5-backup-*.staging"))


@pytest.mark.parametrize("point", ["before-parent-fsync", "after-parent-fsync"])
def test_publication_fsync_fault_is_not_success(authority_store, point):
    authority, store, paths, root = authority_store
    destination = root / "outside" / "publication-fault.botsbackup"

    def fault(observed_point):
        if observed_point == point:
            raise OSError("publication fsync fault")

    capture = RootedBackupCaptureAdapter(
        authority, store, paths, BackupZipPackageAdapter(), data_root_is_override=True
    )
    service = BackupService(
        capture,
        BackupZipPackageAdapter(),
        BackupFilePublicationAdapter(fault_hook=fault),
        Uuid7Factory(),
    )
    with pytest.raises(BackupUncertainPublication, match="fsync is uncertain"):
        service.create_backup(destination)
    assert destination.exists()
    assert not list(destination.parent.glob(".bots5-backup-*.staging"))


def test_partial_artifact_sweep_cannot_verify(authority_store):
    authority, store, paths, root = authority_store
    service = _service(authority, store, paths, root)
    valid = service.create_backup(root / "outside" / "sweep-source.botsbackup")
    raw = valid.destination.read_bytes()
    partials = (raw[:-1], raw + b"\0", b"PK\x03\x04" + raw[4:-1])
    for index, partial in enumerate(partials):
        artifact = root / "outside" / f"partial-{index}.botsbackup"
        artifact.write_bytes(partial)
        with pytest.raises(BackupArchiveInvalid):
            service.verify_backup(artifact)


def test_application_backup_runs_on_worker_and_exposes_progress(monkeypatch):
    class Store:
        def command_admission(self, independent=False):
            return __import__("contextlib").nullcontext()

        @__import__("contextlib").contextmanager
        def event_admission(self):
            yield

        @__import__("contextlib").contextmanager
        def issued_event_effect(self):
            yield

        def assert_admitting(self):
            return None

        def reconcile_interrupted_generations(self, now):
            return None

    class Events:
        def bind_effect_authority(self, admission, effect):
            self.admission = admission
            self.effect = effect

    class Service:
        def __init__(self):
            self.calls = []

        def create_backup(self, destination, *, overwrite=False, cancellation=None, receipt_sink=None, progress_callback=None):
            self.calls.append((threading.get_ident(), progress_callback))
            progress_callback(BackupProgress(BackupProgressState.ACQUIRING_FENCE, "op"))
            progress_callback(BackupProgress(BackupProgressState.HOLDING_RECOVERY_POINT_FENCE, "op"))
            return "result"

    service = Service()
    observed = []
    application = BotsApplication(
        Store(), Events(), object(), backup_service=service
    )
    import bots5.core.application as application_module

    executor = ThreadPoolExecutor(max_workers=1)

    async def off_event_loop(function, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            executor, lambda: function(*args, **kwargs)
        )

    monkeypatch.setattr(application_module.asyncio, "to_thread", off_event_loop)
    try:
        result = asyncio.run(application.create_backup(
            "/tmp/unused.botsbackup",
            progress_callback=observed.append,
        ))
    finally:
        executor.shutdown(wait=True)
    assert result == "result"
    assert service.calls[0][0] != threading.get_ident()
    assert [progress.state.value for progress in observed] == [
        "acquiring-fence", "holding-recovery-point-fence"
    ]



def test_package_port_is_used_and_production_fault_hook_is_absent():
    package_source = (_REPO_ROOT / "src/bots5/infrastructure/backup_package.py").read_text()
    capture_source = (_REPO_ROOT / "src/bots5/infrastructure/backup_capture.py").read_text()
    assert "_TEST_FAULT_HOOK" not in package_source
    assert "self._package.write(" in capture_source
    assert "write_backup_package(" not in capture_source


def test_nontransition_workspace_writer_waits_and_is_not_lost(authority_store):
    authority, store, paths, root = authority_store
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def mutate():
        state = WorkspaceWindowState(
            "window",
            1,
            (0, 0, 10, 10),
            None,
            False,
            True,
            datetime.now(UTC),
            True,
            None,
            None,
        )
        started.set()
        release.wait(timeout=5)
        store.save_workspace_window(state)
        finished.set()

    with authority.operation():
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(mutate)
            assert started.wait(timeout=5)
            with store.mutation_transition():
                assert not finished.wait(timeout=0.1)
                release.set()
            future.result(timeout=5)
            assert finished.wait(timeout=5)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SQLITE_SOURCE = _REPO_ROOT / "src/bots5/infrastructure/persistence/sqlite.py"
_ARCHIVE_STORE_SOURCE = (
    _REPO_ROOT / "src/bots5/infrastructure/persistence/archive_import_store.py"
)
_SEARCH_SOURCE = _REPO_ROOT / "src/bots5/infrastructure/persistence/search.py"
_ATTACHMENTS_SOURCE = _REPO_ROOT / "src/bots5/infrastructure/attachments.py"
_PHASE5_STORE_SOURCE = _REPO_ROOT / "src/bots5/infrastructure/persistence/phase5_store.py"
_DURABLE_SQLITE_SOURCES = (
    _SQLITE_SOURCE,
    _PHASE5_STORE_SOURCE,
    _ARCHIVE_STORE_SOURCE,
    _SEARCH_SOURCE,
)

_IMPORT_ALIASES = {
    "archive_queue_item": "queue_item",
    "enqueue_archive_import": "enqueue",
    "reorder_queue": "reorder",
}

# Bare calls to these exact, singly imported symbols are resolved to their
# defining module and function.  No ambiguous same-name lookup is permitted.
_IMPORT_TARGETS = {
    name: f"{_ARCHIVE_STORE_SOURCE}::{name}"
    for name in (
        "advance_queue_control",
        "enqueue",
        "_persist_transition",
        "begin_staging",
        "advance_journal",
        "fail_known_pregraph",
        "begin_graph_commit",
        "complete_known_graph",
    )
}

# Startup recovery runs while the authority owns exclusive admission and before
# the store is published ready.  Ordinary commands and backup capture cannot yet
# race it.  The last helper is one caller of attachment healing on that startup
# path; the same healing helper is otherwise reached through fenced ingestion/GC.
_STARTUP_AUTHORITY_EXEMPTIONS = {
    f"{_SQLITE_SOURCE}::SQLiteAppStateStore._recover_attachment_rows",
    f"{_SQLITE_SOURCE}::SQLiteAppStateStore._recover_deleting_blob",
    f"{_SQLITE_SOURCE}::SQLiteAppStateStore._heal_ready_imported_attachment_refs_startup",
}


def _ast_call_name(call: ast.Call) -> str:
    def expression(node: ast.AST) -> str:
        if isinstance(node, ast.Attribute):
            return f"{expression(node.value)}.{node.attr}"
        if isinstance(node, ast.Name):
            return node.id
        return "<unknown>"

    return expression(call.func).rpartition(".")[2]


def _ast_dml_site(call: ast.Call, source: str) -> bool:
    if _ast_call_name(call) not in {"execute", "executemany", "exec_driver_sql"}:
        return False
    text = " ".join((ast.get_source_segment(source, call) or "").split()).upper()
    textual_dml = any(
        marker in text
        for marker in ("INSERT INTO", "UPDATE ", "DELETE FROM", "REPLACE INTO")
    )
    orm_dml = any(
        isinstance(node, ast.Call)
        and _ast_call_name(node) in {"insert", "update", "delete"}
        for node in ast.walk(call)
    )
    return textual_dml or orm_dml


def _ast_attachment_filesystem_site(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr in {
        "write_bytes",
        "write_text",
        "unlink",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "rmtree",
        "copy",
        "copyfile",
        "copytree",
        "move",
    }


def _ast_fences(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    fences: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            values = [item.context_expr]
            if isinstance(item.context_expr, (ast.Tuple, ast.List)):
                values.extend(item.context_expr.elts)
            for value in values:
                if isinstance(value, ast.Call) and _ast_call_name(value) in {
                    "transition",
                    "mutation_transition",
                }:
                    fences.add(_ast_call_name(value))
    return fences


class _StaticFunction:
    def __init__(self, path: Path, cls: str | None, node):
        self.path = path
        self.cls = cls
        self.node = node
        self.name = node.name
        owner = f"{cls}." if cls else ""
        self.key = f"{path}::{owner}{node.name}"
        self.fences = _ast_fences(node)
        self.sites: list[int] = []
        self.callees: set[str] = set()
        self.static_edges: set[str] = set()
        source = path.read_text()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if path in _DURABLE_SQLITE_SOURCES and _ast_dml_site(call, source):
                self.sites.append(call.lineno)
            if path == _ATTACHMENTS_SOURCE and _ast_attachment_filesystem_site(call):
                self.sites.append(call.lineno)
            self.callees.add(_IMPORT_ALIASES.get(_ast_call_name(call), _ast_call_name(call)))


def _call_target_key(call: ast.Call, path: Path, cls: str | None) -> str | None:
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
        and cls is not None
    ):
        return f"{path}::{cls}.{func.attr}"
    if isinstance(func, ast.Name):
        if func.id in _IMPORT_TARGETS:
            return _IMPORT_TARGETS[func.id]
        return f"{path}::{func.id}"
    return None


def _static_edges(function: _StaticFunction) -> set[str]:
    return {
        target
        for call in ast.walk(function.node)
        if isinstance(call, ast.Call)
        and (target := _call_target_key(call, function.path, function.cls)) is not None
    }


def _static_durable_functions() -> list[_StaticFunction]:
    functions: list[_StaticFunction] = []
    for path in (*_DURABLE_SQLITE_SOURCES, _ATTACHMENTS_SOURCE):
        tree = ast.parse(path.read_text())
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef):
                children = statement.body
                class_name: str | None = statement.name
            else:
                children = (statement,)
                class_name = None
            for child in children:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(_StaticFunction(path, class_name, child))
    return functions


def _static_reaches_fence(
    function: _StaticFunction,
    functions_by_key: dict[str, _StaticFunction],
    callers_by_key: dict[str, set[str]],
    *,
    seen: frozenset[str] | None = None,
) -> bool:
    seen = seen or frozenset()
    if function.key in seen:
        return False
    path = seen | {function.key}
    if function.fences or function.key in _STARTUP_AUTHORITY_EXEMPTIONS:
        return True
    # Proof edges are source-file, declaring-class, and method-name exact.
    # There is deliberately no bare-name caller map and no inverse fallback
    # that treats an unrelated fenced callee as proof for this writer.
    return all(
        _static_reaches_fence(
            functions_by_key[caller], functions_by_key, callers_by_key, seen=path
        )
        for caller in callers_by_key.get(function.key, set())
    )


def test_mutation_gate_then_authority_transition_does_not_deadlock(authority_store):
    import concurrent.futures
    from contextlib import ExitStack

    authority, store, _paths, _root = authority_store
    acquired_mutation = threading.Event()
    acquired_transition = threading.Event()

    def same_global_order() -> None:
        with ExitStack() as stack:
            stack.enter_context(authority.operation())
            stack.enter_context(store.mutation_transition())
            acquired_mutation.set()
            assert acquired_mutation.wait(timeout=5)
            stack.enter_context(authority.transition())
            acquired_transition.set()
            # Leave a little time for an opposite-order thread to invert.
            threading.Event().wait(0.05)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(same_global_order)
        second = pool.submit(same_global_order)
        first.result(timeout=5)
        second.result(timeout=5)
    assert acquired_transition.is_set()


def test_static_proof_enumerates_every_durable_write_site():
    functions = _static_durable_functions()
    functions_by_key: dict[str, _StaticFunction] = {}
    for function in functions:
        function.static_edges = _static_edges(function)
        functions_by_key[function.key] = function
    callers_by_key: dict[str, set[str]] = {
        key: set() for key in functions_by_key
    }
    for function in functions:
        for target in function.static_edges:
            if target in callers_by_key:
                callers_by_key[target].add(function.key)

    durable = [function for function in functions if function.sites]
    required = {
        "update_streaming_message",
        "finalize_generation",
        "update_attempt",
        "save_workspace_window",
        "delete_workspace_window",
        "prepare_imported_user_edit_continuation",
        "ingest_attachment",
        "delete_attachment",
        "gc_attachments",
        "create_provider_connection",
        "update_provider_connection",
        "retire_provider_connection",
        "add_manual_model",
        "refresh_model_catalogue",
        "set_capability_fact",
        "set_capability_override",
        "add_capability_observation",
        "set_application_generation_settings",
        "set_application_default_model",
        "_set_generation_config",
        "set_chat_model_selection",
    }
    assert required.issubset({function.name for function in durable})
    assert any(function.path == _ATTACHMENTS_SOURCE for function in durable)

    for function in durable:
        assert function.sites
        assert _static_reaches_fence(function, functions_by_key, callers_by_key), (
            f"{function.key} durable write sites {function.sites} do not reach an "
            "authority transition, mutation gate, or listed startup exemption"
        )


def test_mutation_gate_is_never_acquired_inside_authority_transition():
    sources = (
        _SQLITE_SOURCE,
        _PHASE5_STORE_SOURCE,
        _ARCHIVE_STORE_SOURCE,
        _SEARCH_SOURCE,
        _ATTACHMENTS_SOURCE,
        _REPO_ROOT / "src/bots5/infrastructure/backup_capture.py",
    )
    local_gates = {"mutation_gate"}

    def gate_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id in local_gates:
            return "mutation_transition"
        if (
            isinstance(node, ast.Call)
            and _ast_call_name(node) in {"transition", "mutation_transition"}
        ):
            return _ast_call_name(node)
        return None

    def visit(node: ast.AST, active: tuple[str, ...] = ()) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            entered: list[str] = []
            for item in node.items:
                values = [item.context_expr]
                if isinstance(item.context_expr, (ast.Tuple, ast.List)):
                    values = list(item.context_expr.elts)
                names = [gate_name(value) for value in values]
                for index, name in enumerate(names):
                    if name is None:
                        continue
                    assert not (name == "mutation_transition" and "transition" in active), (
                        f"mutation gate acquired inside authority transition at line {node.lineno}"
                    )
                    assert not (
                        name == "transition"
                        and "mutation_transition" in active + tuple(entered[:index])
                    ), (
                        f"authority transition acquired before mutation gate at line {node.lineno}"
                    )
                    entered.append(name)
            for child in node.body:
                visit(child, active + tuple(entered))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, active)

    for source in sources:
        visit(ast.parse(source.read_text()))
