"""Python ownership wrapper around the mandatory native rooted SQLite VFS."""

from __future__ import annotations

import _sqlite3
import ctypes
import ctypes.util
import os
import sqlite3
import urllib.parse
import uuid
from pathlib import Path

from bots5.core.errors import AuthorityError


class RootedVfsUnsupported(AuthorityError):
    pass


def _load_library() -> ctypes.CDLL:
    configured = os.environ.get("BOTS5_ROOTED_VFS_LIBRARY")
    candidates = [] if configured is None else [Path(configured)]
    project = Path(__file__).resolve().parents[3]
    candidates.extend(
        (
            project / "build" / "native" / "libbots5_rooted_sqlite_vfs.so",
            project / "build" / "libbots5_rooted_sqlite_vfs.so",
        )
    )
    candidates.extend(
        sorted(
            Path(__file__).with_name("native").glob("_rooted_sqlite_vfs*.so")
        )
    )
    native = next((candidate for candidate in candidates if candidate.is_file()), None)
    if native is None:
        raise RootedVfsUnsupported(
            "native rooted SQLite VFS is not built; run "
            "python -m bots5.infrastructure.native.build_rooted_vfs "
            "--output build/native/libbots5_rooted_sqlite_vfs.so"
        )
    sqlite_module_path = getattr(_sqlite3, "__file__", None)
    if sqlite_module_path is None:
        raise RootedVfsUnsupported(
            "active Python embeds a private SQLite and cannot accept the rooted VFS"
        )
    sqlite_name = ctypes.util.find_library("sqlite3")
    if sqlite_name is None:
        raise RootedVfsUnsupported("shared SQLite library is unavailable")
    module = ctypes.CDLL(sqlite_module_path)
    shared = ctypes.CDLL(sqlite_name)
    module_find = ctypes.cast(module.sqlite3_vfs_find, ctypes.c_void_p).value
    shared_find = ctypes.cast(shared.sqlite3_vfs_find, ctypes.c_void_p).value
    if module_find != shared_find:
        raise RootedVfsUnsupported(
            "Python _sqlite3 and the rooted VFS do not share one SQLite instance"
        )
    library = ctypes.CDLL(str(native), use_errno=True)
    library.bots5_last_error.restype = ctypes.c_char_p
    library.bots5_runtime_sqlite_matches.argtypes = []
    library.bots5_runtime_sqlite_matches.restype = ctypes.c_int
    library.bots5_vfs_register.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulonglong,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.bots5_vfs_register.restype = ctypes.c_int
    library.bots5_vfs_open_count.argtypes = [ctypes.c_char_p]
    library.bots5_vfs_open_count.restype = ctypes.c_int
    library.bots5_vfs_unregister.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ]
    library.bots5_vfs_unregister.restype = ctypes.c_int
    library.bots5_vfs_test_private_fd.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.bots5_vfs_test_private_fd.restype = ctypes.c_int
    library.bots5_vfs_test_inject_close_fault.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.bots5_vfs_test_inject_close_fault.restype = ctypes.c_int
    library.bots5_vfs_after_fork_child.argtypes = []
    library.bots5_vfs_after_fork_child.restype = None
    if library.bots5_runtime_sqlite_matches() != 0:
        value = library.bots5_last_error()
        raise RootedVfsUnsupported(
            "rooted SQLite VFS is bound to a different SQLite instance"
            if not value
            else value.decode("utf-8", "replace")
        )
    return library


_LIBRARY: ctypes.CDLL | None = None


def native_library() -> ctypes.CDLL:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = _load_library()
    return _LIBRARY


