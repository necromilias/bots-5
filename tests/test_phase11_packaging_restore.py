from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bots5.bootstrap import desktop
from tests import test_phase11_packaging as packaging
from tests.test_phase9_restore import (
    _prepare_desktop_root,
    _stored_max_output_tokens,
    restore_environment,
)


def test_source_restore_dispatch_uses_python_module(monkeypatch):
    monkeypatch.delattr(desktop, "__compiled__", raising=False)
    assert desktop._restore_bootstrap_command() == [sys.executable, "-m", "bots5.bootstrap.desktop"]


def test_compiled_restore_dispatch_uses_executable_reentry(monkeypatch):
    monkeypatch.setattr(desktop, "__compiled__", object(), raising=False)
    monkeypatch.setattr(sys, "executable", "/nonexistent/standalone/python")
    assert desktop._restore_bootstrap_command() == [os.readlink("/proc/self/exe")]


def _inventory(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def relocated_payload(tmp_path_factory):
    value = os.environ.get("BOTS5_STANDALONE_ROOT")
    if not value:
        pytest.skip("frozen restore proof requires the explicit M7 artifact")
    base = tmp_path_factory.mktemp("m7-readonly-restore")
    payload = base / "payload"
    shutil.copytree(value, payload, ignore=shutil.ignore_patterns("__pycache__"))
    before = _inventory(payload)
    for path in payload.rglob("*"):
        path.chmod(path.stat().st_mode & ~0o222)
    payload.chmod(payload.stat().st_mode & ~0o222)
    try:
        yield payload
        assert _inventory(payload) == before, "read-only payload bytes changed"
    finally:
        for path in payload.rglob("*"):
            if path.is_dir():
                path.chmod(path.stat().st_mode | 0o700)
        payload.chmod(payload.stat().st_mode | 0o700)
        shutil.rmtree(base)


def test_relocated_read_only_payload_starts(relocated_payload, tmp_path, monkeypatch):
    for name in ("bots5", "bots5-desktop"):
        case = tmp_path / name
        env = packaging._isolated_env(case)
        result = subprocess.run([str(relocated_payload / name), "--help"],
            cwd=case, env=env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
    # Do not overwrite the canonical payload's runtime resource record.
    monkeypatch.delenv("BOTS5_M7_EVIDENCE_DIR", raising=False)
    resources = tmp_path / "resources"
    packaging.test_packaged_runtime_resources_are_present(relocated_payload, resources)
    startup = tmp_path / "startup"
    startup.mkdir()
    packaging.test_desktop_starts_with_external_data_root_and_migrates(relocated_payload, startup)


@pytest.mark.parametrize("case,expected", [("success", 0), ("refusal", 2), ("fail_closed", 3)])
def test_actual_frozen_restore_handoff(case, expected, relocated_payload, tmp_path,
                                     restore_environment):
    env_case = restore_environment
    _prepare_desktop_root(env_case.root)
    environment = packaging._isolated_env(tmp_path / "isolated")
    config = {"case": case, "root": str(env_case.root), "package": str(env_case.package),
        "backup_id": env_case.backup_id, "trace": str(tmp_path / "trace.jsonl")}
    config_path = tmp_path / "restore-probe.json"
    config_path.write_text(json.dumps(config))
    environment["BOTS5_PACKAGING_CHECK"] = "restore-handoff"
    environment["BOTS5_PACKAGING_RESTORE_CONFIG"] = str(config_path)
    command = [str(relocated_payload / "bots5-desktop")]
    result = subprocess.run(command, cwd=tmp_path, env=environment, text=True,
                            capture_output=True, timeout=120)
    evidence = os.environ.get("BOTS5_M7_EVIDENCE_DIR")
    rows = [json.loads(line) for line in Path(config["trace"]).read_text().splitlines()]
    record = {"case": case, "command": command, "exit_status": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr, "trace": rows,
        "development_environment_removed": True, "payload_relocated_read_only": True,
        "uid": os.getuid()}
    if evidence:
        (Path(evidence) / f"validation/FROZEN_RESTORE_{case.upper()}.json").write_text(
            json.dumps(record, indent=2) + "\n")
    assert result.returncode == expected, result.stderr
    summary_line = next(line for line in result.stdout.splitlines()
                        if line.startswith("M7_RESTORE_PROBE_JSON:"))
    summary = json.loads(summary_line.split(":", 1)[1])
    assert summary["parent_exit_status"] == expected
    assert summary["while_live_refusal"]["status"] == 2
    assert summary["fresh_authority_after_close"] and summary["parent_restore_prohibited"]
    assert "-m" not in summary["child_argv"]
    spawn = next(row for row in rows if row["event"] == "parent_spawn_after_release")
    post_close = next(row for row in rows if row["event"] == "parent_close_returned")
    children = [row for row in rows if row["event"] == "child_bootstrap" and row["time_ns"] > spawn["time_ns"]]
    assert len(children) == 1 and children[0]["pid"] != summary["parent_pid"]
    assert children[0]["compiled_bootstrap"] and children[0]["qt_absent"]
    assert post_close["time_ns"] < spawn["time_ns"] < children[0]["time_ns"]
    child_pid = children[0]["pid"]
    child_events = [row["event"] for row in rows if row["pid"] == child_pid]
    assert child_events[:3] == ["child_bootstrap", "child_authority_acquired", "child_before_store"]
    assert child_events[-1] == "child_exit"
    output = summary["results"][0]
    receipt_file = env_case.root / "database/.bots5-restore-receipt.json"
    if case == "success":
        assert summary["real_restore_dialog"]
        assert "child_restore_service" in child_events
        receipt = json.loads(output["stdout"])
        assert receipt["outcome"] == "RESTORED" and receipt["backup_id"] == env_case.backup_id
        assert receipt["package_sha256"] == env_case.artifact_sha256
        assert receipt_file.read_bytes() == output["stdout"].encode()
        assert _stored_max_output_tokens(env_case.root) == 2222
        assert len(list((env_case.root / "retained-installations").iterdir())) == 1
    else:
        assert _stored_max_output_tokens(env_case.root) == 1024
        assert not receipt_file.exists()
        assert not list((env_case.root / "retained-installations").iterdir())
        if case == "refusal":
            assert "restore not committed" in output["stderr"] and "BackupArchiveInvalid" in output["stderr"]
        else:
            assert "restore failed closed" in output["stderr"] and "BackupUnclassifiedState" in output["stderr"]
            assert (env_case.root / "database/.bots5-restore-journal.json").read_bytes() == b'{"journal_version": 1, "restore_tr'
            assert "child_restore_service" not in child_events
