from __future__ import annotations

import fcntl
import os
from pathlib import Path

from bots5.core.errors import AuthorityError


class AuthorityLock:
    """Own one authoritative data root for this process lifetime."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> AuthorityLock:
        if self._fd is not None:
            raise AuthorityError(f"authority lock already acquired: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise AuthorityError(f"cannot open authority lock: {self.path}: {exc}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AuthorityError(f"data root is already owned: {self.path.parent}") from None
        except OSError as exc:
            os.close(fd)
            raise AuthorityError(f"cannot acquire authority lock: {self.path}: {exc}") from None
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> AuthorityLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
