"""Private descriptor-relative attachment filesystem mechanics."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass

from uuid6 import uuid7

from bots5.infrastructure.data_root_authority import (
    DataRootAuthority,
    _RESOLVE_WALK,
    _check_regular,
    _identity,
    _open_component,
)


_TEST_FAULT_HOOK = None


def _fault(point: str) -> None:
    hook = _TEST_FAULT_HOOK
    if hook is not None:
        hook(point)


class AttachmentIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapturedAttachment:
    digest: bytes
    byte_size: int
    operation_id: str
    filename: str
    text: str | None
    representation_id: bytes | None
    ineligibility_reason: str | None


def _component(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 255
        or value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise AttachmentIntegrityError("invalid internal attachment component")
    return value


def _uuid7(value: str) -> str:
    _component(value)
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise AttachmentIntegrityError("invalid attachment operation identity") from exc
    if parsed.version != 7 or str(parsed) != value:
        raise AttachmentIntegrityError("invalid attachment operation identity")
    return value


def _digest(value: bytes) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise AttachmentIntegrityError("attachment digest must be exactly 32 bytes")
    return value


def digest_from_text(value: str) -> bytes:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AttachmentIntegrityError(
            "attachment digest text is not canonical lowercase SHA-256"
        )
    return bytes.fromhex(value)


def digest_to_text(value: bytes) -> str:
    return _digest(value).hex()


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    library = __import__(
        "bots5.infrastructure.rooted_sqlite_vfs", fromlist=["native_library"]
    ).native_library()
    library.bots5_renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    library.bots5_renameat2.restype = ctypes.c_int
    if library.bots5_renameat2(
        src_fd, _component(src).encode(), dst_fd, _component(dst).encode(), 1
    ) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(dst)
        raise OSError(error, os.strerror(error))


def _rename_exchange(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    library = __import__(
        "bots5.infrastructure.rooted_sqlite_vfs", fromlist=["native_library"]
    ).native_library()
    library.bots5_renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    library.bots5_renameat2.restype = ctypes.c_int
    if library.bots5_renameat2(
        src_fd, _component(src).encode(), dst_fd, _component(dst).encode(), 2
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _safe_filename(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 255
        or "\x00" in value
        or "/" in value
        or "\\" in value
    ):
        raise AttachmentIntegrityError("attachment filename is invalid")
    return value


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise AttachmentIntegrityError("attachment write was incomplete")
        offset += written


_CONSTRUCTION_KEY = object()


class _AttachmentFS:
    """Filesystem-only helper. It owns no lifecycle decision or database."""

    __slots__ = (
        "_authority",
        "_objects_fd",
        "_staging_fd",
        "_captures_fd",
        "_gc_fd",
        "_mount_id",
        "_closed",
    )

    def __init__(self, authority: DataRootAuthority, key: object):
        if key is not _CONSTRUCTION_KEY:
            raise TypeError("attachment filesystem is authority-private")
        authority.assert_live()
        self._authority = authority
        self._objects_fd = authority._directory_fd("attachments/objects")
        self._staging_fd = authority._directory_fd("attachments/staging")
        self._captures_fd = authority._directory_fd("attachments/captures")
        self._gc_fd = authority._directory_fd("attachments/gc")
        self._mount_id = authority._root_identity.mount_id
        self._closed = False

    def _live(self) -> None:
        if self._closed:
            raise AttachmentIntegrityError("attachment filesystem is closed")
        authority = self._authority
        if authority is None:
            raise AttachmentIntegrityError("attachment filesystem is closed")
        authority.assert_live()
        if (
            self._objects_fd is None
            or self._staging_fd is None
            or self._captures_fd is None
            or self._gc_fd is None
            or self._mount_id is None
        ):
            raise AttachmentIntegrityError("attachment filesystem capability is invalid")

    def close(self) -> None:
        if self._closed:
            return
        # Invalidate the bearer state before DataRootAuthority begins any
        # physical close.  Retained helpers can therefore never act merely
        # because the kernel later recycles one of these descriptor numbers.
        self._closed = True
        self._objects_fd = None
        self._staging_fd = None
        self._captures_fd = None
        self._gc_fd = None
        self._mount_id = None
        self._authority = None

    def _open_source(self, source: object) -> tuple[int, str]:
        self._live()
        raw = os.fspath(source)
        if type(raw) is not str or not os.path.isabs(raw) or "\x00" in raw:
            raise AttachmentIntegrityError("attachment source must be an absolute path")
        components: list[str] = []
        for value in raw.split("/"):
            if value in {"", "."}:
                continue
            if value == "..":
                raise AttachmentIntegrityError("attachment source traversal is forbidden")
            _component(value)
            components.append(value)
        if not components:
            raise AttachmentIntegrityError("attachment source is not a file")
        slash = next(
            claim.fd
            for claim in self._authority._ancestor_claims
            if claim.label == "ancestor:/"
        )
        assert slash is not None
        parent = slash
        opened: list[int] = []
        try:
            for component in components[:-1]:
                child = _open_component(
                    parent,
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    resolve=_RESOLVE_WALK,
                )
                opened.append(child)
                parent = child
            fd = _open_component(
                parent,
                components[-1],
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                resolve=_RESOLVE_WALK,
            )
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                raise AttachmentIntegrityError(
                    "attachment source must be a regular file"
                )
            return fd, components[-1]
        except OSError as exc:
            raise AttachmentIntegrityError(
                "attachment source cannot be opened safely"
            ) from exc
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def capture(
        self, source: object, *, filename: str | None = None
    ) -> CapturedAttachment:
        self._live()
        source_fd, source_name = self._open_source(source)
        operation_id = str(uuid7())
        capture_fd = -1
        try:
            before = os.fstat(source_fd)
            capture_fd = os.open(
                operation_id,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._captures_fd,
            )
            os.fchmod(capture_fd, 0o600)
            digest = hashlib.sha256()
            raw = bytearray()
            byte_count = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                _write_all(capture_fd, chunk)
                raw.extend(chunk)
                byte_count += len(chunk)
            os.fsync(capture_fd)
            _fault("after-capture-file-fsync")
            after = os.fstat(source_fd)
            stable = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if not stable or byte_count != after.st_size:
                raise AttachmentIntegrityError(
                    "attachment source changed during capture"
                )
        except BaseException:
            if capture_fd >= 0:
                os.close(capture_fd)
                capture_fd = -1
            try:
                os.unlink(operation_id, dir_fd=self._captures_fd)
                os.fsync(self._captures_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            if capture_fd >= 0:
                os.close(capture_fd)
            os.close(source_fd)
        try:
            text = bytes(raw).decode("utf-8", errors="strict")
            if "\x00" in text:
                text = None
                reason = "contains_nul"
            else:
                reason = None
        except UnicodeDecodeError:
            text = None
            reason = "invalid_utf8"
        value = digest.digest()
        return CapturedAttachment(
            digest=value,
            byte_size=byte_count,
            operation_id=operation_id,
            filename=_safe_filename(filename or source_name),
            text=text,
            representation_id=value if text is not None else None,
            ineligibility_reason=reason,
        )

    def _open_owned(self, directory_fd: int, leaf: str) -> int:
        self._live()
        fd = -1
        try:
            fd = _open_component(
                directory_fd,
                _component(leaf),
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            _check_regular(fd, mount_id=self._mount_id)
            return fd
        except BaseException as exc:
            if fd >= 0:
                os.close(fd)
            raise AttachmentIntegrityError(
                "attachment artifact cannot be opened safely"
            ) from exc

    def _unlink_owned(self, directory_fd: int, leaf: str) -> None:
        self._live()
        leaf = _component(leaf)
        fd = self._open_owned(directory_fd, leaf)
        try:
            opened = os.fstat(fd)
            named = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise AttachmentIntegrityError(
                    "attachment artifact changed before deletion"
                )
            os.unlink(leaf, dir_fd=directory_fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)

    def _read_verified(
        self,
        directory_fd: int,
        leaf: str,
        digest: bytes,
        byte_size: int,
    ) -> bytes:
        self._live()
        fd = self._open_owned(directory_fd, leaf)
        try:
            before = _identity(fd)
            value = bytearray()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                value.extend(chunk)
            after = _identity(fd)
            if before != after:
                raise AttachmentIntegrityError(
                    "attachment artifact changed while read"
                )
        finally:
            os.close(fd)
        result = bytes(value)
        if (
            len(result) != byte_size
            or hashlib.sha256(result).digest() != _digest(digest)
        ):
            raise AttachmentIntegrityError(
                "attachment payload does not match durable identity"
            )
        return result

    def _present(self, directory_fd: int, leaf: str) -> bool:
        self._live()
        try:
            os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def capture_present(self, operation_id: str) -> bool:
        self._live()
        return self._present(self._captures_fd, _uuid7(operation_id))

    def stage_present(self, operation_id: str) -> bool:
        self._live()
        return self._present(self._staging_fd, _uuid7(operation_id))

    def object_present(self, digest: bytes) -> bool:
        self._live()
        return self._present(self._objects_fd, digest_to_text(digest))

    def verify_capture(
        self, operation_id: str, digest: bytes, byte_size: int
    ) -> bytes:
        self._live()
        return self._read_verified(
            self._captures_fd, _uuid7(operation_id), digest, byte_size
        )

    def verify_stage(
        self, operation_id: str, digest: bytes, byte_size: int
    ) -> bytes:
        self._live()
        return self._read_verified(
            self._staging_fd, _uuid7(operation_id), digest, byte_size
        )

    def read_verified(self, digest: bytes, *, expected_size: int) -> bytes:
        self._live()
        return self._read_verified(
            self._objects_fd, digest_to_text(digest), digest, expected_size
        )

    def discard_capture(self, operation_id: str) -> None:
        self._live()
        self._unlink_owned(self._captures_fd, _uuid7(operation_id))

    def capture_to_stage(self, operation_id: str) -> None:
        self._live()
        operation_id = _uuid7(operation_id)
        _fault("before-capture-stage-rename")
        _rename_noreplace(
            self._captures_fd, operation_id, self._staging_fd, operation_id
        )
        _fault("after-capture-stage-rename")
        os.fsync(self._captures_fd)
        _fault("after-captures-directory-fsync")
        os.fsync(self._staging_fd)
        _fault("after-staging-directory-fsync")

    def stage_to_object(self, operation_id: str, digest: bytes) -> None:
        self._live()
        operation_id = _uuid7(operation_id)
        _fault("before-stage-object-rename")
        _rename_noreplace(
            self._staging_fd,
            operation_id,
            self._objects_fd,
            digest_to_text(digest),
        )
        _fault("after-stage-object-rename")
        os.fsync(self._staging_fd)
        _fault("after-stage-object-staging-fsync")
        os.fsync(self._objects_fd)
        _fault("after-objects-directory-fsync")

    def discard_stage(self, operation_id: str) -> None:
        self._live()
        self._unlink_owned(self._staging_fd, _uuid7(operation_id))

    @staticmethod
    def tombstone_bytes(gc_id: str, digest: bytes) -> bytes:
        parsed = uuid.UUID(_uuid7(gc_id))
        return b"BOTS5GC1\0" + parsed.bytes + _digest(digest)

    @staticmethod
    def tombstone_temp(gc_id: str) -> str:
        return _component("." + _uuid7(gc_id) + ".tmp")

    def create_tombstone(self, gc_id: str, digest: bytes) -> None:
        self._live()
        final = _uuid7(gc_id)
        temporary = self.tombstone_temp(gc_id)
        payload = self.tombstone_bytes(gc_id, digest)
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._gc_fd,
            )
            _fault("after-gc-tombstone-temp-create")
        except FileExistsError:
            self._remove_gc_temp(gc_id, digest)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._gc_fd,
            )
            _fault("after-gc-tombstone-temp-create")
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, payload)
            _fault("after-gc-tombstone-write")
            os.fsync(fd)
            _fault("after-gc-tombstone-file-fsync")
        finally:
            os.close(fd)
        _fault("before-gc-tombstone-rename")
        _rename_noreplace(self._gc_fd, temporary, self._gc_fd, final)
        _fault("after-gc-tombstone-rename")
        os.fsync(self._gc_fd)
        _fault("after-gc-tombstone-directory-fsync")
        if not self.tombstone_present(gc_id, digest):
            raise AttachmentIntegrityError("published GC tombstone is invalid")

    def _remove_gc_temp(self, gc_id: str, digest: bytes) -> None:
        self._live()
        leaf = self.tombstone_temp(gc_id)
        expected = self.tombstone_bytes(gc_id, digest)
        fd = self._open_owned(self._gc_fd, leaf)
        try:
            before = _identity(fd)
            observed = os.read(fd, len(expected) + 1)
            after = _identity(fd)
        finally:
            os.close(fd)
        if before != after or observed != expected[: len(observed)] or len(observed) > len(expected):
            raise AttachmentIntegrityError(
                "GC tombstone temporary evidence is not attributable"
            )
        self._unlink_owned(self._gc_fd, leaf)

    def tombstone_present(self, gc_id: str, digest: bytes) -> bool:
        self._live()
        gc_id = _uuid7(gc_id)
        if not self._present(self._gc_fd, gc_id):
            return False
        fd = self._open_owned(self._gc_fd, gc_id)
        try:
            before = _identity(fd)
            payload = os.read(fd, 58)
            after = _identity(fd)
        finally:
            os.close(fd)
        return before == after and payload == self.tombstone_bytes(gc_id, digest)

    def canonical_tombstone_present(self, gc_id: str, digest: bytes) -> bool:
        self._live()
        leaf = digest_to_text(digest)
        if not self._present(self._objects_fd, leaf):
            return False
        fd = self._open_owned(self._objects_fd, leaf)
        try:
            payload = os.read(fd, 58)
        finally:
            os.close(fd)
        return payload == self.tombstone_bytes(gc_id, digest)

    def gc_payload_present(self, gc_id: str) -> bool:
        self._live()
        return self._present(self._gc_fd, _uuid7(gc_id))

    def verify_gc_payload(
        self, gc_id: str, digest: bytes, byte_size: int
    ) -> bytes:
        self._live()
        return self._read_verified(
            self._gc_fd, _uuid7(gc_id), digest, byte_size
        )

    def exchange_object_to_gc(self, digest: bytes, gc_id: str) -> None:
        self._live()
        _fault("before-gc-exchange")
        _rename_exchange(
            self._objects_fd,
            digest_to_text(digest),
            self._gc_fd,
            _uuid7(gc_id),
        )
        _fault("after-gc-exchange")
        os.fsync(self._objects_fd)
        _fault("after-gc-objects-directory-fsync")
        os.fsync(self._gc_fd)
        _fault("after-gc-directory-fsync")

    def delete_gc_payload(self, gc_id: str) -> None:
        self._live()
        _fault("before-gc-payload-unlink")
        self._unlink_owned(self._gc_fd, _uuid7(gc_id))
        _fault("after-gc-payload-unlink-fsync")

    def delete_canonical_tombstone(self, digest: bytes) -> None:
        self._live()
        _fault("before-gc-tombstone-unlink")
        self._unlink_owned(self._objects_fd, digest_to_text(digest))
        _fault("after-gc-tombstone-unlink-fsync")

    def inventory(self, area: str) -> tuple[str, ...]:
        self._live()
        descriptor = {
            "objects": self._objects_fd,
            "staging": self._staging_fd,
            "captures": self._captures_fd,
            "gc": self._gc_fd,
        }.get(area)
        if descriptor is None:
            raise AttachmentIntegrityError("unknown attachment inventory")
        return tuple(sorted(os.listdir(descriptor)))


def _open_attachment_fs(authority: DataRootAuthority) -> _AttachmentFS:
    return _AttachmentFS(authority, _CONSTRUCTION_KEY)
