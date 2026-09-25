"""Rooted Backup v1 capture adapter and exclusive recovery-point fence."""

from __future__ import annotations

import os
import stat
import sqlite3
import time
import uuid
from uuid6 import uuid7
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from contextlib import nullcontext

from bots5.core.backup import BackupPackagePort, BackupProgress, BackupProgressState
from bots5.core.errors import (
    BackupArchiveInvalid,
    BackupError,
    BackupResourceLimit,
    BackupResolutionCancelled,
    BackupUncertainPublication,
    BackupUnclassifiedState,
    BackupUnsupported,
)
from bots5.domain.backup import (
    BackupCaptureFacts,
    BackupRootModel,
)
from bots5.infrastructure.backup_package import BackupSourceEntry

from bots5.infrastructure.app_paths import AppPaths
from bots5.infrastructure.attachments import _identity
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.rooted_sqlite_vfs import (
    RootedSQLiteVfs,
    RootedVfsRegistrationCloseUnknown,
)
from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore


_CHUNK = 1024 * 1024
_OBJECT_NAME = __import__("re").compile(r"[0-9a-f]{64}\Z")
_SOURCE_HASH_SECONDS = 6 * 60 * 60
_ATTACHMENT_CAPABILITY_REVISION = "0009_phase6_context_attachments"


def _cancellation_requested(cancellation: object) -> bool:
    if cancellation is None:
        return False
    if not callable(cancellation):
        raise BackupError("backup cancellation source is not callable")
    return bool(cancellation())


@dataclass(slots=True)
class _OwnedStream:
    descriptor: int

    def reader(self) -> BinaryIO:
        duplicate = os.dup(self.descriptor)
        return os.fdopen(duplicate, "rb", buffering=0)

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(slots=True)
class BackupCaptureResult:
    source: object
    recovery_artifacts_included: bool


