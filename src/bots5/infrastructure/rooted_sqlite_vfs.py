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
from typing import Any

from bots5.core.errors import AuthorityError


class RootedVfsUnsupported(AuthorityError):
    pass


class RootedVfsRegistrationCloseUnknown(RootedVfsUnsupported):
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
    library.bots5_vfs_unknown_close_generation.argtypes = [ctypes.c_char_p]
    library.bots5_vfs_unknown_close_generation.restype = ctypes.c_uint
    library.bots5_vfs_thread_unknown_close_generation.argtypes = [ctypes.c_char_p]
    library.bots5_vfs_thread_unknown_close_generation.restype = ctypes.c_uint
    library.bots5_vfs_unregister.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
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
    library.bots5_vfs_test_reset_trace.argtypes = [ctypes.c_char_p]
    library.bots5_vfs_test_reset_trace.restype = ctypes.c_int
    library.bots5_vfs_test_trace.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    library.bots5_vfs_test_trace.restype = ctypes.c_int
    library.bots5_vfs_test_inject_io_fault.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.bots5_vfs_test_inject_io_fault.restype = ctypes.c_int
    library.bots5_vfs_test_delete_journal.argtypes = [ctypes.c_char_p]
    library.bots5_vfs_test_delete_journal.restype = ctypes.c_int
    library.bots5_vfs_test_inject_untracked_close_fault.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.bots5_vfs_test_inject_untracked_close_fault.restype = ctypes.c_int
    library.bots5_vfs_test_last_untracked_fd.argtypes = [ctypes.c_char_p]
    library.bots5_vfs_test_last_untracked_fd.restype = ctypes.c_int
    library.bots5_vfs_test_open_cleanup_path.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    library.bots5_vfs_test_open_cleanup_path.restype = ctypes.c_int
    library.bots5_vfs_test_inject_registration_cleanup_fault.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.bots5_vfs_test_inject_registration_cleanup_fault.restype = ctypes.c_int
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


