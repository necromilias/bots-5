from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import StorageError, ValidationError
from .manifest import job_to_dict
from .models import Job, RunState, StageRecord
from .paths import validate_run_id, validate_stage_id
from .usage import usage_document


@dataclass(frozen=True)
class RunDirs:
    root: Path
    stages: Path
    events: Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return (value[:48] or "run").lower()


def new_run_id(job_name: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{_slug(job_name)}-{ts}-{uuid4().hex[:8]}"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    if path.exists() and path.is_symlink():
        raise StorageError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise StorageError(f"cannot write artifact: {path}: {exc.strerror or exc}") from None


def atomic_write_json(path: Path, data: Any) -> None:
    try:
        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise StorageError(f"cannot serialize JSON artifact {path}: {exc}") from None
    _atomic_write(path, payload)


def atomic_write_text(path: Path, text: str) -> None:
    _atomic_write(path, text)


def create_run_tree(runs_dir: Path, run_id: str) -> RunDirs:
    validate_run_id(run_id)
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        if runs_dir.is_symlink():
            raise StorageError(f"runs directory must not be a symlink: {runs_dir}")
        run_dir = runs_dir / run_id
        os.mkdir(run_dir)
        stages = run_dir / "stages"
        os.mkdir(stages)
        events = run_dir / "events.jsonl"
        events.touch(exist_ok=False)
        _fsync_dir(run_dir)
        _fsync_dir(runs_dir)
        return RunDirs(root=run_dir, stages=stages, events=events)
    except FileExistsError:
        raise StorageError(f"run directory already exists: {runs_dir / run_id}") from None
    except OSError as exc:
        raise StorageError(f"cannot create run directory under {runs_dir}: {exc.strerror or exc}") from None


def persist_resolved_job(dirs: RunDirs, job: Job) -> None:
    atomic_write_json(dirs.root / "job.resolved.json", job_to_dict(job))


def persist_stage(dirs: RunDirs, stage: StageRecord, text: str | None = None) -> None:
    validate_stage_id(stage.id)
    if text is not None:
        rel = Path("stages") / f"{stage.id}.md"
        stage.output_path = str(rel)
        atomic_write_text(dirs.root / rel, text)
    atomic_write_json(dirs.stages / f"{stage.id}.json", stage.to_dict())


def persist_usage(dirs: RunDirs, stages: list[StageRecord] | tuple[StageRecord, ...]) -> dict[str, Any]:
    doc = usage_document(stages)
    atomic_write_json(dirs.root / "usage.json", doc)
    return doc


def persist_run(
    dirs: RunDirs,
    *,
    run_id: str,
    state: RunState,
    started_at: str,
    ended_at: str | None,
    stages: list[StageRecord] | tuple[StageRecord, ...],
    run_timeout_seconds: float,
    synthesis_skipped_reason: str | None = None,
) -> None:
    usage = usage_document(stages)
    atomic_write_json(
        dirs.root / "run.json",
        {
            "run_id": run_id,
            "state": state.value,
            "started_at": started_at,
            "ended_at": ended_at,
            "run_timeout_seconds": run_timeout_seconds,
            "synthesis_skipped_reason": synthesis_skipped_reason,
            "stage_order": [stage.id for stage in stages],
            "stages": {stage.id: stage.to_dict() for stage in stages},
            "usage": usage["aggregate"],
        },
    )


def persist_result(dirs: RunDirs, text: str) -> None:
    atomic_write_text(dirs.root / "result.md", text)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValidationError(f"artifact not found: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read artifact {path}: {exc}") from None


def load_run_view(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    run = read_json(run_dir / "run.json")
    usage = read_json(run_dir / "usage.json")
    order = run.get("stage_order", [])
    stages: list[dict[str, Any]] = []
    for stage_id in order:
        validate_stage_id(stage_id)
        stages.append(read_json(run_dir / "stages" / f"{stage_id}.json"))
    return run, stages, usage


def load_stage_view(run_dir: Path, stage_id: str) -> tuple[dict[str, Any], str | None]:
    validate_stage_id(stage_id)
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    meta = read_json(run_dir / "stages" / f"{stage_id}.json")
    output = None
    output_path = meta.get("output_path")
    if output_path:
        candidate = (run_dir / output_path).resolve(strict=False)
        if candidate.parent != (run_dir / "stages").resolve(strict=False):
            raise ValidationError("stage output path escapes run directory")
        try:
            output = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationError(f"cannot read stage output: {candidate}: {exc}") from None
    return meta, output
