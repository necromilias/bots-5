from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import ValidationError


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_stage_id(value: str, context: str = "stage id") -> None:
    if not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(
            f"{context} must match {SAFE_ID_RE.pattern!r}; got {value!r}"
        )


def validate_run_id(value: str) -> None:
    if not SAFE_RUN_ID_RE.fullmatch(value):
        raise ValidationError("invalid run id")


def resolve_job_relative(raw: str, base_dir: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def resolve_runs_dir(raw: str, base_dir: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    if path.is_symlink():
        raise ValidationError(f"output.runs_dir must not be a symlink: {path}")
    return validate_runs_dir(path)


def validate_runs_dir(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    anchor = Path(resolved.anchor)
    if resolved == anchor:
        raise ValidationError("output.runs_dir must not resolve to filesystem root")
    if resolved.exists() and not resolved.is_dir():
        raise ValidationError(f"output.runs_dir is not a directory: {resolved}")
    return resolved


def locate_run_dir(run_id: str, runs_dir: Path | None = None) -> Path:
    validate_run_id(run_id)
    base = (runs_dir or (Path.cwd() / ".bots5" / "runs")).resolve(strict=False)
    run_dir = (base / run_id).resolve(strict=False)
    try:
        common = Path(os.path.commonpath([str(base), str(run_dir)]))
    except ValueError as exc:
        raise ValidationError("run directory resolves outside runs directory") from exc
    if common != base:
        raise ValidationError("run directory resolves outside runs directory")
    return run_dir
