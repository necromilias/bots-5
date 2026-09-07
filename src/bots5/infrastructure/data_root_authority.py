"""Kernel-anchored aggregate authority for one B.O.T.S. data root."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import socket
import stat
import threading
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

from bots5.core.errors import AuthorityError
from bots5.infrastructure.rooted_sqlite_vfs import RootedSQLiteVfs, native_library


_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_RESOLVE_SAFE = (
    _RESOLVE_BENEATH | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_XDEV
)
_RESOLVE_WALK = _RESOLVE_BENEATH | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_MAIN_LEAF = "state.sqlite3"
_FIXED_DESCENDANTS = (
    "database",
    "attachments",
    "attachments/objects",
    "attachments/staging",
    "attachments/captures",
    "attachments/gc",
    "database/migration",
    "database/temp",
    "recovery",
)


class AuthorityState(str, Enum):
    ACQUIRING = "ACQUIRING"
    ACQUIRED = "ACQUIRED"
    MIGRATING = "MIGRATING"
    RECOVERING = "RECOVERING"
    READY = "READY"
    CLOSING = "CLOSING"
    RELEASING_LOGICAL = "RELEASING_LOGICAL"
    CLOSED = "CLOSED"
    FAILED_STARTUP = "FAILED_STARTUP"
    POISONED = "POISONED"
    FAILED_CLOSED = "FAILED_CLOSED"


@dataclass(frozen=True, slots=True)
class DataRootSpec:
    logical_root: str

    @classmethod
    def from_path(cls, value: Path | str) -> "DataRootSpec":
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
            raise AuthorityError("configured data root must be a nonempty absolute path")
        components: list[str] = []
        for component in raw.split("/"):
            if component in {"", "."}:
                continue
            if component == "..":
                raise AuthorityError("configured data root must not contain '..'")
            components.append(component)
        if not components:
            raise AuthorityError("filesystem root cannot be a B.O.T.S. data root")
        return cls("/" + "/".join(components))

    @property
    def components(self) -> tuple[str, ...]:
        return tuple(self.logical_root[1:].split("/"))


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device_major: int
    device_minor: int
    inode: int
    mount_id: int
    mode: int
    uid: int
    nlink: int
    size: int

    @property
    def key(self) -> tuple[int, int, int, int]:
        return self.device_major, self.device_minor, self.inode, self.mount_id


@dataclass(slots=True)
class _Claim:
    label: str
    fd: int | None
    status: str = "HELD"


_REGISTRY_LOCK = threading.RLock()
_LOGICAL_ROOTS: dict[str, weakref.ReferenceType["DataRootAuthority"]] = {}
_ROOT_IDENTITIES: dict[tuple[int, int, int, int], weakref.ReferenceType["DataRootAuthority"]] = {}
_DATABASE_IDENTITIES: dict[tuple[int, int, int, int], weakref.ReferenceType["DataRootAuthority"]] = {}
_AUTHORITIES: weakref.WeakSet["DataRootAuthority"] = weakref.WeakSet()


def _native() -> ctypes.CDLL:
    library = native_library()
    library.bots5_statx_fd.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_longlong),
    ]
    library.bots5_statx_fd.restype = ctypes.c_int
    library.bots5_open_component.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_ulonglong,
    ]
    library.bots5_open_component.restype = ctypes.c_int
    return library


def _identity(fd: int) -> FileIdentity:
    values = [ctypes.c_ulonglong() for _ in range(4)]
    mode, uid, nlink = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
    size = ctypes.c_longlong()
    if _native().bots5_statx_fd(
        fd,
        *(ctypes.byref(value) for value in values),
        ctypes.byref(mode),
        ctypes.byref(uid),
        ctypes.byref(nlink),
        ctypes.byref(size),
    ) != 0:
        raise AuthorityError("statx mount identity is unavailable")
    return FileIdentity(
        *(int(value.value) for value in values),
        int(mode.value),
        int(uid.value),
        int(nlink.value),
        int(size.value),
    )


def _open_component(
    parent_fd: int,
    component: str,
    flags: int,
    mode: int = 0,
    *,
    resolve: int = _RESOLVE_SAFE,
) -> int:
    fd = _native().bots5_open_component(
        parent_fd,
        component.encode("utf-8"),
        flags,
        mode,
        resolve,
    )
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), component)
    return fd


def _reject_acl(fd: int) -> None:
    try:
        value = os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno in {61, 95}:
            return
        raise AuthorityError("cannot validate data-root ACL") from exc
    if value:
        raise AuthorityError("data-root capabilities with access ACLs are unsupported")


def _check_directory(fd: int, *, mount_id: int | None = None) -> FileIdentity:
    value = _identity(fd)
    if not stat.S_ISDIR(value.mode):
        raise AuthorityError("data-root capability is not a directory")
    if value.uid != os.geteuid() or stat.S_IMODE(value.mode) != 0o700:
        raise AuthorityError("data-root capability has unsafe owner or permissions")
    if mount_id is not None and value.mount_id != mount_id:
        raise AuthorityError("data-root capability crosses a mount boundary")
    _reject_acl(fd)
    return value


def _check_regular(fd: int, *, mount_id: int) -> FileIdentity:
    value = _identity(fd)
    if (
        not stat.S_ISREG(value.mode)
        or value.uid != os.geteuid()
        or stat.S_IMODE(value.mode) != 0o600
        or value.nlink != 1
        or value.mount_id != mount_id
    ):
        raise AuthorityError("database capability has unsafe identity")
    _reject_acl(fd)
    return value


def _flock(fd: int, operation: int, message: str) -> None:
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
    except OSError as exc:
        raise AuthorityError(message) from exc


def _close_socket(value: socket.socket) -> None:
    value.close()


class DataRootAuthority:
    """Non-copyable, PID-bound aggregate root/database/attachment capability."""

    def __init__(self, root: DataRootSpec | Path | str):
        self.spec = root if isinstance(root, DataRootSpec) else DataRootSpec.from_path(root)
        self.root = self.spec.logical_root  # diagnostic only
        self._pid = os.getpid()
        self._state = AuthorityState.ACQUIRING
        self._condition = threading.Condition(threading.RLock())
        self._transition_gate = threading.Lock()
        self._operation_local = threading.local()
        self._active_operations = 0
        self._checked_out_connections = 0
        self._opening_store = False
        self._private_close_checkout = False
        self._store = None
        self._vfs: RootedSQLiteVfs | None = None
        self._vfs_claims: dict[str, _Claim] = {}
        self._vfs_generation = 0
        self._main_identity: FileIdentity | None = None
        self._claims: list[_Claim] = []
        self._ancestor_claims: list[_Claim] = []
        self._descendant_claims: dict[str, _Claim] = {}
        self._root_claim: _Claim | None = None
        self._main_claim: _Claim | None = None
        self._migration_claim: _Claim | None = None
        self._migration_identity: FileIdentity | None = None
        self._logical_socket: socket.socket | None = None
        self._logical_status = "UNBOUND"
        self._physical_release_complete = False
        self._closed_error: str | None = None
        self._close_owner: int | None = None
        self._close_result: tuple[type[BaseException], tuple[object, ...], str] | None = None
        _AUTHORITIES.add(self)

    def __copy__(self):
        raise TypeError("DataRootAuthority is not copyable")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("DataRootAuthority is not copyable")

    def __reduce__(self):
        raise TypeError("DataRootAuthority is not pickleable")

    @property
    def state(self) -> AuthorityState:
        return self._state

    @property
    def claim_inventory(self) -> tuple[tuple[str, str], ...]:
        return tuple((claim.label, claim.status) for claim in self._claims) + (
            ("logical-root", self._logical_status),
        )

    @property
    def acquired(self) -> bool:
        return self._state in {
            AuthorityState.ACQUIRED,
            AuthorityState.MIGRATING,
            AuthorityState.RECOVERING,
            AuthorityState.READY,
        }

    def _assert_pid(self) -> None:
        if os.getpid() != self._pid:
            raise AuthorityError("data root authority cannot be used after fork")

    def assert_live(self) -> None:
        self._assert_pid()
        admitted_during_close = (
            self._state == AuthorityState.CLOSING and self._operation_depth() > 0
        )
        if not self.acquired and not admitted_during_close:
            raise AuthorityError(f"data root authority is not live: {self._state.value}")
        if self._root_claim is None or self._root_claim.fd is None:
            raise AuthorityError("data root authority has no physical root claim")

    def _operation_depth(self) -> int:
        return int(getattr(self._operation_local, "depth", 0))

    @property
    def _root_capability(self) -> int:
        self.assert_live()
        assert self._root_claim is not None and self._root_claim.fd is not None
        return self._root_claim.fd

    @property
    def _database_dir_capability(self) -> int:
        return self._directory_fd("database")

    @property
    def _attachments_dir_capability(self) -> int:
        return self._directory_fd("attachments")

    def _directory_fd(self, relative: str) -> int:
        self.assert_live()
        claim = self._descendant_claims.get(relative)
        if claim is None or claim.fd is None or claim.status != "HELD":
            raise AuthorityError(f"unknown or released data-root descendant: {relative}")
        return claim.fd

    def _bind_logical(self) -> None:
        digest = hashlib.sha256(
            f"bots5-root-v2\0{os.geteuid()}\0{self.spec.logical_root}".encode("utf-8")
        ).hexdigest()
        claim = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        try:
            claim.bind(f"\0bots5-root-v2-{os.geteuid()}-{digest}")
        except OSError as exc:
            claim.close()
            raise AuthorityError(
                "mandatory abstract Unix logical-root claim is unavailable or already owned"
            ) from exc
        with _REGISTRY_LOCK:
            previous = _LOGICAL_ROOTS.get(self.spec.logical_root)
            if previous is not None and previous() is not None:
                claim.close()
                raise AuthorityError("configured logical data root is already owned")
            _LOGICAL_ROOTS[self.spec.logical_root] = weakref.ref(self)
        self._logical_socket = claim
        self._logical_status = "HELD"

    def _append_claim(self, label: str, fd: int, *, ancestor: bool = False) -> _Claim:
        claim = _Claim(label, fd)
        self._claims.append(claim)
        if ancestor:
            self._ancestor_claims.append(claim)
        return claim

    def _probe_required_primitives(self) -> None:
        """Exercise required data-filesystem primitives before any DB open."""
        claim = self._descendant_claims.get("database/temp")
        if claim is None or claim.fd is None:
            raise AuthorityError("data-root capability probe has no temporary directory")
        directory_fd = claim.fd
        if os.listdir(directory_fd):
            raise AuthorityError("database temporary directory contains unexplained state")
        token = uuid.uuid4().hex
        first = f".bots5-probe-{token}-a"
        second = f".bots5-probe-{token}-b"
        descriptors: list[int] = []
        try:
            for leaf, payload in ((first, b"A"), (second, b"B")):
                fd = os.open(
                    leaf,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                descriptors.append(fd)
                os.fchmod(fd, 0o600)
                _check_regular(fd, mount_id=self._root_identity.mount_id)
                if os.write(fd, payload) != 1:
                    raise AuthorityError("data-root capability probe write was short")
                os.fsync(fd)
            first_identity = _identity(descriptors[0])
            second_identity = _identity(descriptors[1])
            from bots5.infrastructure.attachments import (
                _rename_exchange,
                _rename_noreplace,
            )

            _rename_exchange(directory_fd, first, directory_fd, second)
            opened_first = _open_component(
                directory_fd, first, os.O_RDONLY | os.O_CLOEXEC
            )
            opened_second = _open_component(
                directory_fd, second, os.O_RDONLY | os.O_CLOEXEC
            )
            try:
                if (
                    _identity(opened_first).key != second_identity.key
                    or _identity(opened_second).key != first_identity.key
                ):
                    raise AuthorityError("RENAME_EXCHANGE identity proof failed")
            finally:
                os.close(opened_second)
                os.close(opened_first)
            _rename_exchange(directory_fd, first, directory_fd, second)
            try:
                _rename_noreplace(directory_fd, first, directory_fd, second)
            except FileExistsError:
                pass
            else:
                raise AuthorityError("RENAME_NOREPLACE collision proof failed")
            os.unlink(second, dir_fd=directory_fd)
            _rename_noreplace(directory_fd, first, directory_fd, second)
            os.fsync(directory_fd)
            os.unlink(second, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            while descriptors:
                os.close(descriptors.pop())

    def acquire(self) -> "DataRootAuthority":
        self._assert_pid()
        if self._state != AuthorityState.ACQUIRING:
            raise AuthorityError("data root authority may be acquired only once")
        self._bind_logical()
        try:
            slash = os.open("/", _DIRECTORY_FLAGS)
            self._append_claim("ancestor:/", slash, ancestor=True)
            _flock(slash, fcntl.LOCK_SH, "data-root hierarchy is already owned")
            parent_fd = slash
            parent_identity = _identity(slash)
            for index, component in enumerate(self.spec.components):
                final = index == len(self.spec.components) - 1
                created = False
                try:
                    child = _open_component(
                        parent_fd, component, _DIRECTORY_FLAGS, resolve=_RESOLVE_WALK
                    )
                except FileNotFoundError:
                    if not final:
                        raise AuthorityError("configured data-root parent does not exist") from None
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    child = _open_component(
                        parent_fd, component, _DIRECTORY_FLAGS, resolve=_RESOLVE_WALK
                    )
                    created = True
                identity = _identity(child)
                if not stat.S_ISDIR(identity.mode) or (
                    final and identity.mount_id != parent_identity.mount_id
                ):
                    os.close(child)
                    raise AuthorityError("configured data root crosses an unsafe mount or type")
                named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(child)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    os.close(child)
                    raise AuthorityError("configured data-root component changed during acquisition")
                if final:
                    if created:
                        os.fchmod(child, 0o700)
                    root_identity = _check_directory(child, mount_id=parent_identity.mount_id)
                    _flock(child, fcntl.LOCK_EX, "configured data root is already owned")
                    self._root_claim = self._append_claim("root", child)
                    with _REGISTRY_LOCK:
                        previous = _ROOT_IDENTITIES.get(root_identity.key)
                        if previous is not None and previous() is not None:
                            raise AuthorityError("configured data-root inode is already owned")
                        _ROOT_IDENTITIES[root_identity.key] = weakref.ref(self)
                    self._root_identity = root_identity
                    break
                _flock(child, fcntl.LOCK_SH, "data-root hierarchy is already owned")
                self._append_claim(
                    "ancestor:/" + "/".join(self.spec.components[: index + 1]),
                    child,
                    ancestor=True,
                )
                parent_fd = child
                parent_identity = identity
            assert self._root_claim is not None and self._root_claim.fd is not None
            descendants: dict[str, int] = {}
            for relative in _FIXED_DESCENDANTS:
                current = self._root_claim.fd
                parts: list[str] = []
                for component in relative.split("/"):
                    parts.append(component)
                    key = "/".join(parts)
                    if key in descendants:
                        current = descendants[key]
                        continue
                    created = False
                    try:
                        fd = _open_component(current, component, _DIRECTORY_FLAGS)
                    except FileNotFoundError:
                        os.mkdir(component, 0o700, dir_fd=current)
                        os.fsync(current)
                        fd = _open_component(current, component, _DIRECTORY_FLAGS)
                        created = True
                    if created:
                        os.fchmod(fd, 0o700)
                    _check_directory(fd, mount_id=self._root_identity.mount_id)
                    _flock(fd, fcntl.LOCK_EX, "data-root descendant is already owned")
                    claim = self._append_claim(f"descendant:{key}", fd)
                    self._descendant_claims[key] = claim
                    descendants[key] = fd
                    current = fd
            # Creating the fixed first-level descendants changes a directory's
            # link count.  Persist the post-bootstrap root identity so a
            # journal written by the first owner remains valid for its
            # successor after a crash or failed migration.
            self._root_identity = _check_directory(
                self._root_claim.fd, mount_id=self._root_identity.mount_id
            )
            self._probe_required_primitives()
            with self._condition:
                self._state = AuthorityState.ACQUIRED
            return self
        except BaseException as exc:
            self._state = AuthorityState.FAILED_STARTUP
            self._close_physical_best_effort()
            self._close_logical_if_physical_released()
            if isinstance(exc, OSError):
                raise AuthorityError("data root authority acquisition failed") from exc
            raise

    def _claim_database(self) -> int:
        self.assert_live()
        if self._main_claim is not None and self._main_claim.fd is not None:
            return self._main_claim.fd
        try:
            fd = _open_component(self._database_dir_capability, _MAIN_LEAF, os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise AuthorityError("authoritative database main is absent") from exc
        identity = _check_regular(fd, mount_id=self._root_identity.mount_id)
        _flock(fd, fcntl.LOCK_EX, "authoritative database main is already owned")
        with _REGISTRY_LOCK:
            previous = _DATABASE_IDENTITIES.get(identity.key)
            if previous is not None and previous() is not None and previous() is not self:
                os.close(fd)
                raise AuthorityError("authoritative database inode is already owned")
            _DATABASE_IDENTITIES[identity.key] = weakref.ref(self)
        self._main_claim = self._append_claim("database-main", fd)
        self._main_identity = identity
        return fd

    def _adopt_migration_candidate(
        self, retained_fd: int, directory_fd: int, leaf: str
    ) -> None:
        """Retain an already-open, exclusively locked private candidate."""
        self.assert_live()
        if self._migration_claim is not None:
            raise AuthorityError("migration candidate claim already exists")
        identity = _check_regular(retained_fd, mount_id=self._root_identity.mount_id)
        _flock(retained_fd, fcntl.LOCK_EX, "migration candidate is already owned")
        named_fd = _open_component(directory_fd, leaf, os.O_RDWR | os.O_CLOEXEC)
        try:
            if _identity(named_fd).key != identity.key:
                raise AuthorityError("migration candidate name does not match retained claim")
        finally:
            os.close(named_fd)
        with _REGISTRY_LOCK:
            previous = _DATABASE_IDENTITIES.get(identity.key)
            if previous is not None and previous() is not None and previous() is not self:
                raise AuthorityError("migration candidate inode is already owned")
            _DATABASE_IDENTITIES[identity.key] = weakref.ref(self)
        self._migration_claim = self._append_claim("migration-candidate", retained_fd)
        self._migration_identity = identity

    def _release_migration_candidate(self) -> None:
        claim = self._migration_claim
        identity = self._migration_identity
        if claim is None:
            return
        self._close_claim(claim)
        if identity is not None:
            with _REGISTRY_LOCK:
                current = _DATABASE_IDENTITIES.get(identity.key)
                if current is not None and current() is self:
                    _DATABASE_IDENTITIES.pop(identity.key, None)
        self._migration_claim = None
        self._migration_identity = None

    def _close_database_vfs(self) -> None:
        if self._vfs is None:
            return
        vfs = self._vfs
        if vfs.open_count != 0:
            raise AuthorityError("cannot replace a database claim with live VFS files")
        try:
            vfs.close()
        finally:
            for label, status in vfs.close_inventory:
                claim = self._vfs_claims.get(label)
                if claim is not None:
                    claim.status = status
            if vfs.closed:
                self._vfs = None

    def _promote_migration_candidate(self) -> int:
        """Reclassify the retained candidate, then release the prior main."""
        self.assert_live()
        claim = self._migration_claim
        identity = self._migration_identity
        if claim is None or claim.fd is None or identity is None:
            return self._replace_database_claim_from_canonical()
        named_fd = _open_component(
            self._database_dir_capability, _MAIN_LEAF, os.O_RDWR | os.O_CLOEXEC
        )
        try:
            if _identity(named_fd).key != identity.key:
                raise AuthorityError("promoted name does not match retained candidate claim")
        finally:
            os.close(named_fd)
        self._close_database_vfs()
        old_claim = self._main_claim
        old_identity = self._main_identity
        claim.label = "database-main"
        self._main_claim = claim
        self._main_identity = identity
        self._migration_claim = None
        self._migration_identity = None
        if old_claim is not None and old_claim is not claim:
            self._close_claim(old_claim)
            if old_identity is not None:
                with _REGISTRY_LOCK:
                    current = _DATABASE_IDENTITIES.get(old_identity.key)
                    if current is not None and current() is self:
                        _DATABASE_IDENTITIES.pop(old_identity.key, None)
        return claim.fd

    def _replace_database_claim_from_canonical(self) -> int:
        """Acquire canonical main before releasing the previously claimed inode."""
        self.assert_live()
        self._close_database_vfs()
        new_fd = _open_component(
            self._database_dir_capability, _MAIN_LEAF, os.O_RDWR | os.O_CLOEXEC
        )
        try:
            identity = _check_regular(new_fd, mount_id=self._root_identity.mount_id)
            old_claim = self._main_claim
            old_identity = self._main_identity
            if (
                old_claim is not None
                and old_claim.fd is not None
                and old_claim.status == "HELD"
                and old_identity is not None
                and old_identity.key == identity.key
            ):
                retained_identity = _check_regular(
                    old_claim.fd, mount_id=self._root_identity.mount_id
                )
                if retained_identity.key != identity.key:
                    raise AuthorityError(
                        "retained database claim changed during canonical handoff"
                    )
                os.close(new_fd)
                self._main_identity = retained_identity
                return old_claim.fd
            _flock(new_fd, fcntl.LOCK_EX, "replacement database main is already owned")
            with _REGISTRY_LOCK:
                previous = _DATABASE_IDENTITIES.get(identity.key)
                if (
                    previous is not None
                    and previous() is not None
                    and previous() is not self
                ):
                    raise AuthorityError("replacement database inode is already owned")
                _DATABASE_IDENTITIES[identity.key] = weakref.ref(self)
        except BaseException:
            os.close(new_fd)
            raise
        old_claim = self._main_claim
        old_identity = self._main_identity
        new_claim = self._append_claim("database-main", new_fd)
        self._main_claim = new_claim
        self._main_identity = identity
        if old_claim is not None and old_claim is not new_claim:
            self._close_claim(old_claim)
            if old_identity is not None:
                with _REGISTRY_LOCK:
                    current = _DATABASE_IDENTITIES.get(old_identity.key)
                    if current is not None and current() is self:
                        _DATABASE_IDENTITIES.pop(old_identity.key, None)
        return new_fd

    def _release_database_claim(self) -> None:
        """Release the current main claim during an exclusive migration handoff."""
        self._close_database_vfs()
        claim = self._main_claim
        identity = self._main_identity
        if claim is None:
            return
        self._close_claim(claim)
        if identity is not None:
            with _REGISTRY_LOCK:
                current = _DATABASE_IDENTITIES.get(identity.key)
                if current is not None and current() is self:
                    _DATABASE_IDENTITIES.pop(identity.key, None)
        self._main_claim = None
        self._main_identity = None

    def _open_rooted_vfs(self) -> RootedSQLiteVfs:
        self.assert_live()
        if self._vfs is not None:
            return self._vfs
        main = self._claim_database()
        self._vfs = RootedSQLiteVfs(
            database_dir_fd=self._database_dir_capability,
            main_claim_fd=main,
            temp_dir_fd=self._directory_fd("database/temp"),
            mount_id=self._root_identity.mount_id,
        )
        self._vfs_generation += 1
        self._vfs_claims = {}
        for label, status in self._vfs.close_inventory:
            claim = _Claim(f"rooted-vfs[{self._vfs_generation}]:{label}", None, status)
            self._claims.append(claim)
            self._vfs_claims[label] = claim
        return self._vfs

    @contextmanager
    def operation(self) -> Iterator[None]:
        self._assert_pid()
        with self._condition:
            depth = self._operation_depth()
            if depth:
                if self._state not in {AuthorityState.READY, AuthorityState.CLOSING}:
                    raise AuthorityError(
                        f"nested data-root operation rejected in {self._state.value}"
                    )
                self._operation_local.depth = depth + 1
                outermost = False
            elif self._state != AuthorityState.READY:
                raise AuthorityError(f"data-root operation rejected in {self._state.value}")
            else:
                self._operation_local.depth = 1
                self._active_operations += 1
                outermost = True
        try:
            yield
        finally:
            with self._condition:
                current = self._operation_depth()
                if current <= 1:
                    self._operation_local.depth = 0
                    if outermost:
                        self._active_operations -= 1
                        self._condition.notify_all()
                else:
                    self._operation_local.depth = current - 1

    @contextmanager
    def transition(self) -> Iterator[None]:
        with self.operation():
            with self._transition_gate:
                yield

    def _connection_checkout(self) -> None:
        self._assert_pid()
        with self._condition:
            if self._state not in {
                AuthorityState.ACQUIRED,
                AuthorityState.MIGRATING,
                AuthorityState.RECOVERING,
                AuthorityState.READY,
            } and not (
                self._state == AuthorityState.CLOSING
                and (
                    self._operation_depth() > 0
                    or (
                        self._private_close_checkout
                        and self._close_owner == threading.get_ident()
                    )
                )
            ):
                raise AuthorityError(f"database checkout rejected in {self._state.value}")
            self._checked_out_connections += 1

    def _connection_checkin(self) -> None:
        with self._condition:
            if self._checked_out_connections > 0:
                self._checked_out_connections -= 1
            self._condition.notify_all()

    def _begin_migration(self) -> None:
        with self._condition:
            self._assert_pid()
            if self._state != AuthorityState.ACQUIRED:
                raise AuthorityError(f"migration rejected in {self._state.value}")
            self._state = AuthorityState.MIGRATING

    def _finish_migration(self, *, success: bool) -> None:
        with self._condition:
            self._state = AuthorityState.ACQUIRED if success else AuthorityState.FAILED_STARTUP
            self._condition.notify_all()

    def register_store(self, store) -> None:
        with self._condition:
            self._assert_pid()
            if self._store is not None and self._store is not store:
                raise AuthorityError("one authority may own only one store")
            if self._state != AuthorityState.ACQUIRED:
                raise AuthorityError(f"store publication rejected in {self._state.value}")
            self._store = store
            self._state = AuthorityState.RECOVERING

    def publish_ready(self, store) -> None:
        with self._condition:
            if self._store is not store or self._state != AuthorityState.RECOVERING:
                raise AuthorityError("store readiness publication is invalid")
            self._state = AuthorityState.READY
            self._condition.notify_all()

    def open_store(self):
        self._assert_pid()
        with self._condition:
            while self._opening_store:
                self._condition.wait()
            if self._store is not None:
                if self._state == AuthorityState.READY:
                    return self._store
                raise AuthorityError("store construction is already in progress")
            if self._state != AuthorityState.ACQUIRED:
                raise AuthorityError(
                    f"store construction rejected in {self._state.value}"
                )
            self._opening_store = True
        from bots5.infrastructure.persistence.migration_runner import upgrade_database
        from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore

        try:
            upgrade_database(authority=self)
            return SQLiteAppStateStore._open_from_authority(self)
        except BaseException:
            with self._condition:
                if self._state not in {
                    AuthorityState.FAILED_STARTUP,
                    AuthorityState.FAILED_CLOSED,
                    AuthorityState.POISONED,
                }:
                    self._state = AuthorityState.FAILED_STARTUP
            raise
        finally:
            with self._condition:
                self._opening_store = False
                self._condition.notify_all()

    def poison(self, message: str) -> None:
        with self._condition:
            if self._state in {AuthorityState.READY, AuthorityState.RECOVERING}:
                self._state = AuthorityState.POISONED
            self._closed_error = message
            self._condition.notify_all()

    def _close_claim(self, claim: _Claim) -> None:
        if claim.fd is None or claim.status != "HELD":
            return
        fd = claim.fd
        claim.fd = None
        try:
            os.close(fd)
        except OSError as exc:
            claim.status = "UNKNOWN"
            raise AuthorityError(f"close outcome is uncertain for {claim.label}") from exc
        claim.status = "RELEASED"

    def _close_physical_best_effort(self) -> None:
        try:
            if self._vfs is not None:
                self._close_database_vfs()
            if self._migration_claim is not None:
                identity = self._migration_identity
                self._close_claim(self._migration_claim)
                if identity is not None:
                    with _REGISTRY_LOCK:
                        current = _DATABASE_IDENTITIES.get(identity.key)
                        if current is not None and current() is self:
                            _DATABASE_IDENTITIES.pop(identity.key, None)
            if self._main_claim is not None:
                identity = self._main_identity
                self._close_claim(self._main_claim)
                if identity is not None:
                    with _REGISTRY_LOCK:
                        current = _DATABASE_IDENTITIES.get(identity.key)
                        if current is not None and current() is self:
                            _DATABASE_IDENTITIES.pop(identity.key, None)
            for relative in (
                "database/temp",
                "database/migration",
                "recovery",
                "attachments/captures",
                "attachments/staging",
                "attachments/gc",
                "attachments/objects",
                "database",
                "attachments",
            ):
                claim = self._descendant_claims.get(relative)
                if claim is not None:
                    self._close_claim(claim)
            if self._root_claim is not None:
                identity = getattr(self, "_root_identity", None)
                self._close_claim(self._root_claim)
                if identity is not None:
                    with _REGISTRY_LOCK:
                        current = _ROOT_IDENTITIES.get(identity.key)
                        if current is not None and current() is self:
                            _ROOT_IDENTITIES.pop(identity.key, None)
            for claim in reversed(self._ancestor_claims):
                self._close_claim(claim)
            self._physical_release_complete = all(
                claim.status == "RELEASED" for claim in self._claims
            )
        except BaseException:
            self._state = AuthorityState.FAILED_CLOSED
            raise

    def _close_logical_if_physical_released(self) -> None:
        if not self._physical_release_complete or self._logical_socket is None:
            return
        claim = self._logical_socket
        self._logical_socket = None
        try:
            _close_socket(claim)
        except OSError as exc:
            self._logical_status = "UNKNOWN"
            self._state = AuthorityState.FAILED_CLOSED
            raise AuthorityError("logical-root close outcome is uncertain") from exc
        self._logical_status = "RELEASED"
        with _REGISTRY_LOCK:
            current = _LOGICAL_ROOTS.get(self.spec.logical_root)
            if current is not None and current() is self:
                _LOGICAL_ROOTS.pop(self.spec.logical_root, None)

    def _terminal_close_exception(self) -> BaseException:
        result = self._close_result
        if result is None:
            return AuthorityError(f"data-root authority is terminal: {self._state.value}")
        error_type, arguments, message = result
        try:
            return error_type(*arguments)
        except BaseException:
            return AuthorityError(message)

    def close(self) -> None:
        self._assert_pid()
        caller = threading.get_ident()
        with self._condition:
            while self._state in {
                AuthorityState.CLOSING,
                AuthorityState.RELEASING_LOGICAL,
            } or (
                self._state == AuthorityState.FAILED_CLOSED
                and self._close_owner is not None
            ):
                if self._close_owner == caller:
                    raise AuthorityError("close owner cannot re-enter data-root close")
                self._condition.wait()
            if self._state == AuthorityState.CLOSED:
                return
            if self._state == AuthorityState.FAILED_CLOSED:
                raise self._terminal_close_exception()
            if self._state in {AuthorityState.MIGRATING, AuthorityState.RECOVERING}:
                raise AuthorityError("cannot close while startup work is active")
            self._state = AuthorityState.CLOSING
            self._close_owner = caller
            self._close_result = None
            while self._active_operations or self._checked_out_connections:
                self._condition.wait()
        try:
            if self._store is not None and not getattr(self._store, "closed", False):
                with self._condition:
                    self._private_close_checkout = True
                try:
                    self._store._close_under_authority()
                finally:
                    with self._condition:
                        self._private_close_checkout = False
                        self._condition.notify_all()
            if self._checked_out_connections:
                raise AuthorityError("database connections remain checked out after close")
            if self._main_claim is not None and self._main_claim.fd is not None:
                database_claim = self._descendant_claims.get("database")
                if database_claim is None or database_claim.fd is None:
                    raise AuthorityError("database directory claim is absent during close")
                os.fsync(self._main_claim.fd)
                for leaf in (
                    "state.sqlite3-journal",
                    "state.sqlite3-wal",
                    "state.sqlite3-shm",
                ):
                    try:
                        os.stat(
                            leaf,
                            dir_fd=database_claim.fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    raise AuthorityError("SQLite sidecar remains during close")
                os.fsync(database_claim.fd)
            self._close_physical_best_effort()
            if not self._physical_release_complete:
                raise AuthorityError("physical data-root release is incomplete")
            with self._condition:
                self._state = AuthorityState.RELEASING_LOGICAL
            self._close_logical_if_physical_released()
        except BaseException as exc:
            with self._condition:
                self._state = AuthorityState.FAILED_CLOSED
                self._close_result = (type(exc), exc.args, str(exc))
                self._close_owner = None
                self._condition.notify_all()
            raise self._terminal_close_exception() from exc
        else:
            with self._condition:
                self._state = AuthorityState.CLOSED
                self._close_owner = None
                self._condition.notify_all()

    release = close

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        self.close()


def _after_fork_child() -> None:
    for authority in list(_AUTHORITIES):
        if authority._pid == os.getpid():
            continue
        if authority._vfs is not None:
            authority._vfs.after_fork_child()
        for claim in authority._claims:
            fd = claim.fd
            claim.fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if authority._logical_socket is not None:
            try:
                os.close(authority._logical_socket.detach())
            except OSError:
                pass
            authority._logical_socket = None
        authority._state = AuthorityState.FAILED_CLOSED


os.register_at_fork(after_in_child=_after_fork_child)
