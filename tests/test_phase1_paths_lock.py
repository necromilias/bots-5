from __future__ import annotations

import subprocess
import sys

import pytest

from bots5.core.errors import AuthorityError
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.authority_lock import AuthorityLock


def test_override_paths_keep_the_complete_test_data_boundary(tmp_path):
    paths = resolve_app_paths(tmp_path / "data")
    assert paths.database == paths.data_root / "state.sqlite3"
    assert paths.authority_lock == paths.data_root / "authority.lock"
    assert paths.config_root == paths.data_root / "config"
    paths.ensure()
    assert paths.data_root.is_dir()


def test_second_process_cannot_acquire_the_same_authority_lock(tmp_path):
    paths = resolve_app_paths(tmp_path / "data")
    first = AuthorityLock(paths.authority_lock).acquire()
    try:
        code = (
            "from bots5.infrastructure.authority_lock import AuthorityLock; "
            f"AuthorityLock({str(paths.authority_lock)!r}).acquire()"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "already owned" in result.stderr
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release(tmp_path):
    path = resolve_app_paths(tmp_path / "data").authority_lock
    first = AuthorityLock(path).acquire()
    first.release()
    second = AuthorityLock(path).acquire()
    second.release()

