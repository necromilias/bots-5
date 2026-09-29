from __future__ import annotations

import hashlib
import json
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
    # HSF-4 option 4a (Phase 10 M0.1a): run-level state only. Stage-level
    # cancellation keeps its own error_type == "cancelled" label (O-3); a
    # sibling cancelled during a generic run failure stays run-level FAILED
    # with internal_error (O-2).
    CANCELLED = "cancelled"


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
class LocalOpenAIConfig:
    base_url: str
    api_key_env: str | None = None


@dataclass(frozen=True)
class ProviderConfigs:
    local_openai: LocalOpenAIConfig | None = None


@dataclass(frozen=True)
class Job:
    schema_version: int
    name: str
    inputs: tuple[InputSpec, ...]
    execution: ExecutionLimits
    workers: tuple[WorkerSpec, ...]
    synthesis: SynthesisSpec | None
    output: OutputConfig
    providers: ProviderConfigs = field(default_factory=ProviderConfigs)


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
    finish_reason: str | None = None
    completion_complete: bool | None = None
    error_type: str | None = None
    error_message: str | None = None
    provider_side_outcome_unknown: bool = False
    # Phase 10 M0.1a additive provenance fields (evidence v2). All default to
    # their version-1 absent value, so version-1 documents remain READABLE
    # (v1 evidence is read, never rewritten). Per CAMPAIGN_EVIDENCE_EVOLUTION.md
    # §3, to_dict() emits attempt_number always; consumed_dependencies /
    # dependency_digests only when non-empty; preflight_digest only when set.
    attempt_number: int = 1
    consumed_dependencies: dict[str, int] | None = None
    dependency_digests: dict[str, str] | None = None
    preflight_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
            "completion": None
            if self.completion_complete is None
            else {
                "finish_reason": self.finish_reason,
                "complete": self.completion_complete,
            },
            "failure": None
            if self.error_type is None
            else {
                "type": self.error_type,
                "message": self.error_message,
                "provider_side_outcome_unknown": self.provider_side_outcome_unknown,
            },
        }
        # Additive v2 keys. Every pre-existing v1 key keeps its value and
        # position; the v2 keys append after "failure". attempt_number is
        # emitted ALWAYS (design of record, CAMPAIGN_EVIDENCE_EVOLUTION.md §3);
        # the map fields are emitted only when non-empty and preflight_digest
        # only when set.
        payload["attempt_number"] = self.attempt_number
        if self.consumed_dependencies:
            payload["consumed_dependencies"] = dict(self.consumed_dependencies)
        if self.dependency_digests:
            payload["dependency_digests"] = dict(self.dependency_digests)
        if self.preflight_digest is not None:
            payload["preflight_digest"] = self.preflight_digest
        return payload


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


