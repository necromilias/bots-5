"""Strict Backup v1 stored-ZIP writer and artifact-only verifier."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import struct
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Callable, Mapping

from bots5.domain.backup import (
    BACKUP_VERSION,
    BACKUP_FORMAT,
    BackupCaptureFacts,
    BackupManifest,
    BackupManifestEntry,
    BackupManifestError,
    BackupManifestUnsupported,
    BackupReceipt,
    EMPTY_SHA256,
    MAX_ENTRY_BYTES,
    MAX_ENTRY_COUNT,
    MAX_MANIFEST_BYTES,
    MAX_PATH_BYTES,
    MAX_PATH_DEPTH,
    MAX_SCRATCH_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    classify_entry_path,
    backup_manifest_object,
    canonical_backup_json,
    canonical_utc_timestamp,
    entry_media_type,
    logical_content_digest,
    reject_path_collisions,
    validate_entry_path,
    validate_manifest_object,
    validate_sha256,
)
from bots5.domain.ids import Uuid7Factory
from bots5.domain.ids import uuid7
from bots5.core.errors import (
    BackupArchiveInvalid,
    BackupDestinationExists,
    BackupDestinationInvalid,
    BackupResourceLimit,
    BackupPublicationFailed,
    BackupResolutionCancelled,
    BackupUncertainPublication,
    BackupUnsupported,
)
from bots5.infrastructure.attachments import _rename_exchange, _rename_noreplace
from bots5.infrastructure.persistence.phase9_schema import validate_phase9_schema


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EOCD = struct.Struct("<IHHHHIIH")
_EOCD64 = struct.Struct("<IQII")
_EOCD64_LOCATOR = struct.Struct("<IIQI")
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")
_LOCAL = struct.Struct("<IHHHHHIIIHH")
_CHUNK = 1024 * 1024
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_ATTACHMENT_CAPABILITY_REVISION = "0009_phase6_context_attachments"


def _fault(point: str, hook: Callable[[str], object] | None = None) -> None:
    if hook is not None:
        hook(point)


class BackupPackageError(ValueError):
    """A Backup v1 package is malformed."""


class BackupPackageUnsupported(BackupPackageError):
    """A Backup v1 package uses a version this build cannot verify."""


class BackupPackageResourceLimit(BackupPackageError):
    """A Backup v1 package reached a bounded resource limit."""


@dataclass(frozen=True, slots=True)
class BackupSourceEntry:
    path: str
    size: int
    sha256: str
    reader: Callable[[], BinaryIO] | None = None
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class BackupCaptureSource:
    backup_id: str
    created_at: str
    source_application_version: str
    source_db_migration_revision: str
    facts: BackupCaptureFacts
    entries: tuple[BackupSourceEntry, ...]


@dataclass(frozen=True, slots=True)
class StagedBackup:
    staging_path: Path
    manifest: BackupManifest
    manifest_bytes: bytes
    artifact_size: int
    artifact_sha256: str


def _sha256_reader(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(_CHUNK):
        digest.update(block)
        size += len(block)
        if size > MAX_ENTRY_BYTES:
            raise BackupPackageResourceLimit("backup entry exceeds bounded size")
    return size, digest.hexdigest()


def _manifest(
    source: BackupCaptureSource,
    *,
    recovery_artifacts_included: bool,
) -> tuple[BackupManifest, bytes]:
    entries = list(source.entries)
    paths = [item.path for item in entries]
    validate_entry_path("database/state.sqlite3")
    reject_path_collisions(paths)
    if any(path == "COMPLETED" for path in paths):
        raise BackupPackageError("capture source may not provide a completion marker")
    if not entries or entries[0].path != "database/state.sqlite3":
        raise BackupPackageError("capture source must lead with its SQLite snapshot")
    inventory = [
        BackupManifestEntry(
            item.path,
            entry_media_type(item.path, classify_entry_path(item.path)),
            classify_entry_path(item.path),
            True,
            item.size,
            item.sha256,
        )
        for item in entries
    ]
    inventory.append(
        BackupManifestEntry(
            "COMPLETED", "application/octet-stream", "completion", True, 0, EMPTY_SHA256
        )
    )
    total = sum(item.uncompressed_size for item in inventory)
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES or len(inventory) > MAX_ENTRY_COUNT:
        raise BackupPackageResourceLimit("backup capture exceeds bounded limits")
    from bots5.domain.backup import default_backup_scope

    manifest = BackupManifest(
        source.backup_id,
        source.created_at,
        source.source_application_version,
        source.source_db_migration_revision,
        source.facts,
        default_backup_scope(recovery_artifacts_included),
        tuple(inventory),
    )
    return manifest, canonical_backup_json(backup_manifest_object(manifest))


def _zip_info(path: str) -> zipfile.ZipInfo:
    result = zipfile.ZipInfo(path, date_time=_FIXED_TIME)
    result.compress_type = zipfile.ZIP_STORED
    result.create_system = 3
    result.external_attr = (stat.S_IFREG | 0o600) << 16
    return result


def _open_staging_leaf(path: Path) -> int:
    if path.parent == path or path.name in {"", ".", ".."} or "/" in path.name or "\\" in path.name:
        raise BackupPackageError("backup staging path is unsafe")
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise BackupPackageError("backup staging parent cannot be opened safely") from exc
    try:
        return os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except FileExistsError as exc:
        raise BackupPackageError("backup staging leaf already exists") from exc
    except OSError as exc:
        raise BackupPackageError("backup staging leaf cannot be created safely") from exc
    finally:
        os.close(parent_fd)


def write_backup_package(
    staging_path: Path,
    source: BackupCaptureSource,
    *,
    recovery_artifacts_included: bool,
    fault_hook: Callable[[str], object] | None = None,
) -> StagedBackup:
    """Write one exact closed package to a same-directory staging leaf."""
    manifest, manifest_bytes = _manifest(
        source,
        recovery_artifacts_included=recovery_artifacts_included,
    )
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise BackupPackageResourceLimit("backup manifest exceeds bounded size")
    total_uncompressed = sum(item.uncompressed_size for item in manifest.entry_inventory)
    _preflight_space(staging_path, total_uncompressed)
    descriptor = _open_staging_leaf(staging_path)
    try:
        with os.fdopen(descriptor, "r+b") as raw:
            with zipfile.ZipFile(raw, "w", allowZip64=True) as package:
                package.writestr(_zip_info("manifest.json"), manifest_bytes)
                for entry in source.entries:
                    with package.open(
                        _zip_info(entry.path), "w", force_zip64=True
                    ) as member:
                        if entry.content is not None:
                            member.write(entry.content)
                        elif entry.reader is not None:
                            with entry.reader() as stream:
                                while block := stream.read(_CHUNK):
                                    member.write(block)
                        else:
                            raise BackupPackageError("backup entry has no readable source")
                    _fault(f"after-entry:{entry.path}", fault_hook)
                package.writestr(_zip_info("COMPLETED"), b"")
                _fault("after-completion-marker", fault_hook)
            raw.flush()
            os.fsync(raw.fileno())
            _fault("after-staging-file-fsync", fault_hook)
            size = raw.tell()
    except BaseException:
        raise
    identity = os.fstat(_identity_fd(staging_path))
    if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.geteuid() or identity.st_nlink != 1:
        raise BackupPackageError("backup staging identity changed during finalization")
    if identity.st_size != size:
        raise BackupPackageError("backup staging size is inconsistent")
    artifact_hash = _hash_staging(staging_path, expected_size=size)
    return StagedBackup(
        staging_path,
        manifest,
        manifest_bytes,
        size,
        artifact_hash,
    )


def _identity_fd(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)


def _hash_staging(path: Path, *, expected_size: int) -> str:
    fd = _identity_fd(path)
    try:
        value = os.fstat(fd)
        if value.st_size != expected_size:
            raise BackupPackageError("backup staging size changed before hashing")
        digest = hashlib.sha256()
        offset = 0
        while offset < value.st_size:
            block = os.pread(fd, _CHUNK, offset)
            if not block:
                raise BackupPackageError("backup staging file is truncated")
            digest.update(block)
            offset += len(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _preflight_space(path: Path, total_uncompressed: int) -> None:
    try:
        value = os.statvfs(path.parent)
    except OSError as exc:
        raise BackupResourceLimit("backup destination capacity is unavailable") from exc
    available = int(value.f_bavail) * int(value.f_frsize)
    # Advisory reserve for SQLite temporaries, ZIP local headers/central
    # directory, block rounding, and destination directory metadata.
    reserve = max(64 * 1024 * 1024, total_uncompressed // 32)
    if available < total_uncompressed + reserve:
        raise BackupResourceLimit("backup destination has insufficient space")
    if value.f_favail <= 0:
        raise BackupResourceLimit("backup destination has insufficient inodes")


def _central_directory(source: BinaryIO) -> list[tuple[str, int, int, int]]:
    source.seek(0, os.SEEK_END)
    artifact_size = source.tell()
    if artifact_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise BackupPackageResourceLimit("backup artifact exceeds bounded size")
    tail_size = min(artifact_size, 65557 + _EOCD.size)
    source.seek(artifact_size - tail_size)
    tail = source.read(tail_size)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < _EOCD.size:
        raise BackupPackageError("backup ZIP central directory is truncated")
    values = _EOCD.unpack_from(tail, offset)
    _, disk, directory_disk, disk_entries, entries, directory_size, directory_offset, comment = values
    zip64 = entries == 0xFFFF or disk_entries == 0xFFFF
    if disk or directory_disk or (not zip64 and disk_entries != entries):
        raise BackupPackageError("multi-volume backup ZIP is unsupported")
    if comment != len(tail) - offset - _EOCD.size:
        raise BackupPackageError("backup ZIP comment is malformed")
    if zip64:
        locator_offset = offset - _EOCD64_LOCATOR.size
        if locator_offset < 0:
            raise BackupPackageError("backup ZIP64 locator is absent")
        locator = _EOCD64_LOCATOR.unpack_from(tail, locator_offset)
        if locator[0] != 0x07064B50 or locator[1] or locator[3] != 1:
            raise BackupPackageError("backup ZIP64 locator is malformed")
        source.seek(locator[2])
        record = source.read(_EOCD64.size + 12)
        if len(record) < _EOCD64.size or record[:4] != b"PK\x06\x06":
            raise BackupPackageError("backup ZIP64 directory is malformed")
        _, _, _, disk, directory_disk, _, disk_entries64, entries64, directory_size64, directory_offset64 = _EOCD64.unpack_from(record)
        if disk or directory_disk or disk_entries64 != entries64:
            raise BackupPackageError("multi-volume backup ZIP is unsupported")
        if entries64 > MAX_ENTRY_COUNT or artifact_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BackupPackageResourceLimit("backup ZIP central directory exceeds bounded limits")
        entries = entries64
        directory_size = directory_size64
        directory_offset = directory_offset64
    else:
        if entries > MAX_ENTRY_COUNT or directory_size > MAX_ENTRY_COUNT * 512:
            raise BackupPackageResourceLimit("backup ZIP central directory exceeds bounded limits")
    trailing = len(tail) - offset - _EOCD.size
    if directory_offset + directory_size + _EOCD.size + trailing != artifact_size:
        raise BackupPackageError("backup ZIP directory does not end at EOCD")
    source.seek(directory_offset)
    directory = source.read(directory_size)
    if len(directory) != directory_size:
        raise BackupPackageError("backup ZIP directory is truncated")
    result: list[tuple[str, int, int, int]] = []
    cursor = 0
    for _ in range(entries):
        if cursor + _CENTRAL.size > len(directory):
            raise BackupPackageError("backup ZIP directory is malformed")
        fields = _CENTRAL.unpack_from(directory, cursor)
        signature, _, _, flags, method, _, _, crc, compressed, uncompressed, name_size, extra_size, comment_size, _, _, _, local_offset = fields
        if signature != 0x02014B50 or flags & ~0x800:
            raise BackupPackageError("backup ZIP entry flags are unsupported")
        if method != zipfile.ZIP_STORED:
            raise BackupPackageError("backup ZIP entries must be stored")
        cursor += _CENTRAL.size
        raw_name = directory[cursor:cursor + name_size]
        cursor += name_size + extra_size + comment_size
        try:
            name = raw_name.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise BackupPackageError("backup ZIP entry name is not UTF-8") from exc
        if compressed != uncompressed:
            raise BackupPackageError("backup ZIP stored size is inconsistent")
        result.append((name, local_offset, uncompressed, crc))
    return result


def _validate_zip_names(names: list[str]) -> None:
    if len(names) != len(set(names)):
        raise BackupPackageError("backup ZIP contains duplicate entries")
    collision_keys = {
        unicodedata.normalize("NFC", name).casefold() for name in names
    }
    if len(collision_keys) != len(names):
        raise BackupPackageError("backup ZIP paths collide under Unicode normalisation")
    for name in names:
        try:
            validate_entry_path(name)
        except BackupManifestError as exc:
            raise BackupPackageError("backup ZIP entry path is invalid") from exc


def _verify_entry(package: zipfile.ZipFile, info: zipfile.ZipInfo, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with package.open(info, "r") as stream:
        while block := stream.read(_CHUNK):
            size += len(block)
            if size > max_bytes:
                raise BackupPackageResourceLimit("backup entry exceeds bounded size")
            digest.update(block)
        if stream.read(1):
            raise BackupPackageError("backup entry size is inconsistent")
    if size != info.file_size:
        raise BackupPackageError("backup entry size does not match ZIP metadata")
    return size, digest.hexdigest()


def _manifest_from_package(package: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[dict[str, object], bytes]:
    if info.file_size > MAX_MANIFEST_BYTES:
        raise BackupPackageResourceLimit("backup manifest exceeds bounded size")
    with package.open(info, "r") as stream:
        raw = stream.read()
        if stream.read(1):
            raise BackupPackageResourceLimit("backup manifest exceeds bounded size")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BackupPackageResourceLimit("backup manifest exceeds bounded size")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, BackupPackageError) as exc:
        raise BackupPackageError("backup manifest is malformed JSON") from exc
    try:
        validated = validate_manifest_object(value)
    except BackupManifestUnsupported as exc:
        raise BackupPackageUnsupported(str(exc)) from exc
    except BackupManifestError as exc:
        raise BackupPackageError(str(exc)) from exc
    canonical = canonical_backup_json(value)
    if canonical != raw:
        raise BackupPackageError("backup manifest is not canonical")
    return validated, raw


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BackupPackageError("backup manifest contains duplicate keys")
        result[key] = value
    return result


def _verify_database(package: zipfile.ZipFile, info: zipfile.ZipInfo, manifest: Mapping[str, object]) -> None:
    expected_size = int(manifest["capture"]["source_database_size"])
    expected_hash = str(manifest["capture"]["source_database_sha256"])
    if info.file_size != expected_size:
        raise BackupPackageError("packaged SQLite size is inconsistent")
    scratch_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="bots5-backup-verify-", suffix=".sqlite3", delete=False) as handle:
            scratch_name = handle.name
            digest = hashlib.sha256()
            size = 0
            with package.open(info, "r") as stream:
                while block := stream.read(_CHUNK):
                    handle.write(block)
                    digest.update(block)
                    size += len(block)
                    if size > MAX_SCRATCH_BYTES:
                        raise BackupPackageResourceLimit("backup SQLite scratch exceeds bounded limit")
            handle.flush()
            os.fsync(handle.fileno())
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise BackupPackageError("packaged SQLite digest or size is inconsistent")
        connection = sqlite3.connect(f"file:{scratch_name}?mode=ro", uri=True)
        try:
            try:
                result = connection.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    raise BackupPackageError("packaged SQLite integrity_check failed")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise BackupPackageError("packaged SQLite foreign keys are inconsistent")
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            except sqlite3.Error as exc:
                raise BackupPackageError("packaged SQLite cannot be independently read") from exc
            if revision != [(str(manifest["source_db_migration_revision"]),)]:
                raise BackupPackageError("packaged SQLite revision is inconsistent")
            from bots5.infrastructure.persistence.migration_runner import _MIGRATION_CHAIN
            declared_revision = str(revision[0][0])
            if declared_revision not in _MIGRATION_CHAIN:
                raise BackupPackageUnsupported("packaged SQLite revision is unsupported")
            from sqlalchemy import create_engine
            from sqlalchemy.pool import NullPool
            from sqlalchemy import event
            from bots5.infrastructure.persistence.transition_guard import (
                install_transition_guard,
            )

            engine = create_engine(
                f"sqlite:///file:{scratch_name}?mode=ro&uri=true",
                poolclass=NullPool,
                future=True,
            )
            event.listen(engine, "connect", install_transition_guard)
            try:
                with engine.connect() as sql_connection:
                    try:
                        _validate_packaged_schema(
                            sql_connection,
                            revision=declared_revision,
                        )
                    except Exception as exc:
                        raise BackupPackageError(
                            "packaged SQLite domain schema is invalid"
                        ) from exc
            finally:
                engine.dispose()
            attachment_capable = declared_revision >= _ATTACHMENT_CAPABILITY_REVISION
            if attachment_capable:
                try:
                    counts = connection.execute(
                        "SELECT state, count(*) FROM attachment_blobs GROUP BY state"
                    ).fetchall()
                except sqlite3.Error as exc:
                    raise BackupPackageError("packaged SQLite attachment state is unreadable") from exc
                observed = {str(state): int(count) for state, count in counts}
                declared = manifest["capture"]["attachment_blob_state_counts"]
                for state in ("ready", "staging", "deleting"):
                    if observed.get(state, 0) != int(declared[state]):
                        raise BackupPackageError("packaged attachment state counts are inconsistent")
            else:
                declared = manifest["capture"]["attachment_blob_state_counts"]
                if any(int(declared[state]) != 0 for state in ("ready", "staging", "deleting")):
                    raise BackupPackageError(
                        "pre-0009 backup cannot declare attachment state"
                    )
                if int(manifest["capture"]["required_payload_count"]) != 0:
                    raise BackupPackageError(
                        "pre-0009 backup cannot declare required attachment payloads"
                    )
            _verify_payload_mapping(
                connection, manifest, attachment_capable=attachment_capable
            )
        except sqlite3.Error as exc:
            raise BackupPackageError("packaged SQLite cannot be independently read") from exc
        finally:
            connection.close()
    finally:
        if scratch_name is not None:
            try:
                os.unlink(scratch_name)
            except FileNotFoundError:
                pass


def _digest_text(value: bytes) -> str:
    if len(value) != 32:
        raise BackupPackageError("packaged SQLite attachment digest is malformed")
    return value.hex()


def _validate_packaged_schema(connection, *, revision: str) -> None:
    """Run the validator subset that is authoritative for each frozen revision."""
    import importlib

    from bots5.infrastructure.persistence.sqlite import (
        _validate_open_connection,
        _validate_phase4_schema,
    )

    if revision >= _ATTACHMENT_CAPABILITY_REVISION:
        _validate_open_connection(
            connection,
            expected_revision=revision,
            destructive_phase6=False,
        )
        return
    if revision >= "0006_phase4_workspace":
        _validate_phase4_schema(connection)
    if revision >= "0003_integrity_boundaries":
        integrity = importlib.import_module(
            "bots5.infrastructure.persistence.migrations.versions."
            "0003_integrity_boundaries"
        )
        integrity._validate_existing_state(connection)


def _verify_payload_mapping(
    connection: sqlite3.Connection,
    manifest: Mapping[str, object],
    *,
    attachment_capable: bool,
) -> None:
    inventory = manifest["entry_inventory"]
    payload_paths = {
        str(row["path"]).removeprefix("payloads/sha256/")
        for row in inventory
        if str(row["logical_role"]) == "attachment-payload"
    }
    if not attachment_capable:
        if payload_paths:
            raise BackupPackageError(
                "pre-0009 backup cannot contain attachment payloads"
            )
        return
    required = connection.execute(
        "SELECT hex(digest), byte_size FROM attachment_blobs WHERE state='ready'"
    ).fetchall()
    has_import_tables = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
            "('archive_import_payload_reservations','archive_import_operations')"
        ).fetchall()
        == [(1,), (1,)]
    )
    reservations = (
        connection.execute(
            "SELECT hex(r.digest), r.size FROM archive_import_payload_reservations r "
            "JOIN archive_import_operations o ON o.id=r.operation_id "
            "WHERE r.publication_state='READY' AND o.state IN ('STAGING','COMMITTING')"
        ).fetchall()
        if has_import_tables
        else []
    )
    required_digests = {str(row[0]).lower() for row in required}
    required_digests.update(str(row[0]).lower() for row in reservations)
    if payload_paths != required_digests:
        raise BackupPackageError("packaged SQLite and payload inventory are not bijective")
    sizes = {str(row[0]).lower(): int(row[1]) for row in (*required, *reservations)}
    for row in inventory:
        path = str(row["path"])
        if path.startswith("payloads/sha256/"):
            digest = path.removeprefix("payloads/sha256/")
            if sizes.get(digest) != int(row["uncompressed_size"]):
                raise BackupPackageError("payload entry size contradicts SQLite")


def verify_backup_package(path: Path, *, expected_backup_id: str | None = None) -> BackupReceipt:
    """Verify only the artifact and its contained SQLite state."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1:
            raise BackupPackageError("backup artifact has unsafe identity")
    finally:
        os.close(fd)
    with path.open("rb", buffering=0) as source:
        artifact_hash = hashlib.sha256()
        artifact_size = 0
        source.seek(0)
        while block := source.read(_CHUNK):
            artifact_hash.update(block)
            artifact_size += len(block)
            if artifact_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise BackupPackageResourceLimit("backup artifact exceeds bounded size")
        source.seek(0)
        records = _central_directory(source)
        with zipfile.ZipFile(source, "r", allowZip64=True) as package:
            infos = package.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(records):
                raise BackupPackageError("backup ZIP directory is inconsistent")
            _validate_zip_names(names)
            if len(names) < 3 or names[0] != "manifest.json" or names[-1] != "COMPLETED" or names[1] != "database/state.sqlite3":
                raise BackupPackageError("backup ZIP entry order is invalid")
            if "COMPLETED" in names[1:-1]:
                raise BackupPackageError("backup completion marker is misplaced")
            middle = names[1:-1]
            if middle != sorted(middle):
                raise BackupPackageError("backup ZIP entries are not sorted")
            manifest, manifest_bytes = _manifest_from_package(package, infos[0])
            if expected_backup_id is not None and manifest["backup_id"] != expected_backup_id:
                raise BackupPackageError("backup id does not match expected identity")
            total = int(manifest["declared_total_uncompressed_bytes"])
            if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise BackupPackageResourceLimit("backup declared total exceeds bounded limit")
            by_path = {info.filename: info for info in infos}
            inventory_by_path = {
                str(row["path"]): row for row in manifest["entry_inventory"]
            }
            observed_rows: list[dict[str, object]] = []
            checks: list[str] = ["artifact-sha256", "zip-structure", "manifest-canonical"]
            for index, (name, record_offset, record_size, _crc) in enumerate(records):
                info = by_path[name]
                if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & ~0x800:
                    raise BackupPackageError("backup ZIP entry metadata is unsupported")
                if info.file_size != record_size:
                    raise BackupPackageError("backup ZIP entry size is inconsistent")
                if info.header_offset != record_offset:
                    raise BackupPackageError("backup ZIP local entry offset is inconsistent")
                if index == 0:
                    continue
                maximum = MAX_SCRATCH_BYTES if name == "database/state.sqlite3" else MAX_ENTRY_BYTES
                size, digest = _verify_entry(package, info, max_bytes=maximum)
                row = inventory_by_path.get(name)
                if row is None:
                    raise BackupPackageError("backup contains an undeclared ZIP entry")
                if row["uncompressed_size"] != size or row["sha256"] != digest:
                    raise BackupPackageError("backup entry digest or size is inconsistent")
                observed_rows.append(row)
                checks.append(f"entry:{name}")
            if logical_content_digest(
                tuple(
                    BackupManifestEntry(
                        str(row["path"]), str(row["media_type"]), str(row["logical_role"]),
                        True, int(row["uncompressed_size"]), str(row["sha256"]),
                    )
                    for row in observed_rows
                )
            ) != manifest["logical_content_digest"]:
                raise BackupPackageError("backup logical digest is inconsistent")
            checks.append("logical-content-digest")
            _verify_database(package, by_path["database/state.sqlite3"], manifest)
            checks.extend(["sqlite-integrity", "payload-bijection"])
            return BackupReceipt(
                format=BACKUP_FORMAT,
                receipt_version=1,
                verification_id=str(uuid7()),
                verified_at=canonical_utc_timestamp(datetime.now(UTC)),
                backup_id=str(manifest["backup_id"]),
                backup_logical_content_digest=str(manifest["logical_content_digest"]),
                artifact_size=artifact_size,
                artifact_sha256=artifact_hash.hexdigest(),
                source_db_migration_revision=str(manifest["source_db_migration_revision"]),
                verifier_application_version="bots5-0.1.0",
                sqlite_runtime_version=sqlite3.sqlite_version,
                outcome="VALID",
                passed_checks=tuple(checks),
                failed_check_ids=(),
                reason_code=None,
            )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_CHUNK):
            digest.update(block)
    return digest.hexdigest()


