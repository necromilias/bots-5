"""Private descriptor-relative attachment filesystem mechanics."""

from __future__ import annotations

import ctypes
import codecs
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


class AttachmentCleanupUncertain(AttachmentIntegrityError):
    """A capture cleanup mutation lacks its mandatory namespace barrier."""


@dataclass(frozen=True, slots=True)
class CapturedAttachment:
    digest: bytes
    byte_size: int
    operation_id: str
    filename: str
    text: str | None
    representation_id: bytes | None
    ineligibility_reason: str | None


@dataclass(frozen=True, slots=True)
class AttachmentTextClassification:
    """Pure semantic classification of canonical attachment bytes."""

    text: str | None
    representation_id: bytes | None
    ineligibility_reason: str | None
    decode_error: UnicodeDecodeError | None = None


@dataclass(frozen=True, slots=True)
class VerifiedAttachmentTextFacts:
    """Streaming classification facts for a verified canonical object.

    Unlike ``AttachmentTextClassification``, this intentionally never carries
    decoded content.  Recovery only needs the durable representation identity
    and ineligibility disposition to create its missing attachment row.
    """

    representation_id: bytes | None
    ineligibility_reason: str | None


def classify_attachment_text(raw: bytes) -> AttachmentTextClassification:
    """Derive the one Phase 6 text-representation meaning of canonical bytes."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return AttachmentTextClassification(
            text=None,
            representation_id=None,
            ineligibility_reason="invalid_utf8",
            decode_error=exc,
        )
    if "\x00" in text:
        return AttachmentTextClassification(
            text=None,
            representation_id=None,
            ineligibility_reason="contains_nul",
        )
    return AttachmentTextClassification(
        text=text,
        representation_id=hashlib.sha256(raw).digest(),
        ineligibility_reason=None,
    )


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

    def _track_fd(self, label: str, fd: int) -> None:
        self._authority._claim_scoped_fd(f"attachment:{label}", fd)

    def _close_fd(self, fd: int) -> None:
        claim = next(
            (
                item
                for item in self._authority._claims
                if item.fd == fd
                and item.status == "HELD"
                and item.label.startswith("scoped:attachment:")
            ),
            None,
        )
        if claim is None:
            raise AttachmentIntegrityError("attachment descriptor has no owner")
        try:
            self._authority._release_scoped_fd(claim)
        except BaseException as exc:
            self._authority.poison("attachment descriptor close outcome is uncertain")
            raise AttachmentIntegrityError(
                "attachment descriptor close outcome is uncertain"
            ) from exc

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
                self._track_fd("source-directory", child)
                opened.append(child)
                parent = child
            fd = _open_component(
                parent,
                components[-1],
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                resolve=_RESOLVE_WALK,
            )
            self._track_fd("source-file", fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self._close_fd(fd)
                raise AttachmentIntegrityError(
                    "attachment source must be a regular file"
                )
            return fd, components[-1]
        except OSError as exc:
            raise AttachmentIntegrityError(
                "attachment source cannot be opened safely"
            ) from exc
        finally:
            close_error: BaseException | None = None
            for descriptor in reversed(opened):
                try:
                    self._close_fd(descriptor)
                except BaseException as exc:
                    if close_error is None:
                        close_error = exc
            if close_error is not None:
                raise close_error

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
            self._track_fd("capture", capture_fd)
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
            os.fsync(self._captures_fd)
            _fault("after-capture-directory-fsync")
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
                closing_fd = capture_fd
                capture_fd = -1
                self._close_fd(closing_fd)
            try:
                os.unlink(operation_id, dir_fd=self._captures_fd)
                os.fsync(self._captures_fd)
            except FileNotFoundError:
                pass
            except BaseException as exc:
                raise AttachmentCleanupUncertain(
                    "attachment capture cleanup durability is uncertain"
                ) from exc
            raise
        finally:
            close_error: BaseException | None = None
            if capture_fd >= 0:
                closing_fd = capture_fd
                capture_fd = -1
                try:
                    self._close_fd(closing_fd)
                except BaseException as exc:
                    close_error = exc
            try:
                self._close_fd(source_fd)
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
            if close_error is not None:
                raise close_error
        classification = classify_attachment_text(bytes(raw))
        value = digest.digest()
        return CapturedAttachment(
            digest=value,
            byte_size=byte_count,
            operation_id=operation_id,
            filename=_safe_filename(filename or source_name),
            text=classification.text,
            representation_id=classification.representation_id,
            ineligibility_reason=classification.ineligibility_reason,
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
            self._track_fd("artifact-proof", fd)
            _check_regular(fd, mount_id=self._mount_id)
            return fd
        except BaseException as exc:
            if fd >= 0:
                self._close_fd(fd)
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
            self._close_fd(fd)
        os.fsync(directory_fd)

    def _read_verified(
        self,
        directory_fd: int,
        leaf: str,
        digest: bytes,
        byte_size: int,
        *,
        materialize: bool = True,
        retain_identity: bool = False,
    ) -> bytes | None | tuple[bytes | None, object]:
        self._live()
        fd = self._open_owned(directory_fd, leaf)
        try:
            before = _identity(fd)
            value = bytearray() if materialize else None
            actual = hashlib.sha256()
            count = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                actual.update(chunk)
                count += len(chunk)
                if value is not None:
                    value.extend(chunk)
            after = _identity(fd)
            if before != after:
                raise AttachmentIntegrityError(
                    "attachment artifact changed while read"
                )
        finally:
            self._close_fd(fd)
        if (
            count != byte_size
            or actual.digest() != _digest(digest)
        ):
            raise AttachmentIntegrityError(
                "attachment payload does not match durable identity"
            )
        result = None if value is None else bytes(value)
        return (result, before) if retain_identity else result

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
        leaf = digest_to_text(digest)
        try:
            return self._read_verified(
                self._objects_fd, leaf, digest, expected_size
            )
        except AttachmentIntegrityError:
            # ``read_verified`` is the canonical-object boundary: its digest
            # and size are durable authority-owned facts supplied by the
            # store, not untrusted request validation.  Once those bytes are
            # absent, unsafe, or different, the discovering grant may unwind
            # but must never be reused for forward work.
            self._authority.poison(
                "required attachment payload integrity failure"
            )
            raise

    def verify_object(self, digest: bytes, *, expected_size: int) -> None:
        """Check a canonical blob with bounded reads without copying it to RAM."""
        self._live()
        leaf = digest_to_text(digest)
        try:
            self._read_verified(
                self._objects_fd, leaf, digest, expected_size, materialize=False,
            )
        except AttachmentIntegrityError:
            self._authority.poison("required attachment payload integrity failure")
            raise

    def classify_verified_object(
        self, digest: bytes, *, expected_size: int
    ) -> VerifiedAttachmentTextFacts:
        """Hash and classify one canonical object through one owned descriptor.

        This deliberately does not call ``read_verified``: startup reconciliation
        needs the exact Phase 6 classification for a ready CAS object which has
        not yet acquired an ``attachments`` backing row.  The bytes are streamed
        through the same descriptor-relative identity and hash checks as every
        other canonical-object proof.  Recovery needs no decoded content, so
        every decoded chunk is discarded after UTF-8/NUL classification.
        """
        self._live()
        digest = _digest(digest)
        if type(expected_size) is not int or expected_size < 0:
            raise AttachmentIntegrityError("attachment byte size is invalid")
        fd = self._open_owned(self._objects_fd, digest_to_text(digest))
        try:
            before = _identity(fd)
            actual = hashlib.sha256()
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            decode_error: UnicodeDecodeError | None = None
            saw_nul = False
            count = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                actual.update(chunk)
                count += len(chunk)
                if decode_error is None:
                    try:
                        text = decoder.decode(chunk, final=False)
                    except UnicodeDecodeError as exc:
                        decode_error = exc
                    else:
                        saw_nul = saw_nul or "\x00" in text
            if decode_error is None:
                try:
                    text = decoder.decode(b"", final=True)
                except UnicodeDecodeError as exc:
                    decode_error = exc
                else:
                    saw_nul = saw_nul or "\x00" in text
            after = _identity(fd)
            if before != after:
                raise AttachmentIntegrityError("attachment artifact changed while read")
        finally:
            self._close_fd(fd)
        if count != expected_size or actual.digest() != digest:
            self._authority.poison("required attachment payload integrity failure")
            raise AttachmentIntegrityError("attachment payload does not match durable identity")
        if decode_error is not None:
            return VerifiedAttachmentTextFacts(
                representation_id=None,
                ineligibility_reason="invalid_utf8",
            )
        if saw_nul:
            return VerifiedAttachmentTextFacts(
                representation_id=None,
                ineligibility_reason="contains_nul",
            )
        return VerifiedAttachmentTextFacts(
            representation_id=actual.digest(),
            ineligibility_reason=None,
        )

    def verified_object_identity(self, digest: bytes, *, expected_size: int) -> object:
        """Hash-verify a canonical object and retain its stable inode identity."""
        try:
            _value, identity = self._read_verified(
                self._objects_fd, digest_to_text(digest), digest, expected_size,
                materialize=False, retain_identity=True,
            )
            return identity
        except AttachmentIntegrityError:
            self._authority.poison("required attachment payload integrity failure")
            raise

    def object_identity(self, digest: bytes) -> object:
        self._live()
        fd = self._open_owned(self._objects_fd, digest_to_text(digest))
        try:
            return _identity(fd)
        finally:
            self._close_fd(fd)

    def capture_verified_snapshot(
        self, snapshot, digest: bytes, byte_size: int, *, filename: str = "imported-payload",
    ) -> CapturedAttachment:
        """Copy one sealed anonymous snapshot directly into the capture namespace."""
        self._live()
        digest = _digest(digest)
        if type(byte_size) is not int or byte_size < 0:
            raise AttachmentIntegrityError("attachment byte size is invalid")
        operation_id = str(uuid7())
        capture_fd = -1
        try:
            snapshot.seek(0)
            capture_fd = os.open(
                operation_id,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._captures_fd,
            )
            self._track_fd("capture", capture_fd)
            os.fchmod(capture_fd, 0o600)
            actual = hashlib.sha256()
            remaining = byte_size
            while remaining:
                block = snapshot.read(min(1024 * 1024, remaining))
                if not block:
                    raise AttachmentIntegrityError("sealed attachment snapshot is truncated")
                actual.update(block)
                _write_all(capture_fd, block)
                remaining -= len(block)
            if snapshot.read(1):
                raise AttachmentIntegrityError("sealed attachment snapshot length changed")
            if actual.digest() != digest:
                raise AttachmentIntegrityError("sealed attachment snapshot changed")
            os.fsync(capture_fd)
            _fault("after-capture-file-fsync")
            os.fsync(self._captures_fd)
            _fault("after-capture-directory-fsync")
        except BaseException:
            if capture_fd >= 0:
                closing_fd = capture_fd
                capture_fd = -1
                self._close_fd(closing_fd)
            try:
                os.unlink(operation_id, dir_fd=self._captures_fd)
                os.fsync(self._captures_fd)
            except FileNotFoundError:
                pass
            except BaseException as exc:
                raise AttachmentCleanupUncertain(
                    "attachment capture cleanup durability is uncertain"
                ) from exc
            raise
        finally:
            if capture_fd >= 0:
                self._close_fd(capture_fd)
        return CapturedAttachment(
            digest=digest,
            byte_size=byte_size,
            operation_id=operation_id,
            filename=_safe_filename(filename),
            text=None,
            representation_id=None,
            ineligibility_reason=None,
        )

    def discard_capture(self, operation_id: str) -> None:
        self._live()
        self._unlink_owned(self._captures_fd, _uuid7(operation_id))

    def capture_to_stage(
        self, operation_id: str, digest: bytes, byte_size: int
    ) -> None:
        self._live()
        operation_id = _uuid7(operation_id)
        captures = set(self.inventory("captures"))
        staging = set(self.inventory("staging"))
        source = operation_id in captures
        destination = operation_id in staging
        if source:
            self.verify_capture(operation_id, digest, byte_size)
        if destination:
            self.verify_stage(operation_id, digest, byte_size)
        if source and not destination:
            _fault("before-capture-stage-rename")
            _rename_noreplace(
                self._captures_fd, operation_id, self._staging_fd, operation_id
            )
            _fault("after-capture-stage-rename")
            # Destination first: an interrupted move may leave two names, but
            # never a durable staging row without attributable bytes.
            os.fsync(self._staging_fd)
            _fault("after-staging-directory-fsync")
            os.fsync(self._captures_fd)
            _fault("after-captures-directory-fsync")
        elif source and destination:
            os.fsync(self._staging_fd)
            _fault("after-staging-directory-fsync")
            self.discard_capture(operation_id)
            _fault("after-captures-directory-fsync")
        elif not destination:
            raise AttachmentIntegrityError(
                "durable staging row has no capture or stage payload"
            )
        self.verify_stage(operation_id, digest, byte_size)
        if operation_id in self.inventory("captures"):
            raise AttachmentIntegrityError("capture-to-stage source remains")
        if operation_id not in self.inventory("staging"):
            raise AttachmentIntegrityError("capture-to-stage destination is absent")

    def stage_to_object(
        self, operation_id: str, digest: bytes, byte_size: int
    ) -> None:
        self._live()
        operation_id = _uuid7(operation_id)
        object_leaf = digest_to_text(digest)
        staging = set(self.inventory("staging"))
        objects = set(self.inventory("objects"))
        source = operation_id in staging
        destination = object_leaf in objects
        if source:
            self.verify_stage(operation_id, digest, byte_size)
        if destination:
            self.read_verified(digest, expected_size=byte_size)
        if source and not destination:
            _fault("before-stage-object-rename")
            _rename_noreplace(
                self._staging_fd,
                operation_id,
                self._objects_fd,
                object_leaf,
            )
            _fault("after-stage-object-rename")
            os.fsync(self._objects_fd)
            _fault("after-objects-directory-fsync")
            os.fsync(self._staging_fd)
            _fault("after-stage-object-staging-fsync")
        elif source and destination:
            os.fsync(self._objects_fd)
            _fault("after-objects-directory-fsync")
            self.discard_stage(operation_id)
            _fault("after-stage-object-staging-fsync")
        elif not destination:
            raise AttachmentIntegrityError(
                "durable staging row has no stage or canonical payload"
            )
        self.read_verified(digest, expected_size=byte_size)
        if operation_id in self.inventory("staging"):
            raise AttachmentIntegrityError("stage-to-object source remains")
        if object_leaf not in self.inventory("objects"):
            raise AttachmentIntegrityError("stage-to-object destination is absent")

    def prove_staging_publication(
        self, operation_id: str, digest: bytes, byte_size: int
    ) -> None:
        self._live()
        operation_id = _uuid7(operation_id)
        os.fsync(self._captures_fd)
        os.fsync(self._staging_fd)
        os.fsync(self._objects_fd)
        if operation_id in self.inventory("captures"):
            raise AttachmentIntegrityError("publication capture source remains")
        if operation_id in self.inventory("staging"):
            raise AttachmentIntegrityError("publication stage source remains")
        if digest_to_text(digest) not in self.inventory("objects"):
            raise AttachmentIntegrityError("publication canonical destination is absent")
        self.read_verified(digest, expected_size=byte_size)

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
            self._track_fd("gc-tombstone-temp", fd)
            _fault("after-gc-tombstone-temp-create")
        except FileExistsError:
            self._remove_gc_temp(gc_id, digest)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._gc_fd,
            )
            self._track_fd("gc-tombstone-temp", fd)
            _fault("after-gc-tombstone-temp-create")
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, payload)
            _fault("after-gc-tombstone-write")
            os.fsync(fd)
            _fault("after-gc-tombstone-file-fsync")
        finally:
            self._close_fd(fd)
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
            self._close_fd(fd)
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
            self._close_fd(fd)
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
            self._close_fd(fd)
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
        os.fsync(self._gc_fd)
        _fault("after-gc-directory-fsync")
        os.fsync(self._objects_fd)
        _fault("after-gc-objects-directory-fsync")

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

    def delete_canonical_payload(self, digest: bytes) -> None:
        self._live()
        self._unlink_owned(self._objects_fd, digest_to_text(digest))

    def delete_gc_tombstone(self, gc_id: str) -> None:
        self._live()
        self._unlink_owned(self._gc_fd, _uuid7(gc_id))

    def sync_namespace(self, area: str) -> None:
        self._live()
        descriptor = {
            "objects": self._objects_fd,
            "staging": self._staging_fd,
            "captures": self._captures_fd,
            "gc": self._gc_fd,
        }.get(area)
        if descriptor is None:
            raise AttachmentIntegrityError("unknown attachment namespace")
        os.fsync(descriptor)

    def sync_all_namespaces(self) -> None:
        self._live()
        for area in ("captures", "staging", "objects", "gc"):
            self.sync_namespace(area)

    def gc_content_state(
        self, digest: bytes, byte_size: int, gc_id: str
    ) -> tuple[str, str]:
        self._live()
        object_leaf = digest_to_text(digest)
        gc_leaf = _uuid7(gc_id)
        objects = set(self.inventory("objects"))
        gc_names = set(self.inventory("gc"))

        def classify_object() -> str:
            if object_leaf not in objects:
                return "absent"
            if self.canonical_tombstone_present(gc_id, digest):
                return "tombstone"
            self.read_verified(digest, expected_size=byte_size)
            return "payload"

        def classify_gc() -> str:
            if gc_leaf not in gc_names:
                return "absent"
            if self.tombstone_present(gc_id, digest):
                return "tombstone"
            self.verify_gc_payload(gc_id, digest, byte_size)
            return "payload"

        return classify_object(), classify_gc()

    def inventory(self, area: str) -> tuple[str, ...]:
        if self._closed or self._authority is None:
            self._live()
        authority = self._authority
        assert authority is not None
        with authority.operation():
            self._live()
            descriptor = {
                "objects": self._objects_fd,
                "staging": self._staging_fd,
                "captures": self._captures_fd,
                "gc": self._gc_fd,
            }.get(area)
            if descriptor is None:
                raise AttachmentIntegrityError("unknown attachment inventory")
            relative = {
                "objects": "attachments/objects",
                "staging": "attachments/staging",
                "captures": "attachments/captures",
                "gc": "attachments/gc",
            }[area]
            try:
                return self._authority.fresh_directory_inventory(relative)
            except BaseException as exc:
                raise AttachmentIntegrityError(
                    "attachment inventory cannot be observed safely"
                ) from exc

    def namespace_capacity(self, area: str) -> tuple[int, int, int]:
        """Return rooted namespace device, available bytes, and allocation unit."""
        self._live()
        descriptor = {
            "objects": self._objects_fd,
            "staging": self._staging_fd,
            "captures": self._captures_fd,
            "gc": self._gc_fd,
        }.get(area)
        if descriptor is None:
            raise AttachmentIntegrityError("unknown attachment capacity namespace")
        try:
            status = os.fstatvfs(descriptor)
            identity = os.fstat(descriptor)
        except OSError as exc:
            raise AttachmentIntegrityError(
                "attachment namespace capacity cannot be observed safely"
            ) from exc
        unit = status.f_frsize or status.f_bsize
        if unit <= 0:
            raise AttachmentIntegrityError("attachment namespace allocation unit is invalid")
        return identity.st_dev, status.f_bavail * unit, unit


def _open_attachment_fs(authority: DataRootAuthority) -> _AttachmentFS:
    return _AttachmentFS(authority, _CONSTRUCTION_KEY)
