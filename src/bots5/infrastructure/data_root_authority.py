"""Kernel-anchored aggregate authority for one B.O.T.S. data root."""

from __future__ import annotations

import asyncio
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
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator

from bots5.core.errors import AuthorityError
from bots5.infrastructure.rooted_sqlite_vfs import (
    RootedSQLiteVfs,
    RootedVfsRegistrationCloseUnknown,
    native_library,
)


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


@dataclass(frozen=True, slots=True)
class DirectoryIdentityBaseline:
    device_major: int
    device_minor: int
    inode: int
    mount_id: int
    mode: int
    uid: int
    nlink: int

    @classmethod
    def from_identity(cls, value: FileIdentity) -> "DirectoryIdentityBaseline":
        return cls(
            value.device_major,
            value.device_minor,
            value.inode,
            value.mount_id,
            value.mode,
            value.uid,
            value.nlink,
        )

    def matches(self, value: FileIdentity) -> bool:
        return self == DirectoryIdentityBaseline.from_identity(value)


@dataclass(slots=True)
class _Claim:
    label: str
    fd: int | None
    status: str = "HELD"


class _GrantStatus(str, Enum):
    FORWARD = "FORWARD"
    CLEANUP = "CLEANUP"
    RELEASED = "RELEASED"


@dataclass(slots=True)
class _EffectGrant:
    grant_id: str
    epoch: str
    pid: int
    owner: object
    purpose: str
    status: _GrantStatus = _GrantStatus.FORWARD
    resources: set[str] = field(default_factory=set)
    issued_effects: int = 0


class _DatabaseResourceLease:
    """One serialized native/DBAPI resource owned by a logical grant."""

    __slots__ = ("_authority", "_grant", "_resource", "released")

    def __init__(self, authority, grant, resource: str):
        self._authority = authority
        self._grant = grant
        self._resource = resource
        self.released = False

    def assert_forward(self) -> None:
        """Require this resource's exact logical owner, not any live grant."""
        if self.released:
            raise AuthorityError("database resource ownership has been released")
        authority = self._authority
        authority._assert_pid()
        with authority._condition:
            authority._poll_native_unknown_locked()
            current = authority._current_grant_locked(allow_startup=True)
            if current is not self._grant or self._grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError(
                    "database resource owning grant is unavailable or revoked"
                )

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        authority = self._authority
        with authority._condition:
            self._grant.resources.discard(self._resource)
            authority._publish_requested_poison_locked()
            authority._condition.notify_all()


