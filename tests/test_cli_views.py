from __future__ import annotations

import asyncio
import os

from bots5.cli import main
from bots5.manifest import load_job
from bots5.runner import run_job

from .helpers import FakeProvider, make_job_tree


def test_validate_makes_no_api_call_or_run_dir(tmp_path, capsys, monkeypatch):
    path, _ = make_job_tree(tmp_path)

    def provider_must_not_be_constructed(*args, **kwargs):
        raise AssertionError("validation must not construct a provider")

    monkeypatch.setattr("bots5.cli.OpenRouterProvider", provider_must_not_be_constructed)
    assert main(["validate", str(path)]) == 0
    assert not (tmp_path / ".bots5" / "runs").exists()


def test_missing_api_key_makes_no_run_dir(tmp_path, capsys):
    path, _ = make_job_tree(tmp_path)
    assert main(["run", str(path)]) == 1
    assert not (tmp_path / ".bots5" / "runs").exists()


def test_status_and_inspect_from_disk_only(tmp_path, capsys):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    result = asyncio.run(run_job(job, FakeProvider(), run_id="disk-run"))

    assert main(["status", "disk-run", "--runs-dir", str(job.output.runs_dir)]) == 0
    status_text = capsys.readouterr().out
    assert "state: succeeded" in status_text
    assert "w1:" in status_text

    assert main(["inspect", "disk-run", "w1", "--runs-dir", str(job.output.runs_dir)]) == 0
    inspect_text = capsys.readouterr().out
    assert "--- output ---" in inspect_text
    assert "output:model-w1" in inspect_text