class RootedSQLiteVfs:
    """One registered VFS bound to one claimed main inode and fixed directories."""

    __slots__ = (
        "_library",
        "name",
        "synthetic",
        "_pid",
        "_closed",
        "_close_inventory",
    )

    _PRIVATE_LABELS = (
        "database-directory",
        "main-claim",
        "temp-directory",
    )
    _CLOSE_STATUS = {0: "HELD", 1: "RELEASED", 2: "UNKNOWN"}

    def __init__(
        self,
        *,
        database_dir_fd: int,
        main_claim_fd: int,
        temp_dir_fd: int,
        mount_id: int,
        main_leaf: str = "state.sqlite3",
        journal_leaf: str = "state.sqlite3-journal",
        intake_wal: bool = False,
    ) -> None:
        self._library = native_library()
        identity = uuid.uuid4().hex
        self.name = f"bots5vfs{identity}"
        self.synthetic = f"bots5db{identity}"
        self._pid = os.getpid()
        self._closed = False
        self._close_inventory = tuple(
            (label, "HELD") for label in self._PRIVATE_LABELS
        )
        result = self._library.bots5_vfs_register(
            self.name.encode("ascii"),
            self.synthetic.encode("ascii"),
            database_dir_fd,
            main_claim_fd,
            temp_dir_fd,
            main_leaf.encode("ascii"),
            journal_leaf.encode("ascii"),
            mount_id,
            self._pid,
            int(intake_wal),
        )
        if result != 0:
            raise RootedVfsUnsupported(self._error())

    def _error(self) -> str:
        value = self._library.bots5_last_error()
        return "rooted SQLite VFS failure" if not value else value.decode("utf-8", "replace")

    def _assert_live(self) -> None:
        if self._closed or os.getpid() != self._pid:
            raise AuthorityError("rooted SQLite VFS is closed or belongs to another process")

    def connect(self) -> sqlite3.Connection:
        self._assert_live()
        uri = (
            "file:"
            + urllib.parse.quote(self.synthetic, safe="")
            + "?mode=rw&vfs="
            + urllib.parse.quote(self.name, safe="")
        )
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        row = connection.execute("PRAGMA database_list").fetchone()
        if row is None or str(row[2]) != self.synthetic:
            connection.close()
            raise RootedVfsUnsupported("Python sqlite3 did not preserve the rooted synthetic name")
        return connection

    @property
    def open_count(self) -> int:
        self._assert_live()
        value = self._library.bots5_vfs_open_count(self.name.encode("ascii"))
        if value < 0:
            raise AuthorityError(self._error())
        return value

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_inventory(self) -> tuple[tuple[str, str], ...]:
        return self._close_inventory

    def close(self) -> None:
        self._assert_live()
        if self.open_count != 0:
            raise AuthorityError("rooted SQLite VFS still has open files")
        statuses = tuple(ctypes.c_int() for _ in self._PRIVATE_LABELS)
        result = self._library.bots5_vfs_unregister(
            self.name.encode("ascii"),
            *(ctypes.byref(status) for status in statuses),
        )
        try:
            self._close_inventory = tuple(
                (label, self._CLOSE_STATUS[status.value])
                for label, status in zip(self._PRIVATE_LABELS, statuses, strict=True)
            )
        except KeyError as exc:
            self._closed = True
            raise AuthorityError("rooted SQLite VFS returned an invalid close inventory") from exc
        if any(status.value != 0 for status in statuses):
            # Once native teardown has invalidated any private identity the
            # wrapper is irreversibly dead, including on an uncertain close.
            self._closed = True
        if result != 0:
            raise AuthorityError(self._error())
        self._closed = True

    def _test_private_fds(self) -> tuple[int, int, int]:
        """Return native duplicate numbers solely for deterministic fd-reuse tests."""
        self._assert_live()
        values = tuple(
            self._library.bots5_vfs_test_private_fd(self.name.encode("ascii"), slot)
            for slot in range(1, 4)
        )
        if any(value < 0 for value in values):
            raise AuthorityError(self._error())
        return values

    def _test_inject_close_fault(self, slot: int, mode: int) -> None:
        """Install a one-instance native teardown fault; unused in production."""
        self._assert_live()
        if self._library.bots5_vfs_test_inject_close_fault(
            self.name.encode("ascii"), slot, mode
        ) != 0:
            raise AuthorityError(self._error())

    def after_fork_child(self) -> None:
        self._library.bots5_vfs_after_fork_child()
        self._closed = True
        self._close_inventory = tuple(
            (label, "UNKNOWN") for label in self._PRIVATE_LABELS
        )
