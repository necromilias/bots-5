from __future__ import annotations

import json

import pytest

from bots5.errors import FileValidationError, InvalidJsonError, ValidationError
from bots5.manifest import load_job, validate_referenced_files

from .helpers import make_job_tree


def test_valid_manifest(tmp_path):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    validate_referenced_files(job)
    assert job.schema_version == 1
    assert job.workers[0].id == "w1"


def test_schema_v2_validates_local_provider_config(tmp_path):
    path, job = make_job_tree(tmp_path, workers=1, synthesis=False)
    job["schema_version"] = 2
    job["workers"][0]["provider"] = "local_openai"
    job["providers"] = {
        "local_openai": {
            "base_url": "http://127.0.0.1:8000/v1/",
            "api_key_env": "LOCAL_OPENAI_API_KEY",
        }
    }
    path.write_text(json.dumps(job), encoding="utf-8")

    parsed = load_job(path)

    assert parsed.schema_version == 2
    assert parsed.providers.local_openai is not None
    assert parsed.providers.local_openai.base_url == "http://127.0.0.1:8000/v1"
    assert parsed.providers.local_openai.api_key_env == "LOCAL_OPENAI_API_KEY"


def test_schema_v2_requires_providers_top_level(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["schema_version"] = 2
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ValidationError, match="missing field.*providers"):
        load_job(path)


def test_schema_v1_rejects_providers_and_local_openai(tmp_path):
    path, job = make_job_tree(tmp_path, workers=1, synthesis=False)
    job["providers"] = {}
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown field.*providers"):
        load_job(path)

    job.pop("providers")
    job["workers"][0]["provider"] = "local_openai"
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ValidationError, match="unsupported provider"):
        load_job(path)


@pytest.mark.parametrize(
    "providers",
    [
        {"local_openai": {"base_url": "ftp://127.0.0.1:8000/v1"}},
        {"local_openai": {"base_url": "http://127.0.0.1:8000/v1?token=secret"}},
        {"local_openai": {"base_url": "http://127.0.0.1:8000/v1", "extra": True}},
        {"mystery": {"base_url": "http://127.0.0.1:8000/v1"}},
    ],
)
def test_schema_v2_rejects_invalid_provider_config(tmp_path, providers):
    path, job = make_job_tree(tmp_path, workers=1, synthesis=False)
    job["schema_version"] = 2
    job["workers"][0]["provider"] = "local_openai"
    job["providers"] = providers
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_job(path)


def test_schema_v2_requires_local_config_when_declared(tmp_path):
    path, job = make_job_tree(tmp_path, workers=1, synthesis=False)
    job["schema_version"] = 2
    job["workers"][0]["provider"] = "local_openai"
    job["providers"] = {}
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ValidationError, match="required.*local_openai"):
        load_job(path)


def test_malformed_json(tmp_path):
    path = tmp_path / "job.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(InvalidJsonError):
        load_job(path)


def test_nested_duplicate_key_rejected(tmp_path):
    path, job = make_job_tree(tmp_path)
    text = path.read_text()
    text = text.replace('"max_parallelism": 2,', '"max_parallelism": 2, "max_parallelism": 3,')
    path.write_text(text)
    with pytest.raises(InvalidJsonError):
        load_job(path)


def test_non_finite_json_rejected(tmp_path):
    path, _ = make_job_tree(tmp_path)
    text = path.read_text().replace('"temperature": 0.1', '"temperature": NaN', 1)
    path.write_text(text)
    with pytest.raises(InvalidJsonError):
        load_job(path)


def test_unsupported_schema(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["schema_version"] = 2
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_unknown_field_rejected(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["surprise"] = True
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_bool_not_accepted_as_integer(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["execution"]["max_parallelism"] = True
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_unsupported_provider(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["workers"][0]["provider"] = "mystery"
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_duplicate_worker_ids(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["workers"][1]["id"] = job["workers"][0]["id"]
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_synthesis_unknown_dependency(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["synthesis"]["depends_on"] = ["w1", "missing"]
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_unsafe_stage_id_rejected(tmp_path):
    path, job = make_job_tree(tmp_path)
    job["workers"][0]["id"] = "../oops"
    path.write_text(json.dumps(job))
    with pytest.raises(ValidationError):
        load_job(path)


def test_configured_runs_dir_symlink_rejected_before_resolution(tmp_path):
    path, job = make_job_tree(tmp_path)
    target = tmp_path / "actual-runs"
    target.mkdir()
    link = tmp_path / "linked-runs"
    link.symlink_to(target, target_is_directory=True)
    job["output"]["runs_dir"] = "./linked-runs"
    path.write_text(json.dumps(job))

    with pytest.raises(ValidationError, match="must not be a symlink"):
        load_job(path)


def test_missing_referenced_file(tmp_path):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    job.inputs[0].path.unlink()
    with pytest.raises(FileValidationError):
        validate_referenced_files(job)


def test_invalid_utf8_referenced_file(tmp_path):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    job.inputs[0].path.write_bytes(b"\xff")
    with pytest.raises(FileValidationError):
        validate_referenced_files(job)


def test_invalid_utf8_worker_contract_rejected(tmp_path):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    job.workers[0].system_prompt_path.write_bytes(b"\xff")
    with pytest.raises(FileValidationError, match="not valid UTF-8"):
        validate_referenced_files(job)


def test_invalid_worker_contract_rejected_by_file_validation(tmp_path):
    path, _ = make_job_tree(tmp_path)
    job = load_job(path)
    job.workers[0].system_prompt_path.write_text("TASK\nOnly a task.\n", encoding="utf-8")
    with pytest.raises(FileValidationError, match="missing section"):
        validate_referenced_files(job)
