"""Minimal ctypes binding to a scratch-built libsqlite3 for compatibility replay.

Loads an isolated SQLite shared library (never the host library) so that
version-specific parser behaviour can be exercised without mutating the host.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

SQLITE_OK = 0
SQLITE_ROW = 100
SQLITE_DONE = 101


class Sqlite:
    def __init__(self, library: Path):
        self.lib = ctypes.CDLL(str(library))
        L = self.lib
        L.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        L.sqlite3_open.restype = ctypes.c_int
        L.sqlite3_close.argtypes = [ctypes.c_void_p]
        L.sqlite3_close.restype = ctypes.c_int
        L.sqlite3_exec.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p),
        ]
        L.sqlite3_exec.restype = ctypes.c_int
        L.sqlite3_errmsg.argtypes = [ctypes.c_void_p]
        L.sqlite3_errmsg.restype = ctypes.c_char_p
        L.sqlite3_libversion.restype = ctypes.c_char_p
        L.sqlite3_free.argtypes = [ctypes.c_void_p]
        self.handle = ctypes.c_void_p()

    def version(self) -> str:
        return self.lib.sqlite3_libversion().decode()

    def open(self, path: str) -> None:
        rc = self.lib.sqlite3_open(path.encode(), ctypes.byref(self.handle))
        if rc != SQLITE_OK:
            raise RuntimeError(f"open failed: {self.error()}")

    def error(self) -> str:
        return self.lib.sqlite3_errmsg(self.handle).decode(errors="replace")

    def execute(self, sql: str) -> tuple[int, str]:
        err = ctypes.c_char_p()
        rc = self.lib.sqlite3_exec(
            self.handle, sql.encode(), None, None, ctypes.byref(err)
        )
        message = ""
        if err.value:
            message = err.value.decode(errors="replace")
            self.lib.sqlite3_free(err)
        return rc, message

    def close(self) -> None:
        if self.handle:
            self.lib.sqlite3_close(self.handle)
            self.handle = ctypes.c_void_p()

    def query(self, sql: str, parameters: tuple = ()) -> tuple[list[tuple], str]:
        """Run one possibly-parameterised statement and return its text rows."""
        L = self.lib
        L.sqlite3_prepare_v2.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_char_p),
        ]
        L.sqlite3_prepare_v2.restype = ctypes.c_int
        L.sqlite3_step.argtypes = [ctypes.c_void_p]
        L.sqlite3_step.restype = ctypes.c_int
        L.sqlite3_column_count.argtypes = [ctypes.c_void_p]
        L.sqlite3_column_count.restype = ctypes.c_int
        L.sqlite3_column_text.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.sqlite3_column_text.restype = ctypes.c_char_p
        L.sqlite3_bind_text.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p,
        ]
        L.sqlite3_finalize.argtypes = [ctypes.c_void_p]
        statement = ctypes.c_void_p()
        rc = L.sqlite3_prepare_v2(self.handle, sql.encode(), -1, ctypes.byref(statement), None)
        if rc != 0:
            return [], self.error()
        try:
            for position, value in enumerate(parameters, start=1):
                if value is None:
                    L.sqlite3_bind_null(statement, position)
                else:
                    encoded = str(value).encode()
                    L.sqlite3_bind_text(statement, position, encoded, len(encoded), None)
            rows = []
            columns = L.sqlite3_column_count(statement)
            while True:
                rc = L.sqlite3_step(statement)
                if rc == SQLITE_ROW:
                    rows.append(
                        tuple(
                            None if L.sqlite3_column_text(statement, index) is None
                            else L.sqlite3_column_text(statement, index).decode(errors="replace")
                            for index in range(columns)
                        )
                    )
                elif rc == SQLITE_DONE:
                    return rows, ""
                else:
                    return rows, self.error()
        finally:
            L.sqlite3_finalize(statement)