class _RootedCursor(sqlite3.Cursor):
    """DB-API cursor that hands native resource facts back before returning."""

    def __init__(self, connection: "_RootedConnection") -> None:
        super().__init__(connection)
        self._bots5_connection = connection
        self._bots5_closed = False
        connection._bots5_register_cursor(self)

    def _bots5_forward(self, operation: str, method, *args, **kwargs):
        connection = self._bots5_connection
        connection._bots5_before_effect()
        try:
            return method(*args, **kwargs)
        finally:
            connection._bots5_handoff(operation)

    def execute(self, *args, **kwargs):
        return self._bots5_forward("cursor execute", super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._bots5_forward(
            "cursor executemany", super().executemany, *args, **kwargs
        )

    def executescript(self, *args, **kwargs):
        return self._bots5_forward(
            "cursor executescript", super().executescript, *args, **kwargs
        )

    def fetchone(self, *args, **kwargs):
        return self._bots5_forward(
            "cursor fetchone", super().fetchone, *args, **kwargs
        )

    def fetchmany(self, *args, **kwargs):
        return self._bots5_forward(
            "cursor fetchmany", super().fetchmany, *args, **kwargs
        )

    def fetchall(self, *args, **kwargs):
        return self._bots5_forward(
            "cursor fetchall", super().fetchall, *args, **kwargs
        )

    def __next__(self):
        return self._bots5_forward("cursor next", super().__next__)

    def close(self) -> None:
        # Cursor finalization is release-only: a revoked owner must still be
        # able to settle its existing statement without minting forward work.
        if self._bots5_closed:
            return
        connection = self._bots5_connection
        error: BaseException | None = None
        handoff_error: BaseException | None = None
        settled = False
        try:
            super().close()
            settled = True
        except BaseException as exc:
            error = exc
            connection._bots5_vfs._report_database_uncertainty(
                "cursor close outcome"
            )
        finally:
            try:
                connection._bots5_handoff("cursor close")
            except BaseException as exc:
                handoff_error = exc
            finally:
                if settled:
                    self._bots5_closed = True
                    connection._bots5_release_cursor(self)
        if error is not None:
            if handoff_error is not None:
                raise error from handoff_error
            raise error
        if handoff_error is not None:
            raise handoff_error


class _RootedConnection(sqlite3.Connection):
    """Connection whose authority resource lasts through consequential close."""

    def _bots5_bind(self, vfs: "RootedSQLiteVfs", lease, generation: int) -> None:
        self._bots5_vfs = vfs
        self._bots5_lease = lease
        self._bots5_generation = generation
        self._bots5_released = False
        self._bots5_closing = False
        self._bots5_live_cursors: dict[int, _RootedCursor] = {}

    def _bots5_before_effect(self) -> None:
        if getattr(self, "_bots5_released", True):
            raise AuthorityError("database connection authority has been released")
        if self._bots5_closing:
            raise AuthorityError("database connection is closing")
        self._bots5_lease.assert_forward()

    def _bots5_register_cursor(self, cursor: _RootedCursor) -> None:
        # Cursor creation is a forward child-resource acquisition.  Retain a
        # strong parent reference until explicit release-only settlement so
        # neither Python destruction timing nor an unread result can outlive
        # the connection's authority resource unnoticed.
        self._bots5_before_effect()
        self._bots5_live_cursors[id(cursor)] = cursor

    def _bots5_release_cursor(self, cursor: _RootedCursor) -> None:
        retained = self._bots5_live_cursors.get(id(cursor))
        if retained is cursor:
            del self._bots5_live_cursors[id(cursor)]

    def _bots5_drain_cursors(self) -> None:
        first_error: BaseException | None = None
        for cursor in tuple(self._bots5_live_cursors.values()):
            try:
                cursor.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        if self._bots5_live_cursors:
            self._bots5_vfs._report_database_uncertainty(
                "child cursor ownership settlement"
            )
            raise AuthorityError("database child cursor ownership did not settle")

    def _bots5_handoff(self, operation: str) -> None:
        generation = self._bots5_vfs._handoff_unknown_since(
            self._bots5_generation, operation
        )
        self._bots5_generation = generation

    def cursor(self, *args, **kwargs):
        self._bots5_before_effect()
        requested_factories = []
        if args:
            requested_factories.append(args[0])
        if "factory" in kwargs:
            requested_factories.append(kwargs["factory"])
        if any(factory is not _RootedCursor for factory in requested_factories):
            raise AuthorityError("raw database cursor factories are not permitted")
        if not requested_factories:
            kwargs["factory"] = _RootedCursor
        cursor = super().cursor(*args, **kwargs)
        self._bots5_handoff("cursor creation")
        return cursor

    def _bots5_cursor_execute(self, method_name: str, *args, **kwargs):
        cursor = self.cursor()
        try:
            return getattr(cursor, method_name)(*args, **kwargs)
        except BaseException:
            cursor.close()
            raise

    def execute(self, *args, **kwargs):
        return self._bots5_cursor_execute("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._bots5_cursor_execute("executemany", *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._bots5_cursor_execute("executescript", *args, **kwargs)

    def commit(self) -> None:
        self._bots5_before_effect()
        try:
            super().commit()
        except BaseException:
            self._bots5_vfs._report_database_uncertainty("commit outcome")
            raise
        finally:
            self._bots5_handoff("commit")

    def rollback(self) -> None:
        # Rollback is permitted as cleanup after the owning grant is revoked.
        try:
            super().rollback()
        except BaseException:
            self._bots5_vfs._report_database_uncertainty("rollback outcome")
            raise
        finally:
            self._bots5_handoff("rollback")

    def close(self) -> None:
        if getattr(self, "_bots5_released", True):
            return
        self._bots5_closing = True
        try:
            self._bots5_drain_cursors()
        except BaseException:
            # A child-settlement failure leaves the underlying connection and
            # its lease owned for a later release-only retry.  In particular,
            # do not skip straight to the parent close/release path merely
            # because another child happened to settle successfully.
            self._bots5_closing = False
            raise
        error: BaseException | None = None
        try:
            super().close()
        except BaseException as exc:
            error = exc
            self._bots5_vfs._report_database_uncertainty("connection close outcome")
        finally:
            try:
                self._bots5_handoff("connection close")
            finally:
                self._bots5_released = True
                lease = self._bots5_lease
                self._bots5_lease = None
                lease.release()
        if error is not None:
            raise error


class RootedSQLiteVfs:
    """One registered VFS bound to one claimed main inode and fixed directories."""

    __slots__ = (
        "_library",
        "name",
        "synthetic",
        "_pid",
        "_closed",
        "_close_inventory",
        "_authority",
        "_resource_key",
    )

    _PRIVATE_LABELS = (
        "database-directory",
        "main-claim",
        "temp-directory",
        "native-open-files",
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
        authority: Any | None = None,
        resource_label: str | None = None,
    ) -> None:
        self._library = native_library()
        identity = uuid.uuid4().hex
        self.name = f"bots5vfs{identity}"
        self.synthetic = f"bots5db{identity}"
        self._pid = os.getpid()
        self._closed = False
        self._authority = authority
        self._resource_key = self.name if resource_label is None else resource_label
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
        if result == -2:
            self._closed = True
            if authority is not None:
                authority._record_vfs_registration_close_unknown(self._resource_key)
            raise RootedVfsRegistrationCloseUnknown(
                "rooted VFS registration cleanup incomplete"
            )
        if result != 0:
            raise RootedVfsUnsupported(self._error())
        if authority is not None:
            authority._register_vfs_instance(self._resource_key, self)

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
        lease = None
        if self._authority is not None:
            lease = self._authority._acquire_database_resource(self._resource_key)
        generation: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            generation = self._thread_unknown_close_generation
            connection = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                factory=(
                    _RootedConnection
                    if self._authority is not None
                    else sqlite3.Connection
                ),
            )
            if lease is not None:
                connection._bots5_bind(self, lease, generation)
            cursor = connection.execute("PRAGMA database_list")
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None or str(row[2]) != self.synthetic:
                connection.close()
                raise RootedVfsUnsupported(
                    "Python sqlite3 did not preserve the rooted synthetic name"
                )
            return connection
        except BaseException:
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    pass
            if generation is not None:
                self._handoff_unknown_since(generation, "connection creation")
            if lease is not None and not lease.released:
                lease.release()
            raise

    @property
    def open_count(self) -> int:
        self._assert_live()
        value = self._library.bots5_vfs_open_count(self.name.encode("ascii"))
        if value < 0:
            raise AuthorityError(self._error())
        return value

    @property
    def unknown_close_generation(self) -> int:
        self._assert_live()
        value = int(
            self._library.bots5_vfs_unknown_close_generation(
                self.name.encode("ascii")
            )
        )
        if value == ctypes.c_uint(-1).value:
            raise AuthorityError(self._error())
        return value

    @property
    def _thread_unknown_close_generation(self) -> int:
        self._assert_live()
        value = int(
            self._library.bots5_vfs_thread_unknown_close_generation(
                self.name.encode("ascii")
            )
        )
        if value == ctypes.c_uint(-1).value:
            raise AuthorityError(self._error())
        return value

    def _assert_forward(self) -> None:
        if self._authority is not None:
            self._authority._assert_current_forward()

    def _report_database_uncertainty(self, operation: str) -> None:
        if self._authority is not None:
            self._authority._request_invalidation(
                "POISONED", f"uncertain database {operation}"
            )

    def _handoff_unknown_since(self, generation: int, operation: str) -> int:
        current = self._thread_unknown_close_generation
        if current != generation and self._authority is not None:
            self._authority._record_native_vfs_unknown(
                self._resource_key, operation, current
            )
        return current

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
            self._close_inventory = tuple(
                (label, "UNKNOWN") for label in self._PRIVATE_LABELS
            )
            if self._authority is not None:
                self._authority._merge_vfs_close_inventory(
                    self._resource_key, self._close_inventory
                )
            raise AuthorityError("rooted SQLite VFS returned an invalid close inventory") from exc
        if any(status.value != 0 for status in statuses):
            # Once native teardown has invalidated any private identity the
            # wrapper is irreversibly dead, including on an uncertain close.
            self._closed = True
        if result != 0:
            if self._authority is not None:
                self._authority._merge_vfs_close_inventory(
                    self._resource_key, self._close_inventory
                )
            raise AuthorityError(self._error())
        self._closed = True
        if self._authority is not None:
            self._authority._merge_vfs_close_inventory(
                self._resource_key, self._close_inventory
            )

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

    def _test_reset_trace(self) -> None:
        self._assert_live()
        if self._library.bots5_vfs_test_reset_trace(self.name.encode("ascii")) != 0:
            raise AuthorityError(self._error())

    def _test_trace(self) -> tuple[int, ...]:
        self._assert_live()
        events = (ctypes.c_int * 256)()
        count = self._library.bots5_vfs_test_trace(
            self.name.encode("ascii"), events, len(events)
        )
        if count < 0:
            raise AuthorityError(self._error())
        return tuple(int(events[index]) for index in range(count))

    def _test_inject_io_fault(self, event: int) -> None:
        self._assert_live()
        if self._library.bots5_vfs_test_inject_io_fault(
            self.name.encode("ascii"), int(event)
        ) != 0:
            raise AuthorityError(self._error())

    def _test_delete_journal(self) -> int:
        """Invoke the registered VFS journal-delete path for deterministic tests."""
        self._assert_live()
        return int(
            self._library.bots5_vfs_test_delete_journal(self.name.encode("ascii"))
        )

    def _test_inject_untracked_close_fault(self, site: int, mode: int) -> None:
        self._assert_live()
        if self._library.bots5_vfs_test_inject_untracked_close_fault(
            self.name.encode("ascii"), int(site), int(mode)
        ) != 0:
            raise AuthorityError(self._error())

    def _test_open_cleanup_path(self, site: int) -> int:
        self._assert_live()
        return int(
            self._library.bots5_vfs_test_open_cleanup_path(
                self.name.encode("ascii"), int(site)
            )
        )

    def _test_last_untracked_fd(self) -> int:
        self._assert_live()
        value = int(
            self._library.bots5_vfs_test_last_untracked_fd(
                self.name.encode("ascii")
            )
        )
        if value < 0:
            raise AuthorityError(self._error())
        return value

    def after_fork_child(self) -> None:
        self._library.bots5_vfs_after_fork_child()
        self._closed = True
        self._close_inventory = tuple(
            (label, "UNKNOWN") for label in self._PRIVATE_LABELS
        )


def _test_inject_registration_cleanup_fault(slot: int, mode: int) -> None:
    library = native_library()
    if library.bots5_vfs_test_inject_registration_cleanup_fault(
        int(slot), int(mode)
    ) != 0:
        value = library.bots5_last_error()
        raise AuthorityError(
            "rooted VFS registration cleanup fault injection failed"
            if not value
            else value.decode("utf-8", "replace")
        )