def canonical_json(payload: Any) -> str:
    """Canonical JSON used for digests: sorted keys, no insignificant space.

    Sorting keys makes any two equal payloads serialize identically regardless
    of dict insertion order; ``ensure_ascii=False`` keeps non-ASCII text as
    itself. Digests are sha256 over the UTF-8 encoding of this string.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FileSnapshot:
    """Frozen snapshot of one referenced file (Phase 10 M0.1a).

    ``content_utf8`` optionally pins the decoded bytes; it is emitted in
    ``to_dict()`` only when set, mirroring the additive-key convention used by
    :class:`StageRecord`.
    """

    path: str
    sha256: str
    size_bytes: int
    content_utf8: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }
        if self.content_utf8 is not None:
            payload["content_utf8"] = self.content_utf8
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSnapshot:
        return cls(
            path=data["path"],
            sha256=data["sha256"],
            size_bytes=data["size_bytes"],
            content_utf8=data.get("content_utf8"),
        )


@dataclass(frozen=True)
class PreflightSnapshot:
    """Frozen job/configuration snapshot bound into an approval (M0.1a).

    ``provider_routes`` route records use the serialized field name
    ``api_key_source`` (Mick clarification O-4) — e.g.
    ``{"kind": ..., "base_url": ..., "api_key_env_name": ..., "api_key_source": ...}``.
    They record where a credential comes from (for example ``"environment"``)
    plus the environment variable NAME; a secret value is never recorded.
    """

    job_name: str
    schema_version: int
    output_runs_dir: str
    execution_limits: dict[str, Any]
    provider_routes: dict[str, dict[str, Any]]
    worker_specs: tuple[dict[str, Any], ...]
    synthesis_spec: dict[str, Any] | None
    inputs: tuple[FileSnapshot, ...]
    contracts: tuple[FileSnapshot, ...]
    system_messages: dict[str, str]
    preflight_digest: str

    def compute_digest(self) -> str:
        """sha256 of the canonical JSON of every field except ``preflight_digest``."""
        return _sha256_of(self._digest_payload())

    def _digest_payload(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "schema_version": self.schema_version,
            "output_runs_dir": self.output_runs_dir,
            "execution_limits": self.execution_limits,
            "provider_routes": self.provider_routes,
            "worker_specs": [dict(spec) for spec in self.worker_specs],
            "synthesis_spec": None
            if self.synthesis_spec is None
            else dict(self.synthesis_spec),
            "inputs": [snap.to_dict() for snap in self.inputs],
            "contracts": [snap.to_dict() for snap in self.contracts],
            "system_messages": self.system_messages,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._digest_payload()
        payload["preflight_digest"] = self.preflight_digest
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreflightSnapshot:
        return cls(
            job_name=data["job_name"],
            schema_version=data["schema_version"],
            output_runs_dir=data["output_runs_dir"],
            execution_limits=dict(data["execution_limits"]),
            provider_routes={
                key: dict(route) for key, route in data["provider_routes"].items()
            },
            worker_specs=tuple(dict(spec) for spec in data["worker_specs"]),
            synthesis_spec=None
            if data.get("synthesis_spec") is None
            else dict(data["synthesis_spec"]),
            inputs=tuple(FileSnapshot.from_dict(item) for item in data["inputs"]),
            contracts=tuple(FileSnapshot.from_dict(item) for item in data["contracts"]),
            system_messages=dict(data["system_messages"]),
            preflight_digest=data["preflight_digest"],
        )


@dataclass(frozen=True)
class OperationSnapshot:
    """Frozen operation-specific binding whose digest is the approval digest.

    ``provider_route`` follows the same O-4 naming rule as
    :attr:`PreflightSnapshot.provider_routes`: ``api_key_source`` names where
    the credential comes from; no secret value is ever recorded.
    """

    operation: str
    target_run_id: str
    stage_id: str | None
    attempt_number: int | None
    model: str | None
    provider_route: dict[str, Any] | None
    system_message: str | None
    user_message: str | None
    dependency_attempts: dict[str, int]
    dependency_digests: dict[str, str]
    preflight_digest: str
    operation_digest: str

    def compute_digest(self) -> str:
        """sha256 of the canonical JSON of every field except ``operation_digest``."""
        return _sha256_of(
            {
                "operation": self.operation,
                "target_run_id": self.target_run_id,
                "stage_id": self.stage_id,
                "attempt_number": self.attempt_number,
                "model": self.model,
                "provider_route": None
                if self.provider_route is None
                else dict(self.provider_route),
                "system_message": self.system_message,
                "user_message": self.user_message,
                "dependency_attempts": self.dependency_attempts,
                "dependency_digests": self.dependency_digests,
                "preflight_digest": self.preflight_digest,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target_run_id": self.target_run_id,
            "stage_id": self.stage_id,
            "attempt_number": self.attempt_number,
            "model": self.model,
            "provider_route": None
            if self.provider_route is None
            else dict(self.provider_route),
            "system_message": self.system_message,
            "user_message": self.user_message,
            "dependency_attempts": dict(self.dependency_attempts),
            "dependency_digests": dict(self.dependency_digests),
            "preflight_digest": self.preflight_digest,
            "operation_digest": self.operation_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationSnapshot:
        return cls(
            operation=data["operation"],
            target_run_id=data["target_run_id"],
            stage_id=data["stage_id"],
            attempt_number=data["attempt_number"],
            model=data["model"],
            provider_route=None
            if data.get("provider_route") is None
            else dict(data["provider_route"]),
            system_message=data["system_message"],
            user_message=data["user_message"],
            dependency_attempts=dict(data["dependency_attempts"]),
            dependency_digests=dict(data["dependency_digests"]),
            preflight_digest=data["preflight_digest"],
            operation_digest=data["operation_digest"],
        )


@dataclass(frozen=True)
class ApprovalRecord:
    """Local operator consent record binding an approval to a digest + target."""

    approval_id: str
    approved_at: str
    approved_by: str
    preflight_digest: str
    scope: str
    target: dict[str, Any]
    pricing_evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "approval_id": self.approval_id,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "preflight_digest": self.preflight_digest,
            "scope": self.scope,
            "target": dict(self.target),
        }
        if self.pricing_evidence is not None:
            payload["pricing_evidence"] = dict(self.pricing_evidence)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        return cls(
            approval_id=data["approval_id"],
            approved_at=data["approved_at"],
            approved_by=data["approved_by"],
            preflight_digest=data["preflight_digest"],
            scope=data["scope"],
            target=dict(data["target"]),
            pricing_evidence=None
            if data.get("pricing_evidence") is None
            else dict(data["pricing_evidence"]),
        )