class BackupZipPackageAdapter:
    """Core-facing adapter for the package and independent verifier."""

    def __init__(self, fault_hook: Callable[[str], object] | None = None) -> None:
        self._fault_hook = fault_hook

    def write(self, staging_path: Path, source: object, *, recovery_artifacts_included: bool):
        if not isinstance(source, BackupCaptureSource):
            raise BackupPackageError("backup package source has the wrong adapter type")
        try:
            return write_backup_package(
                staging_path,
                source,
                recovery_artifacts_included=recovery_artifacts_included,
                fault_hook=self._fault_hook,
            )
        except BackupPackageResourceLimit as exc:
            raise BackupResourceLimit(str(exc)) from exc
        except BackupPackageUnsupported as exc:
            raise BackupUnsupported(str(exc)) from exc
        except (BackupPackageError, BackupPackageUnsupported, OSError) as exc:
            raise BackupArchiveInvalid(str(exc)) from exc

    def verify(self, path: Path, *, expected_backup_id: str | None = None):
        try:
            return verify_backup_package(path, expected_backup_id=expected_backup_id)
        except BackupPackageResourceLimit as exc:
            raise BackupResourceLimit(str(exc)) from exc
        except BackupPackageUnsupported as exc:
            raise BackupUnsupported(str(exc)) from exc
        except (BackupPackageError, OSError, ValueError) as exc:
            raise BackupArchiveInvalid(str(exc)) from exc


