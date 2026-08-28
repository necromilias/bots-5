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
