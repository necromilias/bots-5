from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def standalone_root() -> Path:
    value = os.environ.get("BOTS5_STANDALONE_ROOT")
    if not value:
        pytest.skip("standalone proof is selected by scripts/ci/validate_standalone_build.sh")
    root = Path(value).resolve()
    assert root.is_dir(), f"standalone output does not exist: {root}"
    return root


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    xdg_data = tmp_path / "xdg-data"
    xdg_config = tmp_path / "xdg-config"
    xdg_state = tmp_path / "xdg-state"
    xdg_cache = tmp_path / "xdg-cache"
    runtime = tmp_path / "runtime"
    for path in (home, xdg_data, xdg_config, xdg_state, xdg_cache, runtime):
        path.mkdir(parents=True, mode=0o700)
    return {
        "PATH": os.defpath,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_STATE_HOME": str(xdg_state),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONNOUSERSITE": "1",
    }


def test_standalone_build_command_succeeded(standalone_root: Path) -> None:
    result_path = os.environ.get("BOTS5_M7_BUILD_RESULT")
    assert result_path, "build result record not supplied by validation script"
    result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    assert result["exit_status"] == 0
    assert result["artifact"] == str(standalone_root)
    assert result["environment_is_virtualenv"] is True


def test_expected_standalone_artifacts_exist(standalone_root: Path) -> None:
    for path in (
        standalone_root / "bots5",
        standalone_root / "bots5-desktop",
        standalone_root / "bots5.dist" / "entry_cli.bin",
        standalone_root / "bots5-desktop.dist" / "entry_desktop.bin",
    ):
        assert path.is_file() and os.access(path, os.X_OK), path


@pytest.mark.parametrize("command", ["bots5"])
def test_cli_launches_without_development_python_path(
    standalone_root: Path, tmp_path: Path, command: str
) -> None:
    env = _isolated_env(tmp_path)
    result = subprocess.run(
        [str(standalone_root / command), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "PYTHONPATH" not in env and "VIRTUAL_ENV" not in env


def test_desktop_entrypoint_launches_without_development_python_path(
    standalone_root: Path, tmp_path: Path
) -> None:
    env = _isolated_env(tmp_path)
    result = subprocess.run(
        [str(standalone_root / "bots5-desktop"), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--data-root" in result.stdout
    assert "PYTHONPATH" not in env and "VIRTUAL_ENV" not in env


def test_packaged_runtime_resources_are_present(
    standalone_root: Path, tmp_path: Path
) -> None:
    desktop = standalone_root / "bots5-desktop.dist"
    assert (desktop / "libbots5_rooted_sqlite_vfs.so").is_file()
    migration_dir = desktop / "bots5/infrastructure/persistence/migrations"
    assert (migration_dir / "env.py").is_file()
    assert (migration_dir / "script.py.mako").is_file()
    versions = migration_dir / "versions"
    migration_files = sorted(versions.glob("[0-9]*_*.py"))
    assert len(migration_files) >= 20
    assert (versions / "0020_provider_managed_context.py").is_file()
    env = _isolated_env(tmp_path)
    env["BOTS5_PACKAGING_CHECK"] = "1"
    result = subprocess.run(
        [str(standalone_root / "bots5-desktop")], cwd=tmp_path, env=env,
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    assert probe["shared_sqlite_matches"] is True
    assert Path(probe["sqlite_extension"]).is_relative_to(desktop)
    assert Path(probe["native_vfs"]).is_relative_to(desktop)
    assert Path(probe["migrations"]) == migration_dir
    assert all(probe["icons"].values())
    assert probe["action_icon_renders"] is True
    assert probe["stylesheet_bytes"] > 1000
    assert probe["secret_service_backend"] == "keyring.backends.SecretService.Keyring"
    print("Packaged runtime probe:", json.dumps(probe, sort_keys=True))
    evidence = os.environ.get("BOTS5_M7_EVIDENCE_DIR")
    if evidence:
        (Path(evidence) / "validation/PACKAGED_RUNTIME_PROBE.json").write_text(
            json.dumps(probe, indent=2) + "\n", encoding="utf-8"
        )


def test_desktop_starts_with_external_data_root_and_migrates(
    standalone_root: Path, tmp_path: Path
) -> None:
    # Keep one selector ID and exercise both accepted topology choices.
    for explicit in (True, False):
        case_path = tmp_path / ("explicit" if explicit else "xdg-default")
        case_path.mkdir()
        _assert_desktop_startup(standalone_root, case_path, explicit=explicit)


def _assert_desktop_startup(
    standalone_root: Path, tmp_path: Path, *, explicit: bool
) -> None:
    env = _isolated_env(tmp_path)
    data_root = (tmp_path / "operator-selected-root" if explicit
                 else Path(env["XDG_DATA_HOME"]) / "bots5")
    command = [str(standalone_root / "bots5-desktop")]
    if explicit:
        command.extend(("--data-root", str(data_root)))
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    db_path = data_root / "database/state.sqlite3"
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and process.poll() is None:
            if db_path.is_file():
                try:
                    with sqlite3.connect(db_path, timeout=0.5) as connection:
                        row = connection.execute(
                            "SELECT version_num FROM alembic_version"
                        ).fetchone()
                        windows = connection.execute(
                            "SELECT count(*) FROM workspace_windows "
                            "WHERE selected_chat_id IS NOT NULL AND restore_open = 1"
                        ).fetchone()[0]
                    if row == ("0020_provider_managed_context",) and windows:
                        break
                except sqlite3.Error:
                    pass
            time.sleep(0.25)
        # initialize() persists this row at its end, then open_window() calls
        # show(). Allow the event loop to continue rather than accepting only
        # the earlier migration commit as a successful desktop launch.
        time.sleep(0.5)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"desktop exited during startup ({process.returncode}):\n{stdout}\n{stderr}")
        assert db_path.is_file(), "standalone startup did not create the data-root database"
        with sqlite3.connect(db_path, timeout=1) as connection:
            current = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            windows = connection.execute(
                "SELECT count(*) FROM workspace_windows "
                "WHERE selected_chat_id IS NOT NULL AND restore_open = 1"
            ).fetchone()[0]
        assert current == ("0020_provider_managed_context",)
        assert windows > 0, "desktop did not finish window initialization"
        assert data_root.stat().st_mode & 0o777 == 0o700
        if explicit:
            assert all((data_root / name).is_dir() for name in ("config", "state", "cache"))
            assert (data_root / "state/logs").is_dir()
            # resolve_app_paths() owns the bots5 namespace. Qt/font libraries
            # may use their own XDG caches even with an explicit application
            # data root; those are not B.O.T.S. state or credential stores.
            assert not any((Path(env[name]) / "bots5").exists() for name in (
                "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"
            )), "explicit --data-root should own the XDG application directories"
        else:
            assert all((Path(env[name]) / "bots5").is_dir() for name in (
                "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"
            ))
            assert (Path(env["XDG_STATE_HOME"]) / "bots5/logs").is_dir()
        print("Packaged desktop startup:", "explicit" if explicit else "XDG default", current)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