class BackupFilePublicationAdapter:
    """Publish only after artifact verification using same-directory renameat2."""

    def __init__(self, fault_hook: Callable[[str], object] | None = None) -> None:
        self._fault_hook = fault_hook

    def publish(
        self,
        *,
        staging_path: Path,
        destination: Path,
        overwrite: bool,
        backup_id: str,
        cancellation=None,
    ) -> Path:
        if staging_path.parent != destination.parent:
            raise BackupPackageError("backup publication must be on the same filesystem")
        if type(overwrite) is not bool:
            raise BackupPackageError("backup overwrite flag is malformed")
        if cancellation is not None and not callable(cancellation):
            raise BackupPackageError("backup cancellation source is not callable")
        try:
            parent_fd = os.open(
                destination.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise BackupDestinationInvalid(
                "backup destination parent cannot be opened safely"
            ) from exc
        try:
            if cancellation is not None and cancellation():
                raise BackupResolutionCancelled("backup publication cancelled before rename")
            if overwrite:
                try:
                    existing_fd = os.open(
                        destination.name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    existing_fd = None
                except OSError as exc:
                    raise BackupDestinationInvalid(
                        "existing backup destination cannot be inspected"
                    ) from exc
                if existing_fd is None:
                    self._rename_new(parent_fd, staging_path.name, destination.name)
                else:
                    try:
                        value = os.fstat(existing_fd)
                        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1:
                            raise BackupDestinationInvalid(
                                "existing backup destination has unsafe identity"
                            )
                    finally:
                        os.close(existing_fd)
                    _fault("before-rename", self._fault_hook)
                    try:
                        _rename_exchange(
                            parent_fd, staging_path.name, parent_fd, destination.name
                        )
                    except OSError as exc:
                        raise BackupPublicationFailed(
                            "backup replacement rename failed"
                        ) from exc
                    _fault("after-rename", self._fault_hook)
                try:
                    _fault("before-parent-fsync", self._fault_hook)
                    os.fsync(parent_fd)
                    _fault("after-parent-fsync", self._fault_hook)
                except OSError as exc:
                    raise BackupUncertainPublication(
                        "backup replacement directory fsync is uncertain"
                    ) from exc
                replaced = destination.parent / f".bots5-backup-replaced-{backup_id}"
                if existing_fd is not None:
                    try:
                        os.rename(staging_path, replaced)
                        os.fsync(parent_fd)
                    except BaseException as exc:
                        raise BackupUncertainPublication(
                            "backup replacement publication outcome is uncertain"
                        ) from exc
                if cancellation is not None and cancellation():
                    return destination
                return destination
            self._rename_new(parent_fd, staging_path.name, destination.name)
            try:
                _fault("before-parent-fsync", self._fault_hook)
                os.fsync(parent_fd)
                _fault("after-parent-fsync", self._fault_hook)
            except OSError as exc:
                raise BackupUncertainPublication(
                    "backup publication directory fsync is uncertain"
                ) from exc
            return destination
        except FileExistsError as exc:
            raise BackupDestinationExists(
                f"backup destination already exists: {destination.name}"
            ) from exc
        finally:
            os.close(parent_fd)

    def _rename_new(self, parent_fd: int, staging_leaf: str, destination_leaf: str) -> None:
        _fault("before-rename", self._fault_hook)
        try:
            _rename_noreplace(parent_fd, staging_leaf, parent_fd, destination_leaf)
        except FileExistsError:
            raise
        except OSError as exc:
            raise BackupPublicationFailed("backup publication rename failed") from exc
        _fault("after-rename", self._fault_hook)
