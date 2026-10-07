"""Exercise DEP-01 against actual relocated frozen payloads, without live providers."""
from __future__ import annotations

import os
import tempfile
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_phase11_packaging as packaging
from tests import test_phase11_packaging_restore as restore
from tests.test_phase9_restore import restore_environment

standalone_root = packaging.standalone_root


@pytest.fixture
def payload_copy(tmp_path):
    """Remove each disposable bundle after its case, including failed cases."""
    created = []

    def copy(source, target, *, read_only=False):
        assert target.is_relative_to(tmp_path)
        copy_root = os.environ.get("BOTS5_PAYLOAD_COPY_ROOT")
        if copy_root:
            base = Path(tempfile.mkdtemp(prefix="unicode-payload-", dir=copy_root))
            created.append(base)
            target = base / target.relative_to(tmp_path)
        else:
            created.append(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        if read_only:
            for path in target.rglob("*"):
                path.chmod(path.stat().st_mode & ~0o222)
            target.chmod(target.stat().st_mode & ~0o222)
        return target

    yield copy
    for root in created:
        if root.exists():
            for path in root.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
            root.chmod(root.stat().st_mode | 0o700)
            shutil.rmtree(root)


@pytest.mark.parametrize("relative,rename,locale", [
    ("ascii/application", False, "C.UTF-8"),
    ("ascii/renamed", True, "C.UTF-8"),
    ("accent-é/application", False, "C.UTF-8"),
    ("home/用户/Applications/BOTS", False, "C.UTF-8"),
    ("home/用户/应用-é", True, "C"),
])
def test_frozen_paths_launch_and_load_resources(
    standalone_root: Path, tmp_path: Path, monkeypatch, payload_copy, relative, rename, locale
) -> None:
    payload = tmp_path / relative
    payload = payload_copy(standalone_root, payload)
    for app, original in (("bots5", "entry_cli.bin"), ("bots5-desktop", "entry_desktop.bin")):
        if rename:
            image = payload / f"{app}.dist" / original
            image.rename(image.with_name("可执行-é.bin"))
            launcher = payload / app
            launcher.write_text(launcher.read_text().replace(original, "可执行-é.bin"))
        env = packaging._isolated_env(tmp_path / f"help-{app}")
        env.update(LANG=locale, LC_ALL=locale)
        result = subprocess.run([str(payload / app), "--help"], env=env,
                                cwd=tmp_path, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout.lower()
    # Existing resource and startup assertions cover real native imports,
    # migrations, Qt resources, and both explicit/default disposable roots.
    monkeypatch.delenv("BOTS5_M7_EVIDENCE_DIR", raising=False)
    original_env = packaging._isolated_env
    def isolated_env(path):
        env = original_env(path)
        env.update(LANG=locale, LC_ALL=locale)
        return env
    monkeypatch.setattr(packaging, "_isolated_env", isolated_env)
    packaging.test_packaged_runtime_resources_are_present(payload, tmp_path / "resources")
    startup = tmp_path / "startup"
    startup.mkdir()
    packaging.test_desktop_starts_with_external_data_root_and_migrates(payload, startup)


@pytest.mark.parametrize("case,expected", [("success", 0), ("refusal", 2), ("fail_closed", 3)])
def test_frozen_restore_reentry_under_unicode_ancestors(
    standalone_root: Path, tmp_path: Path, monkeypatch, payload_copy,
    restore_environment, case, expected
) -> None:
    payload = tmp_path / "home" / "用户-é" / "Applications" / "BOTS"
    payload = payload_copy(standalone_root, payload, read_only=True)
    monkeypatch.delenv("BOTS5_M7_EVIDENCE_DIR", raising=False)
    restore.test_actual_frozen_restore_handoff(case, expected, payload, tmp_path,
                                             restore_environment)
