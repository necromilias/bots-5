from __future__ import annotations

import json
import math
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import FileValidationError, InvalidJsonError, ValidationError
from .models import (
    ExecutionLimits,
    InputSpec,
    Job,
    OutputConfig,
    SynthesisSpec,
    WorkerSpec,
)
from .paths import resolve_job_relative, validate_runs_dir, validate_stage_id
from .prompts import parse_worker_contract


TOP_KEYS = {"schema_version", "name", "inputs", "execution", "workers", "synthesis", "output"}
INPUT_KEYS = {"label", "path"}
EXEC_KEYS = {
    "max_parallelism",
    "run_timeout_seconds",
    "stop_before_synthesis_if_known_cost_exceeds_usd",
}
WORKER_KEYS = {
    "id",
    "provider",
    "model",
    "system_prompt_path",
    "temperature",
    "max_output_tokens",
    "timeout_seconds",
}
SYNTH_KEYS = WORKER_KEYS | {"depends_on"}
OUTPUT_KEYS = {"runs_dir"}
PROVIDERS = {"openrouter"}


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InvalidJsonError(f"duplicate key: {key!r}")
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise InvalidJsonError(f"non-finite JSON number is not allowed: {value}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InvalidJsonError(f"cannot read job file: {path}: {exc.strerror or exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidJsonError(f"job file is not valid UTF-8: {path}") from exc
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except InvalidJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise InvalidJsonError(
            f"malformed JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from None
    if type(data) is not dict:
        raise ValidationError("job: expected object")
    return data


def _closed(obj: Any, allowed: set[str], required: set[str], ctx: str) -> dict[str, Any]:
    if type(obj) is not dict:
        raise ValidationError(f"{ctx}: expected object")
    unknown = set(obj) - allowed
    missing = required - set(obj)
    if unknown:
        raise ValidationError(f"{ctx}: unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValidationError(f"{ctx}: missing field(s): {', '.join(sorted(missing))}")
    return obj


def _str(obj: dict[str, Any], key: str, ctx: str) -> str:
    value = obj[key]
    if type(value) is not str or not value.strip():
        raise ValidationError(f"{ctx}.{key}: expected non-empty string")
    return value


def _int(obj: dict[str, Any], key: str, ctx: str, minimum: int = 1) -> int:
    value = obj[key]
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{ctx}.{key}: expected integer >= {minimum}")
    return value


def _number(obj: dict[str, Any], key: str, ctx: str, minimum: float | None = None) -> float:
    value = obj[key]
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValidationError(f"{ctx}.{key}: expected number")
    value = float(value)
    if not math.isfinite(value):
        raise ValidationError(f"{ctx}.{key}: expected finite number")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{ctx}.{key}: expected number >= {minimum}")
    return value


def _temperature(obj: dict[str, Any], key: str, ctx: str) -> float:
    value = _number(obj, key, ctx, 0.0)
    if value > 2.0:
        raise ValidationError(f"{ctx}.{key}: expected number in range 0..2")
    return value


def _decimal_or_none(obj: dict[str, Any], key: str, ctx: str) -> Decimal | None:
    value = obj[key]
    if value is None:
        return None
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValidationError(f"{ctx}.{key}: expected non-negative number or null")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{ctx}.{key}: expected finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValidationError(f"{ctx}.{key}: invalid decimal number") from None
    if result < 0:
        raise ValidationError(f"{ctx}.{key}: expected non-negative number or null")
    return result


def _provider(value: str, ctx: str) -> str:
    if value not in PROVIDERS:
        raise ValidationError(f"{ctx}.provider: unsupported provider {value!r}")
    return value


def _worker(obj: Any, base: Path, index: int) -> WorkerSpec:
    ctx = f"workers[{index}]"
    data = _closed(obj, WORKER_KEYS, WORKER_KEYS, ctx)
    stage_id = _str(data, "id", ctx)
    validate_stage_id(stage_id, f"{ctx}.id")
    provider = _provider(_str(data, "provider", ctx), ctx)
    return WorkerSpec(
        id=stage_id,
        provider=provider,
        model=_str(data, "model", ctx),
        system_prompt_path=resolve_job_relative(_str(data, "system_prompt_path", ctx), base),
        temperature=_temperature(data, "temperature", ctx),
        max_output_tokens=_int(data, "max_output_tokens", ctx),
        timeout_seconds=_number(data, "timeout_seconds", ctx, 0.000001),
    )


def _synthesis(obj: Any, base: Path) -> SynthesisSpec | None:
    if obj is None:
        return None
    ctx = "synthesis"
    data = _closed(obj, SYNTH_KEYS, SYNTH_KEYS, ctx)
    stage_id = _str(data, "id", ctx)
    validate_stage_id(stage_id, f"{ctx}.id")
    deps = data["depends_on"]
    if type(deps) is not list or not deps:
        raise ValidationError("synthesis.depends_on: expected non-empty list")
    parsed: list[str] = []
    for i, dep in enumerate(deps):
        if type(dep) is not str or not dep.strip():
            raise ValidationError(f"synthesis.depends_on[{i}]: expected non-empty string")
        if dep in parsed:
            raise ValidationError(f"synthesis.depends_on: duplicate dependency {dep!r}")
        parsed.append(dep)
    provider = _provider(_str(data, "provider", ctx), ctx)
    return SynthesisSpec(
        id=stage_id,
        provider=provider,
        model=_str(data, "model", ctx),
        system_prompt_path=resolve_job_relative(_str(data, "system_prompt_path", ctx), base),
        temperature=_temperature(data, "temperature", ctx),
        max_output_tokens=_int(data, "max_output_tokens", ctx),
        timeout_seconds=_number(data, "timeout_seconds", ctx, 0.000001),
        depends_on=tuple(parsed),
    )


def validate_job(data: dict[str, Any], base_dir: Path) -> Job:
    data = _closed(data, TOP_KEYS, TOP_KEYS, "job")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValidationError("schema_version: unsupported schema version; expected 1")
    name = _str(data, "name", "job")

    raw_inputs = data["inputs"]
    if type(raw_inputs) is not list:
        raise ValidationError("inputs: expected list")
    inputs: list[InputSpec] = []
    labels: set[str] = set()
    for i, raw in enumerate(raw_inputs):
        ctx = f"inputs[{i}]"
        item = _closed(raw, INPUT_KEYS, INPUT_KEYS, ctx)
        label = _str(item, "label", ctx)
        if label in labels:
            raise ValidationError(f"inputs: duplicate label {label!r}")
        labels.add(label)
        inputs.append(
            InputSpec(label=label, path=resolve_job_relative(_str(item, "path", ctx), base_dir))
        )

    raw_exec = _closed(data["execution"], EXEC_KEYS, EXEC_KEYS, "execution")
    execution = ExecutionLimits(
        max_parallelism=_int(raw_exec, "max_parallelism", "execution"),
        run_timeout_seconds=_number(raw_exec, "run_timeout_seconds", "execution", 0.000001),
        stop_before_synthesis_if_known_cost_exceeds_usd=_decimal_or_none(
            raw_exec, "stop_before_synthesis_if_known_cost_exceeds_usd", "execution"
        ),
    )

    raw_workers = data["workers"]
    if type(raw_workers) is not list:
        raise ValidationError("workers: expected list")
    workers = tuple(_worker(item, base_dir, i) for i, item in enumerate(raw_workers))
    worker_ids = [worker.id for worker in workers]
    duplicates = sorted({wid for wid in worker_ids if worker_ids.count(wid) > 1})
    if duplicates:
        raise ValidationError(f"workers: duplicate worker id(s): {', '.join(duplicates)}")

    synthesis = _synthesis(data["synthesis"], base_dir)
    worker_set = set(worker_ids)
    if synthesis is not None:
        if synthesis.id in worker_set:
            raise ValidationError("synthesis.id collides with a worker id")
        missing = [dep for dep in synthesis.depends_on if dep not in worker_set]
        if missing:
            raise ValidationError(
                f"synthesis.depends_on references unknown worker(s): {', '.join(missing)}"
            )

    raw_output = _closed(data["output"], OUTPUT_KEYS, OUTPUT_KEYS, "output")
    runs_dir = validate_runs_dir(
        resolve_job_relative(_str(raw_output, "runs_dir", "output"), base_dir)
    )

    return Job(
        schema_version=1,
        name=name,
        inputs=tuple(inputs),
        execution=execution,
        workers=workers,
        synthesis=synthesis,
        output=OutputConfig(runs_dir=runs_dir),
    )


def load_job(path: Path | str) -> Job:
    path = Path(path).resolve(strict=False)
    data = _load_json(path)
    return validate_job(data, path.parent)


def _validate_text_file(path: Path, ctx: str) -> str:
    if not path.exists():
        raise FileValidationError(f"{ctx}: file does not exist: {path}")
    if not path.is_file():
        raise FileValidationError(f"{ctx}: not a regular file: {path}")
    if not os.access(path, os.R_OK):
        raise FileValidationError(f"{ctx}: file is not readable: {path}")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileValidationError(f"{ctx}: file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise FileValidationError(f"{ctx}: cannot read file: {path}: {exc.strerror or exc}") from None


def _validate_worker_contract_file(path: Path, ctx: str) -> None:
    text = _validate_text_file(path, ctx)
    try:
        parse_worker_contract(text)
    except ValidationError as exc:
        raise FileValidationError(f"{ctx}: {exc}") from None


def validate_referenced_files(job: Job) -> None:
    for item in job.inputs:
        _validate_text_file(item.path, f"input {item.label!r}")
    for worker in job.workers:
        _validate_worker_contract_file(
            worker.system_prompt_path, f"worker {worker.id!r} prompt"
        )
    if job.synthesis is not None:
        _validate_worker_contract_file(
            job.synthesis.system_prompt_path, "synthesis prompt"
        )


def job_to_dict(job: Job) -> dict[str, Any]:
    def stage_common(spec: WorkerSpec | SynthesisSpec) -> dict[str, Any]:
        return {
            "id": spec.id,
            "provider": spec.provider,
            "model": spec.model,
            "system_prompt_path": str(spec.system_prompt_path),
            "temperature": spec.temperature,
            "max_output_tokens": spec.max_output_tokens,
            "timeout_seconds": spec.timeout_seconds,
        }

    synthesis = None
    if job.synthesis is not None:
        synthesis = stage_common(job.synthesis)
        synthesis["depends_on"] = list(job.synthesis.depends_on)

    return {
        "schema_version": job.schema_version,
        "name": job.name,
        "inputs": [{"label": i.label, "path": str(i.path)} for i in job.inputs],
        "execution": {
            "max_parallelism": job.execution.max_parallelism,
            "run_timeout_seconds": job.execution.run_timeout_seconds,
            "stop_before_synthesis_if_known_cost_exceeds_usd": (
                None
                if job.execution.stop_before_synthesis_if_known_cost_exceeds_usd is None
                else float(job.execution.stop_before_synthesis_if_known_cost_exceeds_usd)
            ),
        },
        "workers": [stage_common(worker) for worker in job.workers],
        "synthesis": synthesis,
        "output": {"runs_dir": str(job.output.runs_dir)},
    }