_REGISTRY_LOCK = threading.RLock()
_LOGICAL_ROOTS: dict[str, weakref.ReferenceType["DataRootAuthority"]] = {}
_ROOT_IDENTITIES: dict[tuple[int, int, int, int], weakref.ReferenceType["DataRootAuthority"]] = {}
_DATABASE_IDENTITIES: dict[tuple[int, int, int, int], weakref.ReferenceType["DataRootAuthority"]] = {}
_AUTHORITIES: weakref.WeakSet["DataRootAuthority"] = weakref.WeakSet()
_TERMINAL_AUTHORITIES: set["DataRootAuthority"] = set()


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
    library.bots5_open_fresh_directory.argtypes = [ctypes.c_int]
    library.bots5_open_fresh_directory.restype = ctypes.c_int
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
        self._epoch = uuid.uuid4().hex
        self._grant_context: ContextVar[_EffectGrant | None] = ContextVar(
            f"bots5_data_root_effect_grant_{id(self)}", default=None
        )
        self._transition_context: ContextVar[str | None] = ContextVar(
            f"bots5_data_root_transition_grant_{id(self)}", default=None
        )
        self._grants: dict[str, _EffectGrant] = {}
        self._startup_grant: _EffectGrant | None = None
        self._pending_invalidation: AuthorityState | None = None
        self._pending_cause: str | None = None
        self._active_operations = 0
        self._poison_requested = False
        self._checked_out_connections = 0
        self._opening_store = False
        self._store = None
        self._vfs: RootedSQLiteVfs | None = None
        self._vfs_claims: dict[str, _Claim] = {}
        self._vfs_claim_groups: dict[str, dict[str, _Claim]] = {}
        self._vfs_instances: dict[str, RootedSQLiteVfs] = {}
        self._vfs_generation = 0
        self._main_identity: FileIdentity | None = None
        self._claims: list[_Claim] = []
        self._ancestor_claims: list[_Claim] = []
        self._descendant_claims: dict[str, _Claim] = {}
        self._directory_baselines: dict[str, DirectoryIdentityBaseline] = {}
        self._root_claim: _Claim | None = None
        self._main_claim: _Claim | None = None
        self._migration_claim: _Claim | None = None
        self._migration_identity: FileIdentity | None = None
        self._logical_socket: socket.socket | None = None
        self._logical_status = "UNBOUND"
        self._physical_release_complete = False
        self._closed_error: str | None = None
        self._close_owner: int | None = None
        self._close_result: tuple[str, str] | None = None
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
        with self._condition:
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
        with self._condition:
            self._poll_native_unknown_locked()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is None or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("data root authority has no forward effect grant")
            if self._state not in {
                AuthorityState.ACQUIRING,
                AuthorityState.ACQUIRED,
                AuthorityState.MIGRATING,
                AuthorityState.RECOVERING,
                AuthorityState.READY,
                AuthorityState.CLOSING,
            }:
                raise AuthorityError(
                    f"data root authority is not live: {self._state.value}"
                )
            if self._root_claim is None or self._root_claim.fd is None:
                raise AuthorityError("data root authority has no physical root claim")

    def _operation_depth(self) -> int:
        grant = self._grant_context.get()
        return int(
            grant is not None
            and grant.epoch == self._epoch
            and grant.pid == os.getpid()
            and grant.owner == self._application_owner()
            and grant.status is not _GrantStatus.RELEASED
        )

    @staticmethod
    def _application_owner() -> object:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        return task if task is not None else ("thread", threading.get_ident())

    def _has_application_admission(self) -> bool:
        grant = self._grant_context.get()
        return bool(
            grant is not None
            and grant.epoch == self._epoch
            and grant.pid == os.getpid()
            and grant.owner == self._application_owner()
            and grant.purpose == "application"
            and grant.status is _GrantStatus.FORWARD
            and self._grants.get(grant.grant_id) is grant
        )

    def _new_grant_locked(self, purpose: str) -> _EffectGrant:
        grant = _EffectGrant(
            uuid.uuid4().hex,
            self._epoch,
            self._pid,
            self._application_owner(),
            purpose,
        )
        self._grants[grant.grant_id] = grant
        if purpose in {"runtime", "application"}:
            self._active_operations += 1
        return grant

    def _current_grant_locked(
        self, *, allow_startup: bool = False
    ) -> _EffectGrant | None:
        grant = self._grant_context.get()
        if grant is not None:
            if (
                grant.epoch != self._epoch
                or grant.pid != os.getpid()
                or self._grants.get(grant.grant_id) is not grant
            ):
                raise AuthorityError("effect grant is stale or belongs to another authority")
            if grant.owner != self._application_owner():
                raise AuthorityError("effect grant belongs to another executor")
            return grant
        if (
            allow_startup
            and self._startup_grant is not None
            and self._startup_grant.owner == self._application_owner()
            and self._startup_grant.status is not _GrantStatus.RELEASED
            and self._state
            in {
                AuthorityState.ACQUIRING,
                AuthorityState.ACQUIRED,
                AuthorityState.MIGRATING,
                AuthorityState.RECOVERING,
            }
        ):
            return self._startup_grant
        return None

    def _assert_current_forward(self) -> None:
        self._assert_pid()
        with self._condition:
            self._poll_native_unknown_locked()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is None or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("forward effect grant is unavailable or revoked")

    @staticmethod
    def _invalidation_rank(state: AuthorityState) -> int:
        return {
            AuthorityState.POISONED: 1,
            AuthorityState.FAILED_STARTUP: 2,
            AuthorityState.FAILED_CLOSED: 3,
        }[state]

    def _forward_work_locked(self) -> bool:
        return any(
            grant.status is _GrantStatus.FORWARD
            or grant.issued_effects
            or grant.resources
            for grant in self._grants.values()
        )

    def _publish_requested_poison_locked(self) -> None:
        target = self._pending_invalidation
        if target is not None and not self._forward_work_locked():
            self._state = target
            self._condition.notify_all()

    def _set_pending_invalidation_locked(
        self,
        target: AuthorityState,
        message: str,
        origin: _EffectGrant | None,
    ) -> None:
        previous = self._pending_invalidation
        if previous is None or self._invalidation_rank(target) > self._invalidation_rank(
            previous
        ):
            self._pending_invalidation = target
        if self._pending_cause is None:
            self._pending_cause = message
        self._poison_requested = True
        self._closed_error = message
        if origin is not None and origin.status is _GrantStatus.FORWARD:
            origin.status = _GrantStatus.CLEANUP
        self._publish_requested_poison_locked()
        self._condition.notify_all()

    def _request_invalidation(
        self, target: AuthorityState | str, message: str
    ) -> None:
        if not isinstance(target, AuthorityState):
            target = AuthorityState(target)
        if target not in {
            AuthorityState.POISONED,
            AuthorityState.FAILED_STARTUP,
            AuthorityState.FAILED_CLOSED,
        }:
            raise ValueError("invalid terminal authority target")
        with self._condition:
            current = self._grant_context.get()
            origin = None
            if (
                current is not None
                and current.epoch == self._epoch
                and current.pid == os.getpid()
                and current.owner == self._application_owner()
                and self._grants.get(current.grant_id) is current
            ):
                origin = current
            self._set_pending_invalidation_locked(target, message, origin)

    def _poll_native_unknown_locked(self) -> None:
        for key, vfs in tuple(self._vfs_instances.items()):
            if vfs.closed:
                continue
            try:
                generation = vfs.unknown_close_generation
            except AuthorityError:
                continue
            claims = self._vfs_claim_groups.get(key, {})
            claim = claims.get("native-open-files")
            if generation and claim is not None and claim.status != "UNKNOWN":
                claim.status = "UNKNOWN"
                self._pin_terminal()
                self._set_pending_invalidation_locked(
                    AuthorityState.FAILED_CLOSED,
                    "native rooted VFS close outcome is uncertain",
                    None,
                )

    @property
    def poison_pending(self) -> bool:
        with self._condition:
            return self._pending_invalidation is not None

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
            self._close_provisional_logical(claim)
            raise AuthorityError(
                "mandatory abstract Unix logical-root claim is unavailable or already owned"
            ) from exc
        with _REGISTRY_LOCK:
            previous = _LOGICAL_ROOTS.get(self.spec.logical_root)
            if previous is not None and previous() is not None:
                self._close_provisional_logical(claim)
                raise AuthorityError("configured logical data root is already owned")
            _LOGICAL_ROOTS[self.spec.logical_root] = weakref.ref(self)
        self._logical_socket = claim
        self._logical_status = "HELD"

    def _close_provisional_logical(self, claim: socket.socket) -> None:
        """Classify an uncertain socket close before the logical claim is bound."""
        try:
            claim.close()
        except OSError as exc:
            self._claims.append(_Claim("provisional:logical-root", None, "UNKNOWN"))
            self._logical_status = "UNKNOWN"
            self._pin_terminal()
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "provisional logical-root close outcome is uncertain",
            )
            raise AuthorityError(
                "provisional logical-root close outcome is uncertain"
            ) from exc

    def _append_claim(self, label: str, fd: int, *, ancestor: bool = False) -> _Claim:
        claim = _Claim(label, fd)
        self._claims.append(claim)
        if ancestor:
            self._ancestor_claims.append(claim)
        return claim

    def _claim_scoped_fd(self, label: str, fd: int) -> _Claim:
        self._assert_pid()
        if any(
            claim.fd == fd and claim.status == "HELD" for claim in self._claims
        ):
            raise AuthorityError("descriptor already has an authority owner")
        return self._append_claim(f"scoped:{label}", fd)

    def _release_scoped_fd(self, claim: _Claim) -> None:
        if claim not in self._claims or not claim.label.startswith("scoped:"):
            raise AuthorityError("scoped descriptor ownership is invalid")
        self._close_claim(claim)

    def _pin_terminal(self) -> None:
        with _REGISTRY_LOCK:
            _TERMINAL_AUTHORITIES.add(self)

    def _record_directory_baselines(self) -> None:
        if self._root_claim is None or self._root_claim.fd is None:
            raise AuthorityError("root baseline has no retained capability")
        values = {"root": self._root_claim.fd}
        values.update(
            {
                relative: claim.fd
                for relative, claim in self._descendant_claims.items()
                if claim.fd is not None
            }
        )
        if set(values) != {"root", *_FIXED_DESCENDANTS}:
            raise AuthorityError("fixed directory topology is incomplete")
        self._directory_baselines = {
            relative: DirectoryIdentityBaseline.from_identity(
                _check_directory(fd, mount_id=self._root_identity.mount_id)
            )
            for relative, fd in values.items()
        }

    def fresh_directory_inventory(self, relative: str) -> tuple[str, ...]:
        """Enumerate one new open-file description and expose it only after close."""
        self._assert_pid()
        if relative not in {"root", *_FIXED_DESCENDANTS}:
            raise AuthorityError(f"unknown directory observation area: {relative}")

        def classify(message: str) -> None:
            with self._condition:
                state = self._state
            target = (
                AuthorityState.POISONED
                if state in {AuthorityState.READY, AuthorityState.RECOVERING}
                else AuthorityState.FAILED_CLOSED
                if state == AuthorityState.CLOSING
                else AuthorityState.FAILED_STARTUP
            )
            self._request_invalidation(target, message)

        with self.operation():
            claim = (
                self._root_claim
                if relative == "root"
                else self._descendant_claims.get(relative)
            )
            baseline = self._directory_baselines.get(relative)
            if (
                claim is None
                or claim.fd is None
                or claim.status != "HELD"
                or baseline is None
            ):
                classify(f"directory observation is not claimed: {relative}")
                raise AuthorityError(
                    f"directory observation is not claimed: {relative}"
                )
            fresh_fd = _native().bots5_open_fresh_directory(claim.fd)
            if fresh_fd < 0:
                error = ctypes.get_errno()
                classify("fresh directory observation cannot be opened")
                raise AuthorityError(
                    "fresh directory observation cannot be opened"
                ) from OSError(error, os.strerror(error))
            fresh_claim = self._append_claim(f"fresh-view:{relative}", fresh_fd)
            try:
                observed = _check_directory(
                    fresh_fd, mount_id=self._root_identity.mount_id
                )
                if not baseline.matches(observed):
                    raise AuthorityError(
                        "fresh directory observation changed identity"
                    )
                result = tuple(sorted(os.listdir(fresh_fd)))
            except BaseException:
                try:
                    self._close_claim(fresh_claim)
                except BaseException:
                    self._request_invalidation(
                        AuthorityState.FAILED_CLOSED,
                        "fresh directory close outcome is uncertain",
                    )
                    raise
                classify("fresh directory observation failed")
                raise
            try:
                self._close_claim(fresh_claim)
            except BaseException:
                self._request_invalidation(
                    AuthorityState.FAILED_CLOSED,
                    "fresh directory close outcome is uncertain",
                )
                raise
            return result

    def database_durability_fence(self) -> None:
        """Durably hand committed database authority to attachment mutation."""
        def sync_claims() -> None:
            self.assert_live()
            if self._main_claim is None or self._main_claim.fd is None:
                raise AuthorityError("database durability fence has no main claim")
            os.fsync(self._main_claim.fd)
            os.fsync(self._database_dir_capability)

        with self.operation():
            try:
                sync_claims()
            except BaseException:
                with self._condition:
                    startup = self._state in {
                        AuthorityState.ACQUIRING,
                        AuthorityState.ACQUIRED,
                        AuthorityState.MIGRATING,
                        AuthorityState.RECOVERING,
                    }
                self._request_invalidation(
                    AuthorityState.FAILED_STARTUP if startup else AuthorityState.POISONED,
                    "database durability fence failed",
                )
                raise

    def _sync_topology_parent(self, parent_fd: int, edge_label: str) -> None:
        """Acknowledge one fixed topology edge for this acquisition.

        The label is intentionally diagnostic only.  Replaying the containing
        directory barrier on every acquisition means a failed mkdir/fsync
        obligation cannot be forgotten merely because the child name remains
        visible on the next startup.
        """
        del edge_label
        os.fsync(parent_fd)

    def _probe_required_primitives(self) -> None:
        """Exercise required data-filesystem primitives before any DB open."""
        claim = self._descendant_claims.get("database/temp")
        if claim is None or claim.fd is None:
            raise AuthorityError("data-root capability probe has no temporary directory")
        directory_fd = claim.fd
        if self.fresh_directory_inventory("database/temp"):
            raise AuthorityError("database temporary directory contains unexplained state")
        token = uuid.uuid4().hex
        first = f".bots5-probe-{token}-a"
        second = f".bots5-probe-{token}-b"
        descriptors: list[_Claim] = []
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
                descriptors.append(
                    self._append_claim(f"provisional:primitive-probe:{leaf}", fd)
                )
                os.fchmod(fd, 0o600)
                _check_regular(fd, mount_id=self._root_identity.mount_id)
                if os.write(fd, payload) != 1:
                    raise AuthorityError("data-root capability probe write was short")
                os.fsync(fd)
            assert descriptors[0].fd is not None and descriptors[1].fd is not None
            first_identity = _identity(descriptors[0].fd)
            second_identity = _identity(descriptors[1].fd)
            from bots5.infrastructure.attachments import (
                _rename_exchange,
                _rename_noreplace,
            )

            _rename_exchange(directory_fd, first, directory_fd, second)
            opened_first = _open_component(
                directory_fd, first, os.O_RDONLY | os.O_CLOEXEC
            )
            first_proof = self._append_claim(
                "provisional:primitive-probe:first-proof", opened_first
            )
            try:
                opened_second = _open_component(
                    directory_fd, second, os.O_RDONLY | os.O_CLOEXEC
                )
                second_proof = self._append_claim(
                    "provisional:primitive-probe:second-proof", opened_second
                )
                try:
                    if (
                        _identity(opened_first).key != second_identity.key
                        or _identity(opened_second).key != first_identity.key
                    ):
                        raise AuthorityError("RENAME_EXCHANGE identity proof failed")
                finally:
                    self._close_claim(second_proof)
            finally:
                self._close_claim(first_proof)
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
                self._close_claim(descriptors.pop())

    def acquire(self) -> "DataRootAuthority":
        self._assert_pid()
        if self._state != AuthorityState.ACQUIRING:
            raise AuthorityError("data root authority may be acquired only once")
        with self._condition:
            startup = self._new_grant_locked("startup")
            self._startup_grant = startup
        token = self._grant_context.set(startup)
        try:
            self._bind_logical()
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
                    child = _open_component(
                        parent_fd, component, _DIRECTORY_FLAGS, resolve=_RESOLVE_WALK
                    )
                    created = True
                child_claim = self._append_claim(
                    "provisional:root-component:" + component, child
                )
                identity = _identity(child)
                if not stat.S_ISDIR(identity.mode) or (
                    final and identity.mount_id != parent_identity.mount_id
                ):
                    raise AuthorityError("configured data root crosses an unsafe mount or type")
                named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(child)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise AuthorityError("configured data-root component changed during acquisition")
                if final:
                    if created:
                        os.fchmod(child, 0o700)
                    root_identity = _check_directory(child, mount_id=parent_identity.mount_id)
                    _flock(child, fcntl.LOCK_EX, "configured data root is already owned")
                    child_claim.label = "root"
                    self._root_claim = child_claim
                    with _REGISTRY_LOCK:
                        previous = _ROOT_IDENTITIES.get(root_identity.key)
                        if previous is not None and previous() is not None:
                            raise AuthorityError("configured data-root inode is already owned")
                        _ROOT_IDENTITIES[root_identity.key] = weakref.ref(self)
                    self._root_identity = root_identity
                    self._sync_topology_parent(parent_fd, "configured-root")
                    break
                _flock(child, fcntl.LOCK_SH, "data-root hierarchy is already owned")
                child_claim.label = (
                    "ancestor:/" + "/".join(self.spec.components[: index + 1])
                )
                self._ancestor_claims.append(child_claim)
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
                        fd = _open_component(current, component, _DIRECTORY_FLAGS)
                        created = True
                    claim = self._append_claim(f"provisional:descendant:{key}", fd)
                    if created:
                        os.fchmod(fd, 0o700)
                    _check_directory(fd, mount_id=self._root_identity.mount_id)
                    _flock(fd, fcntl.LOCK_EX, "data-root descendant is already owned")
                    self._sync_topology_parent(current, key)
                    claim.label = f"descendant:{key}"
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
            self._record_directory_baselines()
            self._probe_required_primitives()
            with self._condition:
                self._state = AuthorityState.ACQUIRED
            return self
        except BaseException as exc:
            self._request_invalidation(
                AuthorityState.FAILED_STARTUP,
                "data root authority acquisition failed",
            )
            self._close_physical_best_effort()
            self._close_logical_if_physical_released()
            if isinstance(exc, OSError):
                raise AuthorityError("data root authority acquisition failed") from exc
            raise
        finally:
            self._grant_context.reset(token)

    def _claim_database(self) -> int:
        self.assert_live()
        if self._main_claim is not None and self._main_claim.fd is not None:
            return self._main_claim.fd
        try:
            fd = _open_component(self._database_dir_capability, _MAIN_LEAF, os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise AuthorityError("authoritative database main is absent") from exc
        claim = self._append_claim("provisional:database-main", fd)
        try:
            identity = _check_regular(fd, mount_id=self._root_identity.mount_id)
            _flock(fd, fcntl.LOCK_EX, "authoritative database main is already owned")
            with _REGISTRY_LOCK:
                previous = _DATABASE_IDENTITIES.get(identity.key)
                if previous is not None and previous() is not None and previous() is not self:
                    raise AuthorityError("authoritative database inode is already owned")
                _DATABASE_IDENTITIES[identity.key] = weakref.ref(self)
        except BaseException:
            self._close_claim(claim)
            raise
        claim.label = "database-main"
        self._main_claim = claim
        self._main_identity = identity
        return fd

    def _adopt_migration_candidate(
        self, retained_fd: int, directory_fd: int, leaf: str
    ) -> None:
        """Retain an already-open, exclusively locked private candidate."""
        self.assert_live()
        if self._migration_claim is not None:
            raise AuthorityError("migration candidate claim already exists")
        claim = next(
            (
                item
                for item in self._claims
                if item.fd == retained_fd
                and item.status == "HELD"
                and item.label.startswith("scoped:")
            ),
            None,
        )
        if claim is None:
            claim = self._append_claim("provisional:migration-candidate", retained_fd)
        try:
            identity = _check_regular(retained_fd, mount_id=self._root_identity.mount_id)
            _flock(retained_fd, fcntl.LOCK_EX, "migration candidate is already owned")
            named_fd = _open_component(directory_fd, leaf, os.O_RDWR | os.O_CLOEXEC)
            proof = self._append_claim("provisional:migration-name-proof", named_fd)
            try:
                if _identity(named_fd).key != identity.key:
                    raise AuthorityError(
                        "migration candidate name does not match retained claim"
                    )
            finally:
                self._close_claim(proof)
            with _REGISTRY_LOCK:
                previous = _DATABASE_IDENTITIES.get(identity.key)
                if previous is not None and previous() is not None and previous() is not self:
                    raise AuthorityError("migration candidate inode is already owned")
                _DATABASE_IDENTITIES[identity.key] = weakref.ref(self)
        except BaseException:
            self._close_claim(claim)
            raise
        claim.label = "migration-candidate"
        self._migration_claim = claim
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
                if status == "UNKNOWN":
                    self._pin_terminal()
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
        proof = self._append_claim("provisional:promotion-name-proof", named_fd)
        try:
            if _identity(named_fd).key != identity.key:
                raise AuthorityError("promoted name does not match retained candidate claim")
        finally:
            self._close_claim(proof)
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
        new_claim = self._append_claim("provisional:database-replacement", new_fd)
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
                self._close_claim(new_claim)
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
            self._close_claim(new_claim)
            raise
        old_claim = self._main_claim
        old_identity = self._main_identity
        new_claim.label = "database-main"
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

    def _record_vfs_registration_close_unknown(
        self, resource_key: str | None = None
    ) -> None:
        if resource_key is None:
            self._vfs_generation += 1
            resource_key = f"rooted-vfs[{self._vfs_generation}]"
        claim = _Claim(
            f"{resource_key}:native-open-files",
            None,
            "UNKNOWN",
        )
        self._claims.append(claim)
        group = {"native-open-files": claim}
        self._vfs_claim_groups[resource_key] = group
        self._vfs_claims = group
        self._close_result = (
            "rooted_vfs_registration_cleanup_incomplete",
            "rooted VFS registration cleanup incomplete",
        )
        self._pin_terminal()
        self._request_invalidation(
            AuthorityState.FAILED_CLOSED,
            "rooted VFS registration cleanup incomplete",
        )

    def _register_vfs_instance(
        self, resource_key: str, vfs: RootedSQLiteVfs
    ) -> None:
        self._assert_current_forward()
        with self._condition:
            if resource_key in self._vfs_claim_groups:
                raise AuthorityError("rooted VFS resource identity is already registered")
            group: dict[str, _Claim] = {}
            for label, status in vfs.close_inventory:
                claim = _Claim(f"{resource_key}:{label}", None, status)
                self._claims.append(claim)
                group[label] = claim
            self._vfs_claim_groups[resource_key] = group
            self._vfs_instances[resource_key] = vfs

    def _merge_vfs_close_inventory(
        self, resource_key: str, inventory: tuple[tuple[str, str], ...]
    ) -> None:
        unknown = False
        with self._condition:
            group = self._vfs_claim_groups.get(resource_key)
            if group is None:
                raise AuthorityError("rooted VFS close has no authority ledger")
            for label, status in inventory:
                claim = group.get(label)
                if claim is None:
                    raise AuthorityError("rooted VFS close inventory changed shape")
                claim.status = status
                unknown = unknown or status == "UNKNOWN"
            self._vfs_instances.pop(resource_key, None)
        if unknown:
            self._pin_terminal()
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "rooted VFS private close outcome is uncertain",
            )

    def _record_native_vfs_unknown(
        self, resource_key: str, operation: str, generation: int
    ) -> None:
        del generation
        with self._condition:
            claim = self._vfs_claim_groups.get(resource_key, {}).get(
                "native-open-files"
            )
            if claim is not None:
                claim.status = "UNKNOWN"
        self._pin_terminal()
        self._request_invalidation(
            AuthorityState.FAILED_CLOSED,
            f"native rooted VFS close outcome is uncertain during {operation}",
        )

    def _open_rooted_vfs(self) -> RootedSQLiteVfs:
        self.assert_live()
        if self._vfs is not None:
            return self._vfs
        main = self._claim_database()
        self._vfs_generation += 1
        resource_key = f"rooted-vfs[{self._vfs_generation}]"
        try:
            vfs = RootedSQLiteVfs(
                database_dir_fd=self._database_dir_capability,
                main_claim_fd=main,
                temp_dir_fd=self._directory_fd("database/temp"),
                mount_id=self._root_identity.mount_id,
                authority=self,
                resource_label=resource_key,
            )
        except RootedVfsRegistrationCloseUnknown:
            raise AuthorityError(
                "rooted VFS registration cleanup incomplete"
            ) from None
        self._vfs = vfs
        self._vfs_claims = self._vfs_claim_groups[resource_key]
        return self._vfs

    @contextmanager
    def operation(self) -> Iterator[None]:
        self._assert_pid()
        token = None
        created = False
        with self._condition:
            self._poll_native_unknown_locked()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is not None:
                if grant.status is not _GrantStatus.FORWARD:
                    raise AuthorityError("effect grant has been revoked")
                if self._grant_context.get() is None:
                    token = self._grant_context.set(grant)
            else:
                if (
                    self._state != AuthorityState.READY
                    or self._pending_invalidation is not None
                ):
                    raise AuthorityError(
                        f"data-root operation rejected in {self._state.value}"
                    )
                grant = self._new_grant_locked("runtime")
                token = self._grant_context.set(grant)
                created = True
        try:
            yield
        finally:
            if created:
                with self._condition:
                    if grant.status is not _GrantStatus.RELEASED:
                        grant.status = _GrantStatus.RELEASED
                        self._active_operations -= 1
                    self._publish_requested_poison_locked()
                    self._condition.notify_all()
            if token is not None:
                self._grant_context.reset(token)

    @contextmanager
    def application_operation(self, *, independent: bool = False) -> Iterator[None]:
        """Keep one application command ordered before any racing poison."""
        self._assert_pid()
        owner = self._application_owner()
        token = None
        created = False
        with self._condition:
            self._poll_native_unknown_locked()
            raw = self._grant_context.get()
            if raw is not None and raw.owner == owner:
                current = self._current_grant_locked()
                if current.status is not _GrantStatus.FORWARD:
                    raise AuthorityError("application effect grant has been revoked")
                grant = current
            elif raw is not None and not independent:
                raise AuthorityError("application effect grant belongs to another executor")
            else:
                grant = None
            if grant is None and (
                self._state != AuthorityState.READY
                or self._pending_invalidation is not None
            ):
                raise AuthorityError(
                    f"application admission rejected in {self._state.value}"
                )
            if grant is None:
                grant = self._new_grant_locked("application")
                token = self._grant_context.set(grant)
                created = True
        try:
            yield
        finally:
            if created:
                with self._condition:
                    if grant.status is not _GrantStatus.RELEASED:
                        grant.status = _GrantStatus.RELEASED
                        self._active_operations -= 1
                    self._publish_requested_poison_locked()
                    self._condition.notify_all()
            if token is not None:
                self._grant_context.reset(token)

    @contextmanager
    def issued_effect(self) -> Iterator[None]:
        """Retain one already-issued child effect through settlement."""
        with self._condition:
            grant = self._current_grant_locked()
            if grant is None or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("event effect grant is unavailable or revoked")
            grant.issued_effects += 1
        try:
            yield
        finally:
            with self._condition:
                grant.issued_effects -= 1
                self._publish_requested_poison_locked()
                self._condition.notify_all()

    def _acquire_database_resource(self, label: str) -> _DatabaseResourceLease:
        with self._condition:
            self._poll_native_unknown_locked()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is None or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("database resource has no forward grant")
        self._assert_current_forward()
        resource = f"database:{label}:{uuid.uuid4().hex}"
        with self._condition:
            grant.resources.add(resource)
        return _DatabaseResourceLease(self, grant, resource)

    @contextmanager
    def transition(self) -> Iterator[None]:
        with self.operation():
            grant = self._grant_context.get()
            assert grant is not None
            if self._transition_context.get() == grant.grant_id:
                yield
                return
            with self._transition_gate:
                token = self._transition_context.set(grant.grant_id)
                try:
                    yield
                finally:
                    self._transition_context.reset(token)

    def _connection_checkout(self) -> None:
        self._assert_pid()
        with self._condition:
            self._poll_native_unknown_locked()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is None or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("database checkout has no forward grant")
            self._checked_out_connections += 1

    def _connection_checkin(self) -> None:
        with self._condition:
            if self._checked_out_connections > 0:
                self._checked_out_connections -= 1
            self._condition.notify_all()

    @contextmanager
    def _startup_operation(self) -> Iterator[None]:
        self._assert_pid()
        token = None
        with self._condition:
            grant = self._startup_grant
            if (
                grant is None
                or grant.owner != self._application_owner()
                or self._grants.get(grant.grant_id) is not grant
                or grant.status is not _GrantStatus.FORWARD
            ):
                raise AuthorityError("startup effect grant is unavailable or revoked")
            raw = self._grant_context.get()
            if raw is not None and raw is not grant:
                raise AuthorityError("another effect grant is active during startup")
            if raw is None:
                token = self._grant_context.set(grant)
        try:
            yield
        finally:
            if token is not None:
                self._grant_context.reset(token)

    def _begin_migration(self) -> None:
        with self._condition:
            self._assert_pid()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is not self._startup_grant or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("migration has no valid startup grant")
            if self._state != AuthorityState.ACQUIRED:
                raise AuthorityError(f"migration rejected in {self._state.value}")
            self._state = AuthorityState.MIGRATING

    def _finish_migration(self, *, success: bool) -> None:
        with self._condition:
            grant = self._current_grant_locked(allow_startup=True)
            if (
                success
                and grant is self._startup_grant
                and grant.status is _GrantStatus.FORWARD
                and self._pending_invalidation is None
            ):
                self._state = AuthorityState.ACQUIRED
            elif self._pending_invalidation is None:
                self._set_pending_invalidation_locked(
                    AuthorityState.FAILED_STARTUP,
                    "database migration failed",
                    grant,
                )
            else:
                self._publish_requested_poison_locked()
            self._condition.notify_all()

    def register_store(self, store) -> None:
        with self._condition:
            self._assert_pid()
            grant = self._current_grant_locked(allow_startup=True)
            if grant is not self._startup_grant or grant.status is not _GrantStatus.FORWARD:
                raise AuthorityError("store publication has no valid startup grant")
            if self._store is not None and self._store is not store:
                raise AuthorityError("one authority may own only one store")
            if self._state != AuthorityState.ACQUIRED:
                raise AuthorityError(f"store publication rejected in {self._state.value}")
            self._store = store
            self._state = AuthorityState.RECOVERING

    def publish_ready(self, store) -> None:
        with self._condition:
            grant = self._current_grant_locked(allow_startup=True)
            if (
                self._store is not store
                or self._state != AuthorityState.RECOVERING
                or grant is not self._startup_grant
                or grant.status is not _GrantStatus.FORWARD
                or self._pending_invalidation is not None
                or grant.resources
                or grant.issued_effects
            ):
                raise AuthorityError("store readiness publication is invalid")
            self._state = AuthorityState.READY
            grant.status = _GrantStatus.RELEASED
            self._startup_grant = None
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
            # Acquisition establishes the one startup grant before any root
            # effect.  Once acquisition returns, its first store opener may be
            # a designated worker; transfer ownership exactly once while the
            # authority is still quiescent ACQUIRED.
            if (
                self._startup_grant is not None
                and self._startup_grant.owner != self._application_owner()
                and self._grant_context.get() is None
            ):
                self._startup_grant.owner = self._application_owner()
            self._opening_store = True
        try:
            with self._startup_operation():
                from bots5.infrastructure.persistence.migration_runner import upgrade_database
                from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore

                upgrade_database(authority=self)
                return SQLiteAppStateStore._open_from_authority(self)
        except BaseException:
            with self._condition:
                if self._pending_invalidation is None:
                    self._set_pending_invalidation_locked(
                        AuthorityState.FAILED_STARTUP,
                        "state store startup failed",
                        self._startup_grant,
                    )
            raise
        finally:
            with self._condition:
                self._opening_store = False
                self._condition.notify_all()

    def poison(self, message: str) -> None:
        with self._condition:
            startup = self._state in {
                AuthorityState.ACQUIRING,
                AuthorityState.ACQUIRED,
                AuthorityState.MIGRATING,
            }
        self._request_invalidation(
            AuthorityState.FAILED_STARTUP if startup else AuthorityState.POISONED,
            message,
        )

    def _close_claim(self, claim: _Claim) -> None:
        if claim.fd is None or claim.status != "HELD":
            return
        fd = claim.fd
        claim.fd = None
        try:
            os.close(fd)
        except OSError as exc:
            claim.status = "UNKNOWN"
            self._pin_terminal()
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                f"close outcome is uncertain for {claim.label}",
            )
            raise AuthorityError(f"close outcome is uncertain for {claim.label}") from exc
        claim.status = "RELEASED"

    def _close_physical_best_effort(self) -> None:
        first_failure: BaseException | None = None

        def attempt(operation) -> None:
            nonlocal first_failure
            try:
                operation()
            except BaseException as exc:
                if first_failure is None:
                    first_failure = exc

        # Migration/recovery engines own distinct registered VFS instances.
        # Their native private descriptors are authority claims too, even
        # though only the steady-state VFS is stored in ``self._vfs``.
        for vfs in tuple(self._vfs_instances.values()):
            if vfs is not self._vfs:
                attempt(vfs.close)
        if self._vfs is not None:
            attempt(self._close_database_vfs)
        if self._migration_claim is not None:
            identity = self._migration_identity
            attempt(lambda: self._close_claim(self._migration_claim))
            if self._migration_claim.status == "RELEASED" and identity is not None:
                with _REGISTRY_LOCK:
                    current = _DATABASE_IDENTITIES.get(identity.key)
                    if current is not None and current() is self:
                        _DATABASE_IDENTITIES.pop(identity.key, None)
        if self._main_claim is not None:
            identity = self._main_identity
            attempt(lambda: self._close_claim(self._main_claim))
            if self._main_claim.status == "RELEASED" and identity is not None:
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
                attempt(lambda claim=claim: self._close_claim(claim))
        if self._root_claim is not None:
            identity = getattr(self, "_root_identity", None)
            attempt(lambda: self._close_claim(self._root_claim))
            if self._root_claim.status == "RELEASED" and identity is not None:
                with _REGISTRY_LOCK:
                    current = _ROOT_IDENTITIES.get(identity.key)
                    if current is not None and current() is self:
                        _ROOT_IDENTITIES.pop(identity.key, None)
        for claim in reversed(self._ancestor_claims):
            attempt(lambda claim=claim: self._close_claim(claim))
        # Any provisional or scoped capability omitted by a role-specific path
        # is still owned here and receives exactly one close attempt.
        for claim in reversed(self._claims):
            if claim.status == "HELD" and claim.fd is not None:
                attempt(lambda claim=claim: self._close_claim(claim))
        self._physical_release_complete = all(
            claim.status == "RELEASED" for claim in self._claims
        )
        if not self._physical_release_complete:
            self._pin_terminal()
        if first_failure is not None:
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "physical data-root release is incomplete",
            )
            raise first_failure

    def _close_logical_if_physical_released(self) -> None:
        if not self._physical_release_complete or self._logical_socket is None:
            return
        claim = self._logical_socket
        self._logical_socket = None
        try:
            _close_socket(claim)
        except OSError as exc:
            self._logical_status = "UNKNOWN"
            self._pin_terminal()
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "logical-root close outcome is uncertain",
            )
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
        _code, message = result
        return AuthorityError(message)

    def _classified_close_failure_result(self) -> tuple[str, str]:
        # Classify only from authority-owned scalar state. In particular, never
        # retain or interpolate the exception whose graph may contain secrets.
        if any(
            claim.status == "UNKNOWN"
            for group in self._vfs_claim_groups.values()
            for claim in group.values()
        ):
            return (
                "rooted_vfs_private_close_incomplete",
                "rooted VFS private close incomplete",
            )
        return (
            "authority_close_failed",
            "data-root authority close failed",
        )

    def _failed_close_requires_terminal_pin(self) -> bool:
        return (
            not self._physical_release_complete
            or self._logical_status in {"HELD", "UNKNOWN"}
            or any(
                claim.status in {"HELD", "UNKNOWN"}
                for claim in self._claims
            )
        )

    @contextmanager
    def _healthy_teardown_operation(self) -> Iterator[None]:
        with self._condition:
            if (
                self._state != AuthorityState.CLOSING
                or self._pending_invalidation is not None
                or self._forward_work_locked()
                or any(grant.resources for grant in self._grants.values())
                or self._checked_out_connections
            ):
                raise AuthorityError("healthy teardown grant is not available")
            grant = self._new_grant_locked("teardown")
            token = self._grant_context.set(grant)
        try:
            yield
        finally:
            with self._condition:
                grant.status = _GrantStatus.RELEASED
                self._publish_requested_poison_locked()
                self._condition.notify_all()
            self._grant_context.reset(token)

    def close(self) -> None:
        self._assert_pid()
        current = self._grant_context.get()
        if (
            current is not None
            and current.epoch == self._epoch
            and current.owner == self._application_owner()
            and current.status is not _GrantStatus.RELEASED
        ):
            raise AuthorityError("an effect owner cannot synchronously close itself")
        caller = threading.get_ident()
        with self._condition:
            while self._state in {
                AuthorityState.CLOSING,
                AuthorityState.RELEASING_LOGICAL,
            } or (
                self._close_owner is not None
            ):
                if self._close_owner == caller:
                    raise AuthorityError("close owner cannot re-enter data-root close")
                self._condition.wait()
            if self._state == AuthorityState.CLOSED:
                return
            if self._close_result is not None and self._state in {
                AuthorityState.POISONED,
                AuthorityState.FAILED_STARTUP,
                AuthorityState.FAILED_CLOSED,
            }:
                raise self._terminal_close_exception()
            if self._state in {AuthorityState.MIGRATING, AuthorityState.RECOVERING}:
                raise AuthorityError("cannot close while startup work is active")
            if self._state == AuthorityState.ACQUIRED and self._startup_grant is not None:
                if self._startup_grant.owner != self._application_owner():
                    raise AuthorityError("only the startup owner may close before READY")
                self._startup_grant.status = _GrantStatus.RELEASED
                self._startup_grant = None
            preexisting_failure = self._pending_invalidation is not None or self._state in {
                AuthorityState.POISONED,
                AuthorityState.FAILED_STARTUP,
                AuthorityState.FAILED_CLOSED,
            }
            self._state = AuthorityState.CLOSING
            self._close_owner = caller
            self._close_result = None
            while (
                self._forward_work_locked()
                or any(grant.resources for grant in self._grants.values())
                or self._checked_out_connections
            ):
                self._condition.wait()
            healthy = not preexisting_failure and self._pending_invalidation is None
        forward_failure: BaseException | None = None
        try:
            if healthy:
                with self._healthy_teardown_operation():
                    if self._store is not None and not getattr(self._store, "closed", False):
                        self._store._close_under_authority()
                    self._assert_current_forward()
                    if self._checked_out_connections:
                        raise AuthorityError(
                            "database connections remain checked out after close"
                        )
                    if self._main_claim is not None and self._main_claim.fd is not None:
                        database_claim = self._descendant_claims.get("database")
                        if database_claim is None or database_claim.fd is None:
                            raise AuthorityError(
                                "database directory claim is absent during close"
                            )
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
            elif self._store is not None and not getattr(self._store, "closed", False):
                try:
                    self._store._release_after_invalidation()
                except BaseException as exc:
                    forward_failure = exc
                    self._request_invalidation(
                        AuthorityState.FAILED_CLOSED,
                        "invalidated store resource release failed",
                    )
        except BaseException as exc:
            forward_failure = exc
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "healthy data-root teardown failed",
            )

        release_failure: BaseException | None = None
        try:
            self._close_physical_best_effort()
            if not self._physical_release_complete:
                raise AuthorityError("physical data-root release is incomplete")
            with self._condition:
                self._state = AuthorityState.RELEASING_LOGICAL
            self._close_logical_if_physical_released()
        except BaseException as exc:
            release_failure = exc
            self._request_invalidation(
                AuthorityState.FAILED_CLOSED,
                "terminal data-root resource release failed",
            )

        with self._condition:
            # A published runtime poison is an admission outcome, not proof
            # that physical release itself is uncertain.  Once invalidated,
            # skip all healthy forward checks, release existing bearers only,
            # and permit CLOSED when every release outcome is known.  UNKNOWN
            # claims still make ``release_failure`` terminal.
            failed = forward_failure is not None or release_failure is not None
        if failed:
            close_result = self._classified_close_failure_result()
            with self._condition:
                self._publish_requested_poison_locked()
                if self._state in {
                    AuthorityState.CLOSING,
                    AuthorityState.RELEASING_LOGICAL,
                }:
                    self._state = (
                        self._pending_invalidation or AuthorityState.FAILED_CLOSED
                    )
                self._close_result = close_result
                if self._failed_close_requires_terminal_pin():
                    self._pin_terminal()
                self._close_owner = None
                self._condition.notify_all()
            raise self._terminal_close_exception() from None
        with self._condition:
            self._state = AuthorityState.CLOSED
            self._pending_invalidation = None
            self._poison_requested = False
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
