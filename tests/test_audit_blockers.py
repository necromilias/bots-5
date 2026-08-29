from __future__ import annotations

import asyncio
import json

import pytest

from bots5.errors import Bots5Error
from bots5.manifest import load_job
from bots5.runner import run_job

from .helpers import FakeProvider, make_job_tree


def test_unexpected_orchestration_error_persists_terminal_failed_run(tmp_path, monkeypatch):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    provider = FakeProvider()

    def explode(_dependencies):
        raise RuntimeError("synthetic orchestration failure")

    monkeypatch.setattr("bots5.runner.render_synthesis_user_message", explode)

    with pytest.raises(Bots5Error, match="run failed with internal error"):
        asyncio.run(run_job(job, provider, run_id="internal-error-run"))

    run_dir = job.output.runs_dir / "internal-error-run"
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["state"] == "failed"
    assert run["ended_at"] is not None
    assert all(stage["state"] != "running" for stage in run["stages"].values())


def test_input_reread_failure_does_not_create_running_run_tree(tmp_path, monkeypatch):
    path, _ = make_job_tree(tmp_path, workers=1, synthesis=False)
    job = load_job(path)
    provider = FakeProvider()
    original_read_bytes = type(job.inputs[0].path).read_bytes

    def fail_input_read(path_obj):
        if path_obj == job.inputs[0].path:
            raise OSError("synthetic reread failure")
        return original_read_bytes(path_obj)

    monkeypatch.setattr(type(job.inputs[0].path), "read_bytes", fail_input_read)

    with pytest.raises(OSError, match="synthetic reread failure"):
        asyncio.run(run_job(job, provider, run_id="must-not-exist"))

    assert not (job.output.runs_dir / "must-not-exist").exists()
