"""Core Backup v1 commands, progress and typed ports."""

from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from bots5.domain.backup import BackupManifest, BackupReceipt
from bots5.domain.ids import IdFactory
from bots5.core.errors import (
    BackupArchiveInvalid,
    BackupDestinationExists,
    BackupDestinationInvalid,
    BackupError,
    BackupPublicationFailed,
    BackupResourceLimit,
    BackupResolutionCancelled,
    BackupUncertainPublication,
    BackupUnclassifiedState,
    BackupUnsupported,
)


class BackupProgressState(str, Enum):
    ACQUIRING_FENCE = "acquiring-fence"
    HOLDING_RECOVERY_POINT_FENCE = "holding-recovery-point-fence"
    FINALIZING = "finalizing"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class BackupProgress:
    state: BackupProgressState
    operation_id: str


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_id: str
    destination: Path
    receipt: BackupReceipt
    manifest: BackupManifest


@dataclass(frozen=True, slots=True)
class VerificationResult:
    receipt: BackupReceipt


class BackupCapturePort(Protocol):
    """Adapter that owns the live capture and its exclusive recovery fence."""

    def capture_under_fence(
        self,
        *,
        progress: BackupProgress,
        destination: Path,
        staging_path: Path,
        cancellation: Callable[[], bool] | None = None,
        pre_reconciliation: bool = False,
        internal_destination: bool = False,
        progress_callback: Callable[[BackupProgress], object] | None = None,
    ) -> object:
        """Return a finalised staging package while holding the recovery fence."""
        ...


class BackupPackagePort(Protocol):
    def write(
        self,
        staging_path: Path,
        source: object,
        *,
        recovery_artifacts_included: bool,
    ) -> object:
        ...

    def verify(self, path: Path, *, expected_backup_id: str | None = None) -> object:
        ...


class BackupPublicationPort(Protocol):
    def publish(
        self,
        *,
        staging_path: Path,
        destination: Path,
        overwrite: bool,
        backup_id: str,
        cancellation: Callable[[], bool] | None = None,
    ) -> Path:
        ...


class BackupPort(Protocol):
    """Core boundary exposed to application composition."""

    def create_backup(
        self,
        destination: Path | str,
        *,
        overwrite: bool = False,
        cancellation: Callable[[], bool] | None = None,
        receipt_sink: Path | str | None = None,
        progress_callback: Callable[[BackupProgress], object] | None = None,
    ) -> BackupResult:
        ...

    def verify_backup(
        self,
        artifact: Path | str,
        *,
        expected_backup_id: str | None = None,
    ) -> VerificationResult:
        ...


def _absolute_destination(destination: Path) -> Path:
    raw = destination.expanduser()
    if not raw.is_absolute() or not raw.name or raw.name in {".", ".."}:
        raise BackupError("backup destination must be a file path")
    if "\x00" in str(raw):
        raise BackupError("backup destination is malformed")
    return raw.absolute()


class BackupService:
    """Create, independently verify, and publish one Backup v1 artifact."""

    def __init__(
        self,
        capture: BackupCapturePort,
        package: BackupPackagePort,
        publisher: BackupPublicationPort,
        ids: IdFactory,
    ) -> None:
        self._capture = capture
        self._package = package
        self._publisher = publisher
        self._ids = ids

    def create_backup(
        self,
        destination: Path | str,
        *,
        overwrite: bool = False,
        cancellation: Callable[[], bool] | None = None,
        receipt_sink: Path | str | None = None,
        progress_callback: Callable[[BackupProgress], object] | None = None,
    ) -> BackupResult:
        if not isinstance(overwrite, bool):
            raise BackupError("backup overwrite request must be boolean")
        path = _absolute_destination(Path(destination))
        operation_id = self._ids.new()
        try:
            destination_exists = path.exists()
        except OSError as exc:
            raise BackupDestinationInvalid(
                "backup destination cannot be inspected"
            ) from exc
        if destination_exists and not overwrite:
            raise BackupDestinationExists(f"backup destination already exists: {path.name}")
        if progress_callback is not None and not callable(progress_callback):
            raise BackupError("backup progress callback is not callable")
        progress = BackupProgress(BackupProgressState.ACQUIRING_FENCE, operation_id)
        self._report_progress(progress, progress_callback)
        staging_leaf = path.parent / f".bots5-backup-{operation_id}.staging"
        try:
            staged = self._capture.capture_under_fence(
                progress=progress,
                destination=path,
                staging_path=staging_leaf,
                cancellation=cancellation,
                progress_callback=progress_callback,
            )
            staged = staged.source
            progress = BackupProgress(
                BackupProgressState.VERIFYING, progress.operation_id
            )
            self._report_progress(progress, progress_callback)
            receipt = self._package.verify(
                staging_leaf, expected_backup_id=staged.manifest.backup_id
            )
            if receipt.artifact_sha256 != staged.artifact_sha256 or receipt.artifact_size != staged.artifact_size:
                raise BackupArchiveInvalid("staged backup changed before publication")
        except BaseException:
            self._cleanup_owned(staging_leaf)
            raise
        progress = BackupProgress(
            BackupProgressState.PUBLISHING, progress.operation_id
        )
        self._report_progress(progress, progress_callback)
        try:
            final_path = self._publisher.publish(
                staging_path=staging_leaf,
                destination=path,
                overwrite=overwrite,
                backup_id=staged.manifest.backup_id,
                cancellation=cancellation,
            )
        except BaseException as exc:
            if not isinstance(exc, BackupUncertainPublication):
                self._cleanup_owned(staging_leaf)
            raise
        self._persist_receipt(receipt, receipt_sink)
        completed = BackupProgress(
            BackupProgressState.COMPLETED, progress.operation_id
        )
        self._report_progress(completed, progress_callback)
        return BackupResult(
            backup_id=staged.manifest.backup_id,
            destination=final_path,
            receipt=receipt,
            manifest=staged.manifest,
        )

    def verify_backup(
        self,
        artifact: Path | str,
        *,
        expected_backup_id: str | None = None,
        receipt_sink: Path | str | None = None,
    ) -> VerificationResult:
        path = Path(artifact).expanduser()
        if not path.is_absolute():
            raise BackupError("backup verification path must be absolute")
        receipt = self._package.verify(
            path, expected_backup_id=expected_backup_id
        )
        self._persist_receipt(receipt, receipt_sink)
        return VerificationResult(receipt)

    @staticmethod
    def _report_progress(
        progress: BackupProgress,
        callback: Callable[[BackupProgress], object] | None,
    ) -> None:
        if callback is not None:
            callback(progress)

    @staticmethod
    def _persist_receipt(
        receipt: BackupReceipt,
        sink: Path | str | None,
    ) -> None:
        if sink is None:
            return
        path = Path(sink).expanduser()
        if not path.is_absolute() or not path.name or path.name in {".", ".."}:
            raise BackupError("backup receipt sink must be a file path")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError as exc:
            raise BackupDestinationExists(
                f"backup receipt sink already exists: {path.name}"
            ) from exc
        except OSError as exc:
            raise BackupArchiveInvalid("backup receipt sink cannot be created") from exc
        try:
            bytes_written = 0
            data = receipt.canonical_bytes()
            while bytes_written < len(data):
                bytes_written += os.write(descriptor, data[bytes_written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        parent = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def _cleanup_owned(self, staging_path: Path) -> None:
        try:
            staging_path.unlink()
            parent_fd = os.open(
                staging_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except FileNotFoundError:
            return
        except Exception as exc:
            raise BackupUncertainPublication(
                f"owned backup staging cleanup failed; exact path preserved: {staging_path.name}"
            ) from exc