class RootedBackupCaptureAdapter:
    """Capture Backup v1 from one authority-owned coherent recovery cut."""

    def __init__(
        self,
        authority: DataRootAuthority,
        store: SQLiteAppStateStore,
        paths: AppPaths,
        package: BackupPackagePort,
        *,
        data_root_is_override: bool,
    ) -> None:
        self._authority = authority
        self._store = store
        self._paths = paths
        self._package = package
        self._root_model = BackupRootModel(
            data_root_is_override=data_root_is_override,
            config_root_is_override=data_root_is_override,
            state_root_is_override=data_root_is_override,
        )

    def capture_under_fence(
        self,
        *,
        progress: BackupProgress,
        destination: Path,
        cancellation=None,
        staging_path: Path | None = None,
        pre_reconciliation: bool = False,
        internal_destination: bool = False,
        progress_callback=None,
    ) -> BackupCaptureResult:
        if staging_path is None:
            raise BackupError("backup capture requires an owned staging path")
        self._reject_destination_inside_recovery_roots(
            destination, internal_destination=internal_destination
        )
        owned_files: list[_OwnedStream] = []
        snapshot_leaf: str | None = None
        try:
            with self._authority.operation():
                mutation_gate = (
                    self._store.mutation_transition()
                    if self._store is not None
                    else nullcontext()
                )
                with mutation_gate, self._authority.transition():
                    try:
                        if progress_callback is not None and not callable(progress_callback):
                            raise BackupError("backup progress callback is not callable")
                        progress = BackupProgress(
                            BackupProgressState.HOLDING_RECOVERY_POINT_FENCE,
                            progress.operation_id,
                        )
                        if progress_callback is not None:
                            progress_callback(progress)
                        if _cancellation_requested(cancellation):
                            raise BackupResolutionCancelled(
                                "backup capture cancelled before the recovery cut"
                            )
                        self._authority.database_durability_fence()
                        snapshot_leaf, snapshot_stream, durability = self._snapshot_database()
                        owned_files.append(snapshot_stream)
                        facts, source_entries, recovery_included, revision = self._inventory_capture(
                            snapshot_stream,
                            pre_reconciliation=pre_reconciliation,
                            durability=durability,
                        )
                        if _cancellation_requested(cancellation):
                            raise BackupResolutionCancelled(
                                "backup capture cancelled after the recovery cut"
                            )
                        source_entries.insert(
                            0,
                            BackupSourceEntry(
                                path="database/state.sqlite3",
                                size=facts.source_database_size,
                                sha256=facts.source_database_sha256,
                                reader=snapshot_stream.reader,
                            ),
                        )
                        from bots5.infrastructure.backup_package import (
                            BackupCaptureSource,
                        )

                        package_source = BackupCaptureSource(
                            backup_id=str(uuid7()),
                            created_at=_utc_now_iso(),
                            source_application_version="bots5-0.1.0",
                            source_db_migration_revision=revision,
                            facts=facts,
                            entries=tuple(source_entries),
                        )
                        self._reject_unclassified_roots()
                        if _cancellation_requested(cancellation):
                            raise BackupResolutionCancelled(
                                "backup capture cancelled before finalization"
                            )
                        progress = BackupProgress(
                            BackupProgressState.FINALIZING,
                            progress.operation_id,
                        )
                        if progress_callback is not None:
                            progress_callback(progress)
                        staged = self._package.write(
                            staging_path,
                            package_source,
                            recovery_artifacts_included=recovery_included,
                        )
                        if _cancellation_requested(cancellation):
                            raise BackupResolutionCancelled(
                                "backup capture cancelled after finalization"
                            )
                        return BackupCaptureResult(staged, recovery_included)
                    finally:
                        for stream in owned_files:
                            stream.close()
                        owned_files.clear()
                        if snapshot_leaf is not None:
                            self._cleanup_snapshot(snapshot_leaf)
                            snapshot_leaf = None
        finally:
            del owned_files, snapshot_leaf

    def _reject_destination_inside_recovery_roots(
        self, destination: Path, *, internal_destination: bool
    ) -> None:
        path = destination.absolute()
        roots = (
            self._paths.data_root,
            self._paths.config_root,
            self._paths.state_root,
            self._paths.cache_root,
            self._paths.logs_root,
        )
        inside_data = _is_relative_to(path, self._paths.data_root)
        if not internal_destination and any(_is_relative_to(path, root) for root in roots):
            raise BackupError("backup destination is inside a source recovery root")
        if internal_destination and inside_data and not _is_relative_to(
            path, self._paths.data_root / "recovery"
        ):
            raise BackupError("internal backup destination must be in authority-owned recovery")

    def _reject_unclassified_roots(self) -> None:
        allowed_top_level = {"database", "attachments", "recovery"}
        for root in (self._paths.config_root, self._paths.state_root, self._paths.cache_root):
            try:
                relative = root.relative_to(self._paths.data_root)
            except ValueError:
                continue
            if relative.parts:
                allowed_top_level.add(relative.parts[0])
        observed_top_level = self._root_top_level_inventory()
        unknown = sorted(set(observed_top_level) - allowed_top_level)
        if unknown:
            raise BackupUnclassifiedState(
                "data-root top level contains unclassified state: "
                + ", ".join(unknown)
            )
        for root in (self._paths.config_root, self._paths.state_root):
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise BackupUnclassifiedState("closed recovery root is not a directory")
            with os.scandir(root) as entries:
                if any(
                    entry.name != "logs" for entry in entries
                ):
                    raise BackupUnclassifiedState(
                        "closed recovery root contains unclassified state"
                    )

    def _root_top_level_inventory(self) -> tuple[str, ...]:
        """List the held data root without treating child count as freshness."""
        root_fd = self._authority._root_capability
        duplicate_fd = os.dup(root_fd)
        try:
            identity = os.fstat(duplicate_fd)
            if (
                not stat.S_ISDIR(identity.st_mode)
                or identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) != 0o700
            ):
                raise BackupUnclassifiedState(
                    "data-root observation capability has unsafe identity"
                )
            return tuple(sorted(os.listdir(duplicate_fd)))
        except OSError as exc:
            raise BackupUnclassifiedState(
                "data-root top level cannot be observed safely"
            ) from exc
        finally:
            os.close(duplicate_fd)

    def _snapshot_database(self) -> tuple[str, _OwnedStream, tuple[str, int, bool]]:
        temp_fd = self._authority._directory_fd("database/temp")
        leaf = f".bots5-backup-snapshot-{uuid.uuid4().hex}"
        descriptor = os.open(
            leaf,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=temp_fd,
        )
        source_vfs = self._open_backup_intake()
        source = None
        target = None
        try:
            source = source_vfs.connect()
            source.execute("PRAGMA foreign_keys=ON")
            source.execute("BEGIN")
            target = sqlite3.connect(f"/proc/self/fd/{descriptor}", uri=False)
            source.backup(target)
            # The packaged database must be standalone.  Normalize only this
            # private snapshot and report its finalized durability facts.
            journal_mode = str(
                target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            ).lower()
            if journal_mode != "delete":
                raise BackupError("backup snapshot could not be made standalone")
            target.execute("PRAGMA synchronous=FULL")
            synchronous = int(target.execute("PRAGMA synchronous").fetchone()[0])
            if synchronous != 2:
                raise BackupError("backup snapshot is not synchronous FULL")
            target.execute("PRAGMA foreign_keys=ON")
            foreign_keys = bool(target.execute("PRAGMA foreign_keys").fetchone()[0])
            target.close()
            target = None
            os.fsync(descriptor)
            identity = os.fstat(descriptor)
            expected = _identity(descriptor)
            if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
                raise BackupError("database snapshot has unsafe identity")
            if identity.st_size != expected.size:
                raise BackupError("database snapshot identity changed")
            source.execute("COMMIT")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return leaf, _OwnedStream(descriptor), (journal_mode, synchronous, foreign_keys)
        except BaseException:
            if target is not None:
                target.close()
            try:
                source.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            os.close(descriptor)
            self._cleanup_snapshot(leaf)
            raise
        finally:
            if source is not None:
                source.close()
            if source_vfs.open_count != 0:
                raise BackupError("backup snapshot left SQLite handles open")
            source_vfs.close()

    def _open_backup_intake(self) -> RootedSQLiteVfs:
        """Open the canonical source through the authority's WAL-capable intake VFS."""
        try:
            return RootedSQLiteVfs(
                database_dir_fd=self._authority._database_dir_capability,
                main_claim_fd=self._authority._claim_database(),
                temp_dir_fd=self._authority._directory_fd("database/temp"),
                mount_id=self._authority._root_identity.mount_id,
                main_leaf="state.sqlite3",
                journal_leaf="state.sqlite3-journal",
                intake_wal=True,
                authority=self._authority,
                resource_label=f"backup-intake-vfs:{uuid.uuid4().hex}",
            )
        except RootedVfsRegistrationCloseUnknown as exc:
            raise BackupError(
                "rooted backup intake VFS registration cleanup incomplete"
            ) from exc

    def _cleanup_snapshot(self, leaf: str) -> None:
        try:
            os.unlink(leaf, dir_fd=self._authority._directory_fd("database/temp"))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackupUnclassifiedState(
                "owned database snapshot cleanup failed"
            ) from exc

    def _inventory_capture(
        self,
        snapshot_stream: _OwnedStream,
        *,
        pre_reconciliation: bool,
        durability: tuple[str, int, bool],
    ) -> tuple[BackupCaptureFacts, list[object], bool, str]:
        """Return one closed capture source and its recovery-artifact flag."""
        scratch_path = f"/proc/self/fd/{snapshot_stream.descriptor}"
        connection = sqlite3.connect(f"file:{scratch_path}?mode=ro", uri=True)
        try:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            if revision is None:
                raise BackupUnclassifiedState("database snapshot has no migration revision")
            revision_id = str(revision[0])
            attachment_capable = revision_id >= _ATTACHMENT_CAPABILITY_REVISION
            has_attachment_blobs = _has_table(connection, "attachment_blobs")
            if attachment_capable and not has_attachment_blobs:
                raise BackupUnclassifiedState(
                    "database snapshot lacks required attachment schema"
                )
            counts = dict(
                connection.execute(
                    "SELECT state, count(*) FROM attachment_blobs GROUP BY state"
                ).fetchall()
            ) if has_attachment_blobs else {}
            attributable_objects: set[str] = set()
            required_rows: list[tuple[object, ...]] = []
            if has_attachment_blobs:
                required_rows = connection.execute(
                    "SELECT hex(digest), byte_size FROM attachment_blobs WHERE state='ready' "
                    "ORDER BY digest"
                ).fetchall()
                attributable_objects.update(
                    str(row[0]).lower()
                    for row in connection.execute("SELECT hex(digest) FROM attachment_blobs")
                )
            reservations = (
                connection.execute(
                    "SELECT hex(r.digest), r.size FROM archive_import_payload_reservations r "
                    "JOIN archive_import_operations o ON o.id=r.operation_id "
                    "WHERE r.publication_state='READY' AND o.state IN ('STAGING','COMMITTING')"
                ).fetchall()
                if _has_table(connection, "archive_import_payload_reservations")
                and _has_table(connection, "archive_import_operations")
                else []
            )
            attributable_objects.update(str(row[0]).lower() for row in reservations)
            required: dict[str, int] = {
                str(row[0]).lower(): int(row[1]) for row in required_rows
            }
            for digest, size in reservations:
                required.setdefault(str(digest).lower(), int(size))
            for name in self._authority.fresh_directory_inventory("attachments/objects"):
                if _OBJECT_NAME.fullmatch(name) is None or name not in attributable_objects:
                    raise BackupUnclassifiedState(
                        "unattributable attachment payload object"
                    )
            payload_entries = [
                self._payload_entry(digest, size) for digest, size in sorted(required.items())
            ]
            lifecycle_entries = self._lifecycle_entries(
                connection,
                pre_reconciliation=pre_reconciliation,
                attachment_capable=attachment_capable,
            )
            source_revision = (
                int(connection.execute(
                    "SELECT source_revision FROM search_source_state LIMIT 2"
                ).fetchone()[0])
                if _has_table(connection, "search_source_state")
                else 0
            )
            database_size = os.fstat(snapshot_stream.descriptor).st_size
            database_hash = _hash_descriptor(
                snapshot_stream.descriptor,
                database_size,
                deadline=time.monotonic() + _SOURCE_HASH_SECONDS,
            )
            os.lseek(snapshot_stream.descriptor, 0, os.SEEK_SET)
            facts = BackupCaptureFacts(
                journal_mode=durability[0],
                synchronous=durability[1],
                foreign_keys=durability[2],
                source_database_size=database_size,
                source_database_sha256=database_hash,
                ready_blob_count=int(counts.get("ready", 0)),
                staging_blob_count=int(counts.get("staging", 0)),
                deleting_blob_count=int(counts.get("deleting", 0)),
                required_payload_count=len(required),
                search_source_revision=source_revision,
                root_model=self._root_model,
            )
            return facts, [*payload_entries, *lifecycle_entries], bool(lifecycle_entries), str(revision[0])
        finally:
            connection.close()

    def _payload_entry(self, digest: str, size: int) -> object:
        objects_fd = self._authority._directory_fd("attachments/objects")
        leaf = digest
        stream = self._open_regular(objects_fd, leaf, expected_size=size)
        return BackupSourceEntry(
            path=f"payloads/sha256/{digest}",
            size=size,
            sha256=digest,
            reader=stream.reader,
        )

    def _lifecycle_entries(
        self,
        connection: sqlite3.Connection,
        *,
        pre_reconciliation: bool,
        attachment_capable: bool,
    ) -> list[object]:
        entries: list[object] = []
        for namespace in ("staging", "captures", "gc"):
            relative = f"attachments/{namespace}"
            names = self._authority.fresh_directory_inventory(relative)
            if not attachment_capable:
                if names:
                    raise BackupUnclassifiedState(
                        "unattributable attachment lifecycle recovery artifact"
                    )
                continue
            if names and not pre_reconciliation:
                raise BackupUnclassifiedState(
                    "healthy READY recovery point contains attachment lifecycle state"
                )
            allowed = self._allowed_lifecycle_leaves(connection, namespace)
            for name in names:
                if name not in allowed:
                    raise BackupUnclassifiedState(
                        "unattributable attachment lifecycle recovery artifact"
                    )
                fd = self._authority._directory_fd(relative)
                stream = self._open_regular(fd, name)
                entries.append(
                    BackupSourceEntry(
                        path=f"recovery-artifacts/attachments/{namespace}/{name}",
                        size=stream.size,
                        sha256=stream.sha256,
                        reader=stream.reader,
                    )
                )
        return entries

    def _allowed_lifecycle_leaves(
        self, connection: sqlite3.Connection, namespace: str
    ) -> set[str]:
        if namespace == "gc":
            return {
                str(row[0]) for row in connection.execute(
                    "SELECT gc_id FROM attachment_blobs WHERE state='deleting' AND gc_id IS NOT NULL"
                )
            }
        column = "stage_name" if namespace == "staging" else "operation_id"
        return {
            str(row[0]) for row in connection.execute(
                f"SELECT {column} FROM attachment_blobs WHERE state='staging' AND {column} IS NOT NULL"
            )
        }

    def _open_regular(self, directory_fd: int, leaf: str, *, expected_size: int | None = None):
        try:
            preliminary = os.stat(
                leaf,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise BackupUnclassifiedState("required source file is missing") from exc
        except OSError as exc:
            raise BackupUnclassifiedState("required source file cannot be inspected") from exc
        if (
            not stat.S_ISREG(preliminary.st_mode)
            or preliminary.st_uid != os.geteuid()
            or preliminary.st_nlink != 1
        ):
            raise BackupUnclassifiedState("required source file has unsafe identity")
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise BackupUnclassifiedState(
                "required source file cannot be opened safely"
            ) from exc
        try:
            value = _identity(descriptor)
            if not stat.S_ISREG(value.mode) or value.uid != os.geteuid() or value.nlink != 1:
                raise BackupUnclassifiedState("required source file has unsafe identity")
            if expected_size is not None and value.size != expected_size:
                raise BackupUnclassifiedState("required source file has inconsistent size")
            size = value.size
            digest = _hash_descriptor(
                descriptor,
                size,
                deadline=time.monotonic() + _SOURCE_HASH_SECONDS,
            )
            if _identity(descriptor) != value:
                raise BackupUnclassifiedState("required source file changed during capture")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return _HashedStream(descriptor, size, digest)
        except BaseException:
            os.close(descriptor)
            raise


@dataclass(slots=True)
class _HashedStream:
    descriptor: int
    size: int
    sha256: str

    def reader(self):
        duplicate = os.dup(self.descriptor)
        return os.fdopen(duplicate, "rb", buffering=0)


def _hash_descriptor(descriptor: int, size: int, *, deadline: float) -> str:
    import hashlib

    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        if time.monotonic() >= deadline:
            raise BackupResourceLimit("required source hashing exceeded bounded time")
        try:
            block = os.pread(descriptor, _CHUNK, offset)
        except OSError as exc:
            raise BackupUnclassifiedState("required source cannot be read") from exc
        if not block:
            raise BackupError("required source changed while being hashed")
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def create_migration_recovery_point(
    authority: DataRootAuthority,
    transaction_id: str,
) -> dict[str, object]:
    """Create, verify, and publish one fresh pre-migration Backup v1 point.

    The returned mapping is the only journal binding.  No receipt pathname,
    source pathname, secret value, or credential pointer is included.
    """
    from bots5.core.backup import BackupService
    from bots5.infrastructure.backup_package import (
        BackupFilePublicationAdapter,
        BackupZipPackageAdapter,
    )

    default_paths = resolve_default_app_paths()
    if default_paths.data_root == Path(authority.root):
        paths = default_paths
        data_root_is_override = False
    else:
        paths = resolve_override_app_paths(Path(authority.root))
        data_root_is_override = True
    capture = RootedBackupCaptureAdapter(
        authority,
        None,
        paths,
        BackupZipPackageAdapter(),
        data_root_is_override=data_root_is_override,
    )
    operation_id = transaction_id
    backup_id = str(uuid7())
    progress = BackupProgress(BackupProgressState.ACQUIRING_FENCE, operation_id)
    recovery_fd = authority._directory_fd("recovery")
    destination = paths.data_root / "recovery" / f"bots5-backup-{backup_id}.botsbackup"
    staging_path = paths.data_root / "recovery" / f".bots5-backup-{backup_id}.staging"
    try:
        captured = capture.capture_under_fence(
            progress=progress,
            destination=destination,
            staging_path=staging_path,
            pre_reconciliation=True,
            internal_destination=True,
        )
        staged = captured.source
        receipt = BackupZipPackageAdapter().verify(
            staging_path,
            expected_backup_id=staged.manifest.backup_id,
        )
        backup_id = receipt.backup_id
        destination = paths.data_root / "recovery" / f"bots5-backup-{backup_id}.botsbackup"
        published = BackupFilePublicationAdapter().publish(
            staging_path=staging_path,
            destination=destination,
            overwrite=False,
            backup_id=backup_id,
        )
    except BackupUncertainPublication:
        raise
    except Exception:
        _cleanup_migration_staging(authority, staging_path.name)
        raise
    return {
        "verification_receipt": receipt.canonical_object(),
        "published_leaf": published.name,
    }


def _cleanup_migration_staging(authority: DataRootAuthority, leaf: str) -> None:
    import os

    directory_fd = authority._directory_fd("recovery")
    try:
        os.unlink(leaf, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileNotFoundError:
        return


def resolve_default_app_paths() -> AppPaths:
    from bots5.infrastructure.app_paths import resolve_app_paths

    return resolve_app_paths(None)


def resolve_override_app_paths(root: Path) -> AppPaths:
    from bots5.infrastructure.app_paths import resolve_app_paths

    return resolve_app_paths(root)
