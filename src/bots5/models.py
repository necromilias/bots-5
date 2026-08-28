from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class StageState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class InputSpec:
    label: str
    path: Path


@dataclass(frozen=True)
class WorkerSpec:
    id: str
    provider: str
    model: str
    system_prompt_path: Path
    temperature: float
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class SynthesisSpec:
    id: str
    provider: str
    model: str
    system_prompt_path: Path
    temperature: float
    max_output_tokens: int
    timeout_seconds: float
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionLimits:
    max_parallelism: int
    run_timeout_seconds: float
    stop_before_synthesis_if_known_cost_exceeds_usd: Decimal | None


@dataclass(frozen=True)
class OutputConfig:
    runs_dir: Path


@dataclass(frozen=True)
class Job:
    schema_version: int
    name: str
    inputs: tuple[InputSpec, ...]
    execution: ExecutionLimits
    workers: tuple[WorkerSpec, ...]
    synthesis: SynthesisSpec | None
    output: OutputConfig


@dataclass
class StageRecord:
    id: str
    provider: str
    requested_model: str
    state: StageState = StageState.QUEUED
    returned_model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    known_cost_usd: Decimal | None = None
    request_id: str | None = None
    output_path: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    provider_side_outcome_unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.id,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "returned_model": self.returned_model,
            "state": self.state.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "total_tokens": self.total_tokens,
            },
            "cost_usd": None if self.known_cost_usd is None else str(self.known_cost_usd),
            "cost_known": self.known_cost_usd is not None,
            "provider_request_id": self.request_id,
            "output_path": self.output_path,
            "failure": None
            if self.error_type is None
            else {
                "type": self.error_type,
                "message": self.error_message,
                "provider_side_outcome_unknown": self.provider_side_outcome_unknown,
            },
        }


@dataclass(frozen=True)
class CostSummary:
    known_sum_usd: Decimal
    status: str
    unknown_stage_ids: tuple[str, ...]
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "known_sum_usd": str(self.known_sum_usd),
            "status": self.status,
            "complete": self.complete,
            "unknown_stage_ids": list(self.unknown_stage_ids),
        }


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    state: RunState
    stages: tuple[StageRecord, ...]
    exit_code: int
