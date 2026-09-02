from __future__ import annotations

import asyncio
import json
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


def test_run_summary_exposes_incomplete_completion(tmp_path, capsys, monkeypatch):
    path, _ = make_job_tree(tmp_path, workers=1, synthesis=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        "bots5.cli.OpenRouterProvider",
        lambda api_key: FakeProvider(finish_reasons={"model-w1": "length"}),
    )

    assert main(["run", str(path)]) == 1
    text = capsys.readouterr().out
    assert "state: failed" in text
    assert "w1: state=succeeded completion=incomplete finish_reason='length'" in text


def _local_v2_job(tmp_path, api_key_env="LOCAL_ONLY_KEY"):
    path, job = make_job_tree(tmp_path, workers=1, synthesis=False)
    job["schema_version"] = 2
    job["workers"][0]["provider"] = "local_openai"
    job["providers"] = {
        "local_openai": {
            "base_url": "http://127.0.0.1:8000/v1",
            **({"api_key_env": api_key_env} if api_key_env is not None else {}),
        }
    }
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def test_local_only_cli_does_not_require_openrouter_key(tmp_path, monkeypatch):
    path = _local_v2_job(tmp_path, api_key_env="LOCAL_ONLY_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_ONLY_KEY", "local-secret")
    monkeypatch.setattr(
        "bots5.cli.OpenRouterProvider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OpenRouter must not be constructed")
        ),
    )
    monkeypatch.setattr(
        "bots5.cli.OpenAICompatibleProvider",
        lambda base_url, *, api_key_env=None: FakeProvider(),
    )

    assert main(["run", str(path)]) == 0


def test_authenticated_local_cli_requires_only_configured_environment(tmp_path, monkeypatch):
    path = _local_v2_job(tmp_path, api_key_env="LOCAL_REQUIRED_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_REQUIRED_KEY", raising=False)

    assert main(["run", str(path)]) == 1
    assert not (tmp_path / ".bots5" / "runs").exists()


def test_status_and_inspect_from_disk_only(tmp_path, capsys):
    path, _ = make_job_tree(tmp_path, workers=1, synthesis=False)
    job = load_job(path)
    provider = FakeProvider(finish_reasons={"model-w1": "length"})
    result = asyncio.run(run_job(job, {"openrouter": provider}, run_id="disk-run"))

    assert main(["status", "disk-run", "--runs-dir", str(job.output.runs_dir)]) == 1
    status_text = capsys.readouterr().out
    assert "state: failed" in status_text
    assert "w1:" in status_text
    assert "completion=incomplete finish_reason='length'" in status_text

    assert main(["inspect", "disk-run", "w1", "--runs-dir", str(job.output.runs_dir)]) == 0
    inspect_text = capsys.readouterr().out
    assert "completion: incomplete" in inspect_text
    assert "finish_reason: 'length'" in inspect_text
    assert "--- output ---" in inspect_text
    assert "output:model-w1" in inspect_text
