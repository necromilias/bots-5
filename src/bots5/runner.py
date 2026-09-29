from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import ApprovalInvalidatedError, Bots5Error, ProviderError, StorageError, ValidationError
from .events import EventWriter, now_iso
from .manifest import job_to_dict, validate_referenced_files
from .models import (
    ApprovalRecord,
    FileSnapshot,
    Job,
    OperationSnapshot,
    PreflightSnapshot,
    RunResult,
    RunState,
    StageRecord,
    StageState,
    SynthesisSpec,
    WorkerSpec,
    canonical_json,
)
from .prompts import compile_worker_system_message
from .providers.base import CompletionRequest, CompletionResult, Provider
from .providers.openai_compatible import OpenAICompatibleProvider
from .providers.openrouter import OpenRouterProvider
from .rendering import render_synthesis_user_message, render_worker_user_message
from .storage import (
    EVIDENCE_VERSION_V2,
    RunDirs,
    approval_consumed,
    consume_approval,
    create_run_tree,
    load_attempt_records,
    new_run_id,
    next_attempt_number,
    persist_preflight,
    persist_resolved_job,
    persist_result,
    persist_run,
    persist_run_v2,
    persist_stage,
    persist_stage_attempt,
    persist_usage,
    persist_usage_v2,
    read_json,
    read_selection,
    selected_attempt,
    write_selection,
)
from .paths import validate_run_id, validate_stage_id
from .usage import aggregate_cost, derive_selected_spend


def _read_utf8(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _failure_message(exc: BaseException) -> str:
    if isinstance(exc, Bots5Error):
        return str(exc)[:500]
    return f"{type(exc).__name__}: {str(exc)[:400]}"


def _apply_result(record: StageRecord, result: CompletionResult) -> None:
    record.returned_model = result.returned_model
    record.request_id = result.request_id
    record.finish_reason = result.finish_reason
    record.completion_complete = result.finish_reason == "stop"
    record.prompt_tokens = result.prompt_tokens
    record.completion_tokens = result.completion_tokens
    record.reasoning_tokens = result.reasoning_tokens
    record.total_tokens = result.total_tokens
    record.known_cost_usd = result.known_cost_usd
    record.duration_seconds = result.duration_seconds


def _stage_completed_successfully(record: StageRecord) -> bool:
    return record.state == StageState.SUCCEEDED and record.completion_complete is True


# --- Phase 10 M0.3a: preflight snapshot + approval binding (v2 run path) -----

# Frozen openrouter route: the manifest declares provider config only for
# local_openai, so the openrouter route is the CLI convention (cli.py; N-6).
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_API_KEY_ENV_NAME = "OPENROUTER_API_KEY"
# M-3 kind-specific provider-object validation: the concrete class exposed for
# each frozen route kind (isinstance, so subclasses keep their route identity;
# providers/** is read, never mutated).
_PROVIDER_KIND_CLASSES: dict[str, type] = {
    "openrouter": OpenRouterProvider,
    "local_openai": OpenAICompatibleProvider,
}
_FULL_RUN_SCOPE = "full_run"


def _declared_provider_ids(job: Job) -> set[str]:
    ids = {worker.provider for worker in job.workers}
    if job.synthesis is not None:
        ids.add(job.synthesis.provider)
    return ids


def _spec_payload(spec: WorkerSpec | SynthesisSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": spec.id,
        "provider": spec.provider,
        "model": spec.model,
        "temperature": spec.temperature,
        "max_output_tokens": spec.max_output_tokens,
        "timeout_seconds": spec.timeout_seconds,
        "system_prompt_path": str(spec.system_prompt_path),
    }
    if isinstance(spec, SynthesisSpec):
        payload["depends_on"] = list(spec.depends_on)
    return payload


def _execution_limits_payload(job: Job) -> dict[str, Any]:
    threshold = job.execution.stop_before_synthesis_if_known_cost_exceeds_usd
    return {
        "max_parallelism": job.execution.max_parallelism,
        "run_timeout_seconds": job.execution.run_timeout_seconds,
        "stop_before_synthesis_if_known_cost_exceeds_usd": (
            None if threshold is None else str(threshold)
        ),
    }


def declared_provider_routes(job: Job) -> dict[str, dict[str, Any]]:
    """Frozen provider route records derived from the job's declared provider
    configuration (O-4 field naming; N-6 openrouter origin).

    ``api_key_source`` records where the credential comes from ("environment"
    when an environment-variable NAME is declared, "inline" otherwise); no
    secret value is ever recorded.
    """
    routes: dict[str, dict[str, Any]] = {}
    for provider_id in sorted(_declared_provider_ids(job)):
        if provider_id == "local_openai":
            config = job.providers.local_openai
            if config is None:
                raise Bots5Error("local_openai provider configuration is missing")
            routes[provider_id] = {
                "kind": "local_openai",
                "base_url": config.base_url,
                "api_key_env_name": config.api_key_env,
                "api_key_source": (
                    "environment" if config.api_key_env is not None else "inline"
                ),
            }
        elif provider_id == "openrouter":
            routes[provider_id] = {
                "kind": "openrouter",
                "base_url": _OPENROUTER_BASE_URL,
                "api_key_env_name": _OPENROUTER_API_KEY_ENV_NAME,
                "api_key_source": "environment",
            }
        else:
            raise Bots5Error(f"unsupported provider id: {provider_id!r}")
    return routes


def preflight_configuration_payload(job: Job) -> dict[str, Any]:
    """Canonical job-configuration digest input (execution assertion 5).

    Shared verbatim by :func:`build_preflight_snapshot` (frozen at preflight
    time) and the engine-side consistency assertion, so a snapshot built from a
    job canonicalizes to exactly this payload and re-verifies against it: the
    job worker/synthesis specs, provider routes and execution limits.
    """
    return {
        "job_name": job.name,
        "schema_version": job.schema_version,
        "output_runs_dir": str(job.output.runs_dir),
        "execution_limits": _execution_limits_payload(job),
        "provider_routes": declared_provider_routes(job),
        "worker_specs": [_spec_payload(worker) for worker in job.workers],
        "synthesis_spec": None if job.synthesis is None else _spec_payload(job.synthesis),
    }


def file_snapshot_for(path: Path) -> FileSnapshot:
    """Digest-identify one referenced file (no content copy: the bytes are
    bound by sha256+size and re-verified from disk at assertion time)."""
    data = path.read_bytes()
    return FileSnapshot(
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def build_preflight_snapshot(job: Job) -> PreflightSnapshot:
    """Build the frozen full-run PreflightSnapshot for ``job`` (M0.3a).

    Zero-spend and side-effect-free: nothing is written and no provider is
    contacted; referenced input and contract bytes are read exactly once, here
    at preflight time (dispatch afterwards is zero-reread). The desktop bridge
    reuses this constructor so the frozen payload shapes cannot drift from the
    engine's consistency assertions.
    """
    config = preflight_configuration_payload(job)
    specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
    if job.synthesis is not None:
        specs += (job.synthesis,)
    snapshot = PreflightSnapshot(
        job_name=config["job_name"],
        schema_version=config["schema_version"],
        output_runs_dir=config["output_runs_dir"],
        execution_limits=dict(config["execution_limits"]),
        provider_routes={key: dict(route) for key, route in config["provider_routes"].items()},
        worker_specs=tuple(dict(spec) for spec in config["worker_specs"]),
        synthesis_spec=None
        if config["synthesis_spec"] is None
        else dict(config["synthesis_spec"]),
        inputs=tuple(file_snapshot_for(item.path) for item in job.inputs),
        contracts=tuple(file_snapshot_for(spec.system_prompt_path) for spec in specs),
        system_messages={
            spec.id: compile_worker_system_message(_read_utf8(spec.system_prompt_path))
            for spec in specs
        },
        preflight_digest="",
    )
    return replace(snapshot, preflight_digest=snapshot.compute_digest())


def _require_approval_binding(
    *,
    job: Job,
    providers: Mapping[str, Provider],
    run_id: str,
    snapshot: PreflightSnapshot,
    approval: ApprovalRecord,
) -> tuple[dict[str, str], str]:
    """Engine-side execution assertions (PREFLIGHT_APPROVAL_STATE_MACHINE.md §4).

    Runs BEFORE the run directory exists and before any provider request:
    digest identity (1) plus snapshot self-integrity, scope + target (2),
    configuration consistency (5), disk consistency of every referenced input
    and contract (4), system-message/contract correspondence, and
    provider-object route validation (7, kind-specific per M-3). Referenced
    bytes are read exactly once here, for verification; dispatch afterwards
    uses only the frozen messages built from the verified bytes (assertion 8,
    zero-reread). Writes nothing and contacts no provider, so a refused
    approval leaves zero run-directory bytes. Returns the frozen
    ``(system_messages, worker_user_message)``.
    """

    def refuse(reason: str) -> ApprovalInvalidatedError:
        return ApprovalInvalidatedError(reason)

    # (1) Identity — and snapshot self-integrity: the approved digest must be
    # the digest of the very payload presented for re-verification.
    if approval.preflight_digest != snapshot.preflight_digest:
        raise refuse("approval preflight_digest does not match the supplied snapshot digest")
    if snapshot.compute_digest() != snapshot.preflight_digest:
        raise refuse("snapshot payload does not match its own preflight_digest")

    # (2) Scope + target: a full-run approval binds exactly this run id.
    if approval.scope != _FULL_RUN_SCOPE:
        raise refuse(
            f"approval scope {approval.scope!r} does not match the {_FULL_RUN_SCOPE!r} operation"
        )
    expected_target = {"run_id": run_id, "stage_id": None, "attempt_number": None}
    if not isinstance(approval.target, dict) or approval.target != expected_target:
        target = approval.target if isinstance(approval.target, dict) else None
        raise refuse(
            f"approval target {target!r} does not match the full-run target {expected_target!r}"
        )

    # (5) Configuration consistency: specs, provider routes, execution limits.
    live = preflight_configuration_payload(job)
    frozen = {
        "job_name": snapshot.job_name,
        "schema_version": snapshot.schema_version,
        "output_runs_dir": snapshot.output_runs_dir,
        "execution_limits": dict(snapshot.execution_limits),
        "provider_routes": {key: dict(route) for key, route in snapshot.provider_routes.items()},
        "worker_specs": [dict(spec) for spec in snapshot.worker_specs],
        "synthesis_spec": None
        if snapshot.synthesis_spec is None
        else dict(snapshot.synthesis_spec),
    }
    if canonical_json(live) != canonical_json(frozen):
        raise refuse(
            "job configuration (worker/synthesis specs, provider routes, execution limits) "
            "does not match the approved preflight snapshot"
        )

    def verify_files(
        entries: tuple[FileSnapshot, ...], paths: list[Path], kind: str
    ) -> dict[str, str]:
        frozen_by_key: dict[str, FileSnapshot] = {}
        for entry in entries:
            key = str(Path(entry.path).resolve(strict=False))
            if key in frozen_by_key:
                raise refuse(f"approved snapshot lists {kind} path {entry.path!r} twice")
            frozen_by_key[key] = entry
        verified: dict[str, str] = {}
        for path in paths:
            key = str(path.resolve(strict=False))
            entry = frozen_by_key.pop(key, None)
            if entry is None:
                raise refuse(f"referenced {kind} file was not part of the approval: {path}")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise refuse(f"approved {kind} file is unreadable: {path}") from exc
            if hashlib.sha256(data).hexdigest() != entry.sha256 or len(data) != entry.size_bytes:
                raise refuse(f"approved {kind} bytes changed after approval: {path}")
            try:
                verified[key] = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise refuse(f"approved {kind} bytes are not valid UTF-8: {path}") from exc
        if frozen_by_key:
            unlisted = ", ".join(sorted(frozen_by_key))
            raise refuse(
                f"approved snapshot lists {kind} files the job no longer references: {unlisted}"
            )
        return verified

    specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
    if job.synthesis is not None:
        specs += (job.synthesis,)

    # (4) Disk consistency: re-read, recompute sha256, compare with snapshot.
    verified_contracts = verify_files(
        snapshot.contracts, [spec.system_prompt_path for spec in specs], "contract"
    )
    verified_inputs = verify_files(snapshot.inputs, [item.path for item in job.inputs], "input")

    # The frozen system messages must be the compiled approved contract bytes.
    system_messages = dict(snapshot.system_messages)
    for spec in specs:
        key = str(spec.system_prompt_path.resolve(strict=False))
        if system_messages.get(spec.id) != compile_worker_system_message(verified_contracts[key]):
            raise refuse(
                f"approved system message for stage {spec.id!r} does not match the "
                "approved contract bytes"
            )

    # (7) Provider-object route validation, kind-specific (F-03/M-3): the
    # provider mapping key set equals the approved provider set; every used
    # instance matches the frozen route's kind and base_url, and exposes an
    # api_key_env equal to the frozen name ONLY where it exposes the property.
    if set(providers.keys()) != set(snapshot.provider_routes.keys()):
        raise refuse("provider mapping keys do not equal the approved provider set")
    for provider_id in sorted(_declared_provider_ids(job)):
        route = snapshot.provider_routes.get(provider_id)
        if not isinstance(route, dict):
            raise refuse(f"approved preflight has no route for provider {provider_id!r}")
        provider = providers[provider_id]
        kind = route.get("kind")
        expected_class = _PROVIDER_KIND_CLASSES.get(kind) if isinstance(kind, str) else None
        if expected_class is None or not isinstance(provider, expected_class):
            raise refuse(
                f"provider {provider_id!r} instance does not match the approved kind {kind!r}"
            )
        if getattr(provider, "base_url", None) != route.get("base_url"):
            raise refuse(f"provider {provider_id!r} base_url does not match the approved route")
        if hasattr(provider, "api_key_env"):
            if getattr(provider, "api_key_env") != route.get("api_key_env_name"):
                raise refuse(
                    f"provider {provider_id!r} api_key_env does not match the approved route"
                )

    # (8) Zero-reread dispatch: the worker user message is rendered from the
    # exact verified input bytes (labels are the job's declared input labels;
    # the approval binds the bytes, not the labels).
    worker_user = render_worker_user_message(
        [
            (item.label, verified_inputs[str(item.path.resolve(strict=False))])
            for item in job.inputs
        ]
    )
    pricing_stages = [
        {"id": spec["id"], "provider": spec["provider"], "model": spec["model"],
         "max_output_tokens": spec["max_output_tokens"],
         "compiled_prompt_text": system_messages[spec["id"]] + "\n" + worker_user}
        for spec in snapshot.worker_specs
    ]
    if snapshot.synthesis_spec is not None:
        spec = snapshot.synthesis_spec
        pricing_stages.append(
            {"id": spec["id"], "provider": spec["provider"], "model": spec["model"],
             "max_output_tokens": spec["max_output_tokens"],
             "compiled_prompt_text": system_messages[spec["id"]] + "\n" + worker_user}
        )
    expected_pricing = build_pricing_evidence(
        pricing_stages, snapshot.provider_routes, approval.pricing_evidence
    )
    has_paid_route = any(
        snapshot.provider_routes.get(stage["provider"], {}).get("kind") != "local_openai"
        for stage in pricing_stages
    )
    if has_paid_route and approval.pricing_evidence is None:
        raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
    if canonical_json(expected_pricing) != canonical_json(approval.pricing_evidence):
        raise ApprovalInvalidatedError(
            "approval pricing evidence does not match the operation routes, models, prompts, and ceilings"
        )
    return system_messages, worker_user


_PRICING_BASIS_FORMULA = (
    "per stage: input token ceiling = ceil(len(compiled_prompt_text) / 4), "
    "output token ceiling = that stage's configured max_output_tokens, "
    "stage cost = input_ceiling * input_rate / 1e6 + output_ceiling * output_rate / 1e6; "
    "bound = sum over all dispatched stages."
)


def build_pricing_evidence(
    stages: list[dict[str, Any]],
    routes: Mapping[str, Mapping[str, Any]],
    operator_entries: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate operator rates and deterministically compute paid-route bounds.

    ``stages`` contains the actual dispatched stage model, provider, prompt text
    and configured output ceiling. The input is an operator-authored block with
    one entry per paid provider (provider, rates, source and observation time).
    """
    paid = [s for s in stages if routes.get(s.get("provider"), {}).get("kind") != "local_openai"]
    if not paid:
        return None
    if operator_entries is None:
        return None
    if not isinstance(operator_entries, dict) or not isinstance(operator_entries.get("entries"), list):
        raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
    supplied = operator_entries["entries"]
    by_provider: dict[str, dict[str, Any]] = {}
    for entry in supplied:
        if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str):
            raise ApprovalInvalidatedError("pricing evidence entries must identify a provider route")
        provider = entry["provider"]
        if provider in by_provider:
            raise ApprovalInvalidatedError(f"duplicate pricing evidence for route {provider!r}")
        by_provider[provider] = entry
    providers = list(dict.fromkeys(s["provider"] for s in paid))
    if set(by_provider) != set(providers):
        raise ApprovalInvalidatedError("pricing evidence must cover exactly every paid route in use")
    entries: list[dict[str, Any]] = []
    for provider in providers:
        source = by_provider[provider]
        try:
            input_rate = Decimal(str(source["input_usd_per_1m"]))
            output_rate = Decimal(str(source["output_usd_per_1m"]))
        except (KeyError, ArithmeticError, ValueError):
            raise ApprovalInvalidatedError(f"pricing rates are missing or invalid for {provider!r}") from None
        if not input_rate.is_finite() or not output_rate.is_finite() or input_rate < 0 or output_rate < 0:
            raise ApprovalInvalidatedError(f"pricing rates must be finite non-negative numbers for {provider!r}")
        rate_source = source.get("rate_source")
        observed_at = source.get("observed_at")
        if not isinstance(rate_source, str) or not rate_source.strip():
            raise ApprovalInvalidatedError(f"pricing rate_source is required for {provider!r}")
        if not isinstance(observed_at, str) or not observed_at.strip():
            raise ApprovalInvalidatedError(f"pricing observed_at is required for {provider!r}")
        route = routes.get(provider)
        if not isinstance(route, Mapping):
            raise ApprovalInvalidatedError(f"pricing route {provider!r} is not present in the operation")
        relevant = [s for s in paid if s["provider"] == provider]
        expected_models = list(dict.fromkeys(s["model"] for s in relevant))
        if source.get("route") is not None and canonical_json(source["route"]) != canonical_json(dict(route)):
            raise ApprovalInvalidatedError(f"pricing route identity does not match live route {provider!r}")
        if source.get("models") is not None and source["models"] != expected_models:
            raise ApprovalInvalidatedError(f"pricing models do not match selected models for {provider!r}")
        details = []
        input_ceilings: dict[str, int] = {}
        for stage in stages:
            if not isinstance(stage.get("max_output_tokens"), int) or stage["max_output_tokens"] <= 0:
                raise ApprovalInvalidatedError(f"invalid output ceiling for stage {stage.get('id')!r}")
            prompt = stage.get("compiled_prompt_text")
            if not isinstance(prompt, str):
                raise ApprovalInvalidatedError(f"missing compiled prompt text for stage {stage.get('id')!r}")
            input_ceiling = (len(prompt) + 3) // 4
            input_ceilings[stage["id"]] = input_ceiling
            details.append(
                f"{stage['id']}(input_token_ceiling={input_ceiling}, "
                f"max_output_tokens={stage['max_output_tokens']})"
            )
        cost = Decimal(0)
        for stage in relevant:
            cost += (Decimal(input_ceilings[stage["id"]]) * input_rate +
                     Decimal(stage["max_output_tokens"]) * output_rate) / Decimal(1_000_000)
        bound_basis = (
            f"{_PRICING_BASIS_FORMULA} Recorded divisor=4; stage_count={len(stages)}; "
            f"configured ceilings: {', '.join(details)}."
        )
        entries.append({
            "provider": provider,
            "route": dict(route),
            "models": list(dict.fromkeys(s["model"] for s in relevant)),
            "input_usd_per_1m": str(input_rate),
            "output_usd_per_1m": str(output_rate),
            "rate_source": rate_source,
            "observed_at": observed_at,
            "conservative_upper_bound_usd": str(cost),
            "basis": bound_basis,
        })
    return {"entries": entries}


def _preflight_document(
    *,
    run_id: str,
    snapshot: PreflightSnapshot,
    approval: ApprovalRecord,
    pricing_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The durable preflight.json document (CAMPAIGN_EVIDENCE_EVOLUTION.md §2.4).

    HSF-1 Branch A records operator-supplied rates and the accepted fixed bound;
    no pricing registry, network lookup, or OPv1 waiver is used.
    """
    contract_sha256 = {
        str(Path(entry.path).resolve(strict=False)): entry.sha256 for entry in snapshot.contracts
    }

    def stage_entry(spec: dict[str, Any]) -> dict[str, Any]:
        path = spec.get("system_prompt_path")
        key = str(Path(path).resolve(strict=False)) if isinstance(path, str) else ""
        return {
            "stage_id": spec.get("id"),
            "provider": spec.get("provider"),
            "model": spec.get("model"),
            "temperature": spec.get("temperature"),
            "max_output_tokens": spec.get("max_output_tokens"),
            "timeout_seconds": spec.get("timeout_seconds"),
            "system_prompt_path": path,
            "system_prompt_sha256": contract_sha256.get(key),
        }

    stages = [stage_entry(spec) for spec in snapshot.worker_specs]
    if snapshot.synthesis_spec is not None:
        stages.append(stage_entry(snapshot.synthesis_spec))
    return {
        "doc_schema_version": 1,
        "job_schema_version": snapshot.schema_version,
        "job_name": snapshot.job_name,
        "run_id": run_id,
        "operation": "full_run",
        "target": {"run_id": run_id, "stage_id": None, "attempt_number": None},
        "prepared_at": now_iso(),
        "execution_limits": dict(snapshot.execution_limits),
        "provider_routes": {key: dict(route) for key, route in snapshot.provider_routes.items()},
        "stages": stages,
        "inputs": [
            {"path": entry.path, "sha256": entry.sha256, "size_bytes": entry.size_bytes}
            for entry in snapshot.inputs
        ],
        "preflight_digest": snapshot.preflight_digest,
        "approval": approval.to_dict(),
        "pricing_evidence": pricing_evidence,
    }


class _Writers:
    """Mode-aware durable artifact writers.

    Legacy mode (no snapshot/approval) delegates to the untouched v1 writers,
    keeping pre-Phase-10 behaviour byte-identical. v2 mode writes the flat
    attempt grammar (``stages/<id>.att1.json``/.md — ``create=True`` claims the
    attempt, ``create=False`` is the queued→running→terminal in-place update),
    the evidence-v2 usage document and run.json with the evidence_version
    marker (via persist_run_v2).
    """

    def __init__(self, dirs: RunDirs, *, v2: bool):
        self.dirs = dirs
        self.v2 = v2

    def stage(
        self,
        record: StageRecord,
        text: str | None = None,
        *,
        create: bool = False,
        attempt_number: int | None = None,
    ) -> None:
        # ``attempt_number`` defaults to 1 (the M0.3a full-run grammar). The
        # M0.3b operations pass the sibling attempt number they claimed; the
        # full-run call sites are unchanged and keep writing attempt 1.
        number = 1 if attempt_number is None else attempt_number
        if self.v2:
            persist_stage_attempt(self.dirs, record, attempt_number=number, text=text, create=create)
        else:
            persist_stage(self.dirs, record, text)

    def usage(self, stages: list[StageRecord] | tuple[StageRecord, ...]) -> dict[str, Any]:
        if self.v2:
            return persist_usage_v2(
                self.dirs,
                list(stages),
                selected_attempts={},
                stage_ids=[record.id for record in stages],
            )
        return persist_usage(self.dirs, stages)

    def run(self, **kwargs: Any) -> None:
        if self.v2:
            persist_run_v2(self.dirs, **kwargs)
        else:
            persist_run(self.dirs, **kwargs)


def _best_effort_internal_failure(
    *,
    writers: _Writers,
    events: EventWriter,
    run_id: str,
    started_at: str,
    records: list[StageRecord],
    run_timeout_seconds: float,
    exc: BaseException,
    synthesis_skipped_reason: str | None,
) -> None:
    message = _failure_message(exc)
    for record in records:
        if record.error_type == "cancelled_pending":
            # Mick O-2/O-3: a sibling cancelled during a generic run failure
            # keeps its OWN cause ("cancelled") — never the run's
            # internal_error, never run_timed_out — while the run itself stays
            # FAILED with internal_error. provider_side_outcome_unknown is
            # preserved exactly as the stage handler recorded it.
            record.error_type = "cancelled"
            record.error_message = "stage was cancelled while the run failed with an internal error"
            record.ended_at = record.ended_at or now_iso()
            try:
                writers.stage(record)
                events.write("stage_failed", record.id, error_type=record.error_type)
            except StorageError:
                pass
    for record in records:
        if record.state in (StageState.QUEUED, StageState.RUNNING):
            record.state = StageState.FAILED
            record.error_type = "internal_error"
            record.error_message = message
            record.ended_at = now_iso()
            try:
                writers.stage(record)
            except StorageError:
                pass
    try:
        events.write("run_failed", error_type="internal_error", message=message)
    except StorageError:
        pass
    try:
        writers.usage(records)
    except StorageError:
        pass
    try:
        writers.run(
            run_id=run_id,
            state=RunState.FAILED,
            started_at=started_at,
            ended_at=now_iso(),
            stages=records,
            run_timeout_seconds=run_timeout_seconds,
            synthesis_skipped_reason=synthesis_skipped_reason,
        )
    except StorageError:
        pass


def _raise_after_best_effort_failure(
    *,
    writers: _Writers,
    events: EventWriter,
    run_id: str,
    started_at: str,
    records: list[StageRecord],
    run_timeout_seconds: float,
    exc: BaseException,
    synthesis_skipped_reason: str | None,
) -> None:
    _best_effort_internal_failure(
        writers=writers,
        events=events,
        run_id=run_id,
        started_at=started_at,
        records=records,
        run_timeout_seconds=run_timeout_seconds,
        exc=exc,
        synthesis_skipped_reason=synthesis_skipped_reason,
    )
    if isinstance(exc, Bots5Error):
        raise exc
    raise Bots5Error(f"run failed with internal error: {_failure_message(exc)}") from exc


async def _execute_stage(
    *,
    spec: WorkerSpec | SynthesisSpec,
    record: StageRecord,
    system_message: str,
    user_message: str,
    provider: Provider,
    semaphore: asyncio.Semaphore,
    writers: _Writers,
    events: EventWriter,
    attempt_number: int = 1,
    model_override: str | None = None,
    usage_callback: Callable[[], None] | None = None,
) -> str | None:
    started_monotonic: float | None = None

    def persist_terminal_usage() -> None:
        if usage_callback is not None:
            usage_callback()
    # M0.3b: a regenerated sibling may request a DIFFERENT model (§2.1, only
    # the model may differ); ``model_override`` is that approved requested
    # model. Full-run dispatch keeps ``spec.model`` (override stays None).
    dispatch_model = spec.model if model_override is None else model_override
    try:
        async with semaphore:
            record.state = StageState.RUNNING
            record.started_at = now_iso()
            started_monotonic = time.monotonic()
            writers.stage(record, attempt_number=attempt_number)
            events.write("stage_started", record.id)
            request = CompletionRequest(
                model=dispatch_model,
                system=system_message,
                user=user_message,
                temperature=spec.temperature,
                max_output_tokens=spec.max_output_tokens,
                timeout_seconds=spec.timeout_seconds,
            )
            events.write("request_sent", record.id, model=dispatch_model)
            try:
                result = await asyncio.wait_for(provider.complete(request), timeout=spec.timeout_seconds)
            except TimeoutError:
                record.state = StageState.FAILED
                record.error_type = "request_timeout"
                record.error_message = "provider request timed out"
                record.provider_side_outcome_unknown = True
                record.ended_at = now_iso()
                record.duration_seconds = (
                    None if started_monotonic is None else time.monotonic() - started_monotonic
                )
                writers.stage(record, attempt_number=attempt_number)
                events.write("stage_failed", record.id, error_type=record.error_type)
                persist_terminal_usage()
                return None
            except ProviderError as exc:
                record.state = StageState.FAILED
                record.error_type = type(exc).__name__
                record.error_message = _failure_message(exc)
                record.ended_at = now_iso()
                record.duration_seconds = (
                    None if started_monotonic is None else time.monotonic() - started_monotonic
                )
                writers.stage(record, attempt_number=attempt_number)
                events.write("stage_failed", record.id, error_type=record.error_type)
                persist_terminal_usage()
                return None
            except Exception as exc:
                record.state = StageState.FAILED
                record.error_type = "internal_error"
                record.error_message = _failure_message(exc)
                record.ended_at = now_iso()
                record.duration_seconds = (
                    None if started_monotonic is None else time.monotonic() - started_monotonic
                )
                writers.stage(record, attempt_number=attempt_number)
                events.write("stage_failed", record.id, error_type=record.error_type)
                persist_terminal_usage()
                return None

            _apply_result(record, result)
            record.state = StageState.SUCCEEDED
            record.ended_at = now_iso()
            if record.duration_seconds == 0.0 and started_monotonic is not None:
                record.duration_seconds = time.monotonic() - started_monotonic
            writers.stage(record, result.output_text, attempt_number=attempt_number)
            events.write("stage_succeeded", record.id)
            persist_terminal_usage()
            return result.output_text

    except asyncio.CancelledError:
        # F-05 repair: a cancellation is NOT a timeout. Write the NEUTRAL
        # marker only — the outer terminalization (overall timeout, operator
        # cancellation, or generic run failure) is the only place that knows
        # the true cause and relabels it (run_timed_out / cancelled).
        if record.state not in (StageState.SUCCEEDED, StageState.FAILED):
            record.state = StageState.FAILED
            record.error_type = "cancelled_pending"
            record.error_message = "stage cancelled before terminal classification"
            record.provider_side_outcome_unknown = started_monotonic is not None
            record.ended_at = now_iso()
            if started_monotonic is not None:
                record.duration_seconds = time.monotonic() - started_monotonic
            try:
                writers.stage(record, attempt_number=attempt_number)
                events.write("stage_failed", record.id, error_type=record.error_type)
            except StorageError:
                pass
        raise


def _all_stage_records(job: Job) -> list[StageRecord]:
    records = [StageRecord(id=w.id, provider=w.provider, requested_model=w.model) for w in job.workers]
    if job.synthesis is not None:
        records.append(
            StageRecord(
                id=job.synthesis.id,
                provider=job.synthesis.provider,
                requested_model=job.synthesis.model,
            )
        )
    return records


def _validate_provider_mapping(job: Job, providers: Mapping[str, Provider]) -> None:
    if not isinstance(providers, Mapping):
        raise Bots5Error("providers must be a mapping of provider IDs to providers")
    required = {worker.provider for worker in job.workers}
    if job.synthesis is not None:
        required.add(job.synthesis.provider)
    missing = sorted(provider_id for provider_id in required if provider_id not in providers)
    if missing:
        raise Bots5Error(
            "missing provider mapping for: " + ", ".join(repr(provider_id) for provider_id in missing)
        )
    invalid = sorted(
        provider_id
        for provider_id in required
        if not callable(getattr(providers[provider_id], "complete", None))
    )
    if invalid:
        raise Bots5Error(
            "invalid provider mapping for: " + ", ".join(repr(provider_id) for provider_id in invalid)
        )


async def run_job(
    job: Job,
    providers: Mapping[str, Provider],
    *,
    run_id: str | None = None,
    snapshot: PreflightSnapshot | None = None,
    approval: ApprovalRecord | None = None,
) -> RunResult:
    v2_mode = snapshot is not None or approval is not None
    if (snapshot is None) != (approval is None):
        raise Bots5Error("run_job requires snapshot and approval together (or neither)")
    validate_referenced_files(job)
    _validate_provider_mapping(job, providers)
    specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
    if job.synthesis is not None:
        specs += (job.synthesis,)

    if v2_mode:
        # v2 mode: assertions bind the approval to the frozen snapshot BEFORE
        # any provider request and BEFORE create_run_tree; dispatch afterwards
        # uses only the frozen messages (zero-reread). The approval binds the
        # target run id, so an omitted run_id resolves from the approval.
        if run_id is None:
            bound = approval.target.get("run_id") if isinstance(approval.target, dict) else None
            if isinstance(bound, str) and bound:
                run_id = bound
        run_id = run_id or new_run_id(job.name)
        system_messages, worker_user = _require_approval_binding(
            job=job,
            providers=providers,
            run_id=run_id,
            snapshot=snapshot,
            approval=approval,
        )
    else:
        # Legacy headless behaviour: byte-identical to the pre-v2 engine.
        system_messages = {
            spec.id: compile_worker_system_message(_read_utf8(spec.system_prompt_path)) for spec in specs
        }
        input_texts = [(item.label, _read_utf8(item.path)) for item in job.inputs]
        worker_user = render_worker_user_message(input_texts)
        run_id = run_id or new_run_id(job.name)

    if v2_mode:
        # One-shot gate BEFORE the run tree exists (§3.1, F-02): a spent
        # approval is refused with the typed error while there is no new run
        # directory and no provider call. The authoritative exclusive-create
        # claim (consume_approval, O_CREAT|O_EXCL) follows as soon as the tree
        # exists and still before preflight.json and any dispatch, so a
        # concurrent consumer always fails closed.
        if approval_consumed(job.output.runs_dir / run_id, approval.approval_id):
            raise ApprovalInvalidatedError(f"approval already consumed: {approval.approval_id}")

    dirs = create_run_tree(job.output.runs_dir, run_id)
    events = EventWriter(dirs.events, run_id)
    writers = _Writers(dirs, v2=v2_mode)
    started_at = now_iso()

    if v2_mode:
        consume_approval(
            dirs.root,
            approval.approval_id,
            {**approval.to_dict(), "consumed_at": now_iso()},
        )
        persist_preflight(
            dirs.root,
            _preflight_document(
                run_id=run_id, snapshot=snapshot, approval=approval,
                pricing_evidence=approval.pricing_evidence,
            ),
        )

    records = _all_stage_records(job)
    if v2_mode:
        # Every v2 attempt record is bound to the approved preflight digest.
        for record in records:
            record.preflight_digest = snapshot.preflight_digest
    record_by_id = {record.id: record for record in records}
    synthesis_skipped_reason: str | None = None

    try:
        persist_resolved_job(dirs, job)
        writers.usage(records)
        writers.run(
            run_id=run_id,
            state=RunState.RUNNING,
            started_at=started_at,
            ended_at=None,
            stages=records,
            run_timeout_seconds=job.execution.run_timeout_seconds,
        )
        events.write("run_started")
        for record in records:
            # v2: create=True claims the attempt-1 namespace exclusively; the
            # queued→running→terminal updates below are in-place (create=False).
            writers.stage(record, create=True)
            events.write("stage_queued", record.id)
    except Exception as exc:
        _raise_after_best_effort_failure(
            writers=writers,
            events=events,
            run_id=run_id,
            started_at=started_at,
            records=records,
            run_timeout_seconds=job.execution.run_timeout_seconds,
            exc=exc,
            synthesis_skipped_reason=synthesis_skipped_reason,
        )

    semaphore = asyncio.Semaphore(job.execution.max_parallelism)
    outputs: dict[str, str] = {}
    worker_tasks: list[asyncio.Task[str | None]] = []
    terminal_usage_callback = (
        (lambda: _persist_operation_usage(dirs, [record.id for record in records]))
        if v2_mode
        else None
    )

    async def pipeline() -> RunState:
        nonlocal synthesis_skipped_reason
        for spec in job.workers:
            record = record_by_id[spec.id]
            task = asyncio.create_task(
                _execute_stage(
                    spec=spec,
                    record=record,
                    system_message=system_messages[spec.id],
                    user_message=worker_user,
                    provider=providers[spec.provider],
                    semaphore=semaphore,
                    writers=writers,
                    events=events,
                    usage_callback=terminal_usage_callback,
                ),
                name=f"bots5-worker-{spec.id}",
            )
            worker_tasks.append(task)

        if worker_tasks:
            results = await asyncio.gather(*worker_tasks, return_exceptions=False)
            for spec, text in zip(job.workers, results):
                if text is not None:
                    outputs[spec.id] = text

        if job.synthesis is None:
            return (
                RunState.SUCCEEDED
                if all(_stage_completed_successfully(record_by_id[w.id]) for w in job.workers)
                else RunState.FAILED
            )

        synth = job.synthesis
        synth_record = record_by_id[synth.id]
        failed_deps = [
            dep for dep in synth.depends_on if record_by_id[dep].state != StageState.SUCCEEDED
        ]
        if failed_deps:
            synthesis_skipped_reason = "dependency_failed"
            synth_record.state = StageState.SKIPPED
            synth_record.known_cost_usd = Decimal("0")
            synth_record.error_type = "dependency_failed"
            synth_record.error_message = "synthesis dependency did not succeed"
            writers.stage(synth_record)
            events.write("stage_skipped", synth.id, reason=synthesis_skipped_reason)
            events.write(
                "synthesis_blocked",
                synth.id,
                reason=synthesis_skipped_reason,
                failed_dependencies=failed_deps,
            )
            return RunState.FAILED

        incomplete_deps = [
            dep for dep in synth.depends_on if record_by_id[dep].completion_complete is not True
        ]
        if incomplete_deps:
            synthesis_skipped_reason = "dependency_incomplete"
            synth_record.state = StageState.SKIPPED
            synth_record.known_cost_usd = Decimal("0")
            synth_record.error_type = "dependency_incomplete"
            synth_record.error_message = "synthesis dependency did not complete normally"
            writers.stage(synth_record)
            events.write("stage_skipped", synth.id, reason=synthesis_skipped_reason)
            events.write(
                "synthesis_blocked",
                synth.id,
                reason=synthesis_skipped_reason,
                incomplete_dependencies=incomplete_deps,
            )
            return RunState.FAILED

        worker_records = [record_by_id[w.id] for w in job.workers]
        threshold = job.execution.stop_before_synthesis_if_known_cost_exceeds_usd
        cost = aggregate_cost(worker_records)
        if threshold is not None and cost.known_sum_usd > threshold:
            synthesis_skipped_reason = "known_cost_threshold_exceeded"
            synth_record.state = StageState.SKIPPED
            synth_record.known_cost_usd = Decimal("0")
            synth_record.error_type = "known_cost_threshold_exceeded"
            synth_record.error_message = (
                f"known worker cost {cost.known_sum_usd} exceeds synthesis gate {threshold}"
            )
            writers.stage(synth_record)
            events.write("stage_skipped", synth.id, reason=synthesis_skipped_reason)
            events.write(
                "synthesis_blocked",
                synth.id,
                reason=synthesis_skipped_reason,
                known_cost_usd=str(cost.known_sum_usd),
                threshold_usd=str(threshold),
            )
            return RunState.FAILED

        dependencies = [(dep, outputs[dep]) for dep in synth.depends_on]
        if v2_mode:
            # M-2: synthesis provenance is recorded AT DISPATCH, after the
            # dependency and cost gates and BEFORE the provider request
            # (request_sent), so a crash cannot leave a dispatched synthesis
            # attempt without provenance. The digests cover the exact output
            # bytes rendered into the synthesis user message — the same texts
            # passed to render_synthesis_user_message below; nothing is
            # re-read from disk here.
            synth_record.consumed_dependencies = {
                dep: selected_attempt(dirs.root, dep) for dep in synth.depends_on
            }
            synth_record.dependency_digests = {
                dep: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for dep, text in dependencies
            }
            writers.stage(synth_record)
        synthesis_user = render_synthesis_user_message(dependencies)
        synthesis_output = await _execute_stage(
            spec=synth,
            record=synth_record,
            system_message=system_messages[synth.id],
            user_message=synthesis_user,
            provider=providers[synth.provider],
            semaphore=semaphore,
            writers=writers,
            events=events,
            usage_callback=terminal_usage_callback,
        )
        if synthesis_output is not None and synth_record.state == StageState.SUCCEEDED:
            persist_result(dirs, synthesis_output)
            if terminal_usage_callback is not None:
                # The synthesis output mirror must be durable before the final
                # usage refresh for this completed synthesis attempt.
                terminal_usage_callback()

        all_workers_ok = all(_stage_completed_successfully(record_by_id[w.id]) for w in job.workers)
        return (
            RunState.SUCCEEDED
            if all_workers_ok and _stage_completed_successfully(synth_record)
            else RunState.FAILED
        )

    try:
        final_state = await asyncio.wait_for(pipeline(), timeout=job.execution.run_timeout_seconds)
    except TimeoutError:
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        for worker in job.workers:
            record = record_by_id[worker.id]
            if record.state in (StageState.QUEUED, StageState.RUNNING):
                record.state = StageState.FAILED
                record.error_type = "run_timed_out"
                record.error_message = "stage did not finish before overall run timeout"
                record.ended_at = now_iso()
                writers.stage(record)
                events.write("stage_failed", record.id, error_type=record.error_type)

        if job.synthesis is not None:
            synth_record = record_by_id[job.synthesis.id]
            if synth_record.state in (StageState.QUEUED, StageState.RUNNING):
                if synth_record.state == StageState.QUEUED:
                    synth_record.state = StageState.SKIPPED
                    synth_record.known_cost_usd = Decimal("0")
                    synth_record.error_type = "run_timed_out"
                    synth_record.error_message = "synthesis was not reached before overall run timeout"
                    events.write("stage_skipped", synth_record.id, reason="run_timed_out")
                else:
                    synth_record.state = StageState.FAILED
                    synth_record.error_type = "run_timed_out"
                    synth_record.error_message = "synthesis did not finish before overall run timeout"
                    synth_record.provider_side_outcome_unknown = True
                    events.write("stage_failed", synth_record.id, error_type="run_timed_out")
                synth_record.ended_at = now_iso()
                writers.stage(synth_record)

        # F-05/O-3 relabel: the stage-level cancellation markers written while
        # draining the tasks were neutral; the overall timeout is the TRUE
        # cause, so relabel them to run_timed_out (preserving
        # provider_side_outcome_unknown). A genuine overall timeout stays
        # RunState.TIMED_OUT and is never reported as a user cancellation (B4).
        for record in records:
            if record.error_type == "cancelled_pending":
                record.error_type = "run_timed_out"
                record.error_message = (
                    "synthesis did not finish before overall run timeout"
                    if job.synthesis is not None and record.id == job.synthesis.id
                    else "stage did not finish before overall run timeout"
                )
                record.ended_at = record.ended_at or now_iso()
                writers.stage(record)
                events.write("stage_failed", record.id, error_type=record.error_type)

        final_state = RunState.TIMED_OUT
        events.write("run_timed_out")
    except asyncio.CancelledError:
        # HSF-4 option 4a (engine-side terminalization, DESKTOP_SURFACE_AND_
        # LIFECYCLE.md §5.2): a user-initiated cancellation terminalizes
        # durably — run-level CANCELLED, stage-level cause exactly "cancelled"
        # (O-3) — BEFORE the cancellation propagates. It is never labelled
        # run_timed_out (F-05) and never internal_error (B4).
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        ended_at = now_iso()
        affected: list[StageRecord] = []
        for record in records:
            if record.error_type == "cancelled_pending":
                record.error_type = "cancelled"
                affected.append(record)
            elif record.state in (StageState.QUEUED, StageState.RUNNING):
                record.state = StageState.FAILED
                record.error_type = "cancelled"
                record.error_message = "run cancelled before the stage reached a terminal state"
                record.provider_side_outcome_unknown = record.started_at is not None
                record.ended_at = ended_at
                affected.append(record)
        for record in affected:
            try:
                writers.stage(record)
                events.write("stage_failed", record.id, error_type=record.error_type)
            except StorageError:
                pass
        try:
            events.write("run_cancelled")
        except StorageError:
            pass
        try:
            writers.usage(records)
        except StorageError:
            pass
        try:
            writers.run(
                run_id=run_id,
                state=RunState.CANCELLED,
                started_at=started_at,
                ended_at=now_iso(),
                stages=records,
                run_timeout_seconds=job.execution.run_timeout_seconds,
                synthesis_skipped_reason=synthesis_skipped_reason,
            )
        except StorageError:
            pass
        # Re-raise only AFTER durable terminalization.
        raise
    except Exception as exc:
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        _raise_after_best_effort_failure(
            writers=writers,
            events=events,
            run_id=run_id,
            started_at=started_at,
            records=records,
            run_timeout_seconds=job.execution.run_timeout_seconds,
            exc=exc,
            synthesis_skipped_reason=synthesis_skipped_reason,
        )

    try:
        ended_at = now_iso()
        writers.usage(records)
        if final_state == RunState.SUCCEEDED:
            events.write("run_succeeded")
        elif final_state == RunState.FAILED:
            events.write("run_failed")
        writers.run(
            run_id=run_id,
            state=final_state,
            started_at=started_at,
            ended_at=ended_at,
            stages=records,
            run_timeout_seconds=job.execution.run_timeout_seconds,
            synthesis_skipped_reason=synthesis_skipped_reason,
        )
    except Exception as exc:
        _raise_after_best_effort_failure(
            writers=writers,
            events=events,
            run_id=run_id,
            started_at=started_at,
            records=records,
            run_timeout_seconds=job.execution.run_timeout_seconds,
            exc=exc,
            synthesis_skipped_reason=synthesis_skipped_reason,
        )

    return RunResult(
        run_id=run_id,
        run_dir=dirs.root,
        state=final_state,
        stages=tuple(records),
        exit_code=0 if final_state == RunState.SUCCEEDED else 1,
    )


# --- Phase 10 M0.3b: explicit worker regeneration + synthesis rerun ----------
# REGENERATION_AND_STALE_SYNTHESIS.md §2/§5 and PREFLIGHT_APPROVAL_STATE_MACHINE.md
# §3/§4/§6. These are the two explicit engine entry points for operations on an
# EXISTING evidence-v2 run directory. They are never invoked by the run pipeline
# itself: there is no automatic retry, no resume, and no auto-selection anywhere
# in these paths. Every precondition is verified from disk BEFORE any provider
# call, and every refusal path writes nothing.

_WORKER_REGENERATION_SCOPE_PREFIX = "worker_regeneration:"
_SYNTHESIS_RERUN_SCOPE = "synthesis_rerun"


def _operation_run_dirs(run_dir: Path) -> RunDirs:
    """A RunDirs view over an EXISTING run directory (no tree creation)."""
    return RunDirs(root=run_dir, stages=run_dir / "stages", events=run_dir / "events.jsonl")


def _require_v2_operation_target(run_dir: Path, run_id: str) -> list[str]:
    """Shared preconditions 0+1, including the M-6 read-only rule for v1.

    The run directory must exist, declare ``evidence_version >= 2`` in
    run.json, carry the requested run id, and list a stage_order. A version 1
    run (marker absent) is READ-ONLY: the typed ValidationError fires before
    anything is written. The declared stage ids are validated and returned.
    """
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir}")
    run = read_json(run_dir / "run.json")
    if not isinstance(run, dict):
        raise ValidationError(f"run document must be a JSON object: {run_dir / 'run.json'}")
    version = run.get("evidence_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError(f"run evidence_version must be an integer; got {version!r}")
    if version < EVIDENCE_VERSION_V2:
        raise ValidationError(
            f"run declares evidence version {version}; this operation requires "
            f"evidence_version >= 2 (version 1 evidence is read-only, nothing was written)"
        )
    if version > EVIDENCE_VERSION_V2:
        raise ValidationError(
            f"unsupported evidence_version {version}; this engine supports up to "
            f"{EVIDENCE_VERSION_V2}"
        )
    if run.get("run_id") != run_id:
        raise ValidationError(
            f"run.json run_id {run.get('run_id')!r} does not match the requested run id {run_id!r}"
        )
    stage_order = run.get("stage_order")
    if (
        not isinstance(stage_order, list)
        or not stage_order
        or not all(isinstance(stage_id, str) for stage_id in stage_order)
    ):
        raise ValidationError("run.json stage_order must be a non-empty list of stage ids")
    for stage_id in stage_order:
        validate_stage_id(stage_id, "run stage_order stage id")
    return stage_order


def _require_resolved_job_matches(job: Job, resolved: dict[str, Any]) -> None:
    """Precondition 2: job.resolved.json canonically equals the supplied job."""
    if canonical_json(resolved) != canonical_json(job_to_dict(job)):
        raise ValidationError(
            "job.resolved.json does not canonically match the supplied job; the "
            "operation refuses to run a different topology"
        )


def _read_operation_bytes(path: Path, kind: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ApprovalInvalidatedError(f"referenced {kind} file is unreadable: {path}") from exc


def _decode_operation_bytes(data: bytes, path: Path, kind: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApprovalInvalidatedError(
            f"referenced {kind} bytes are not valid UTF-8: {path}"
        ) from exc


def _rebuild_live_preflight_digest(
    job: Job,
) -> tuple[str, list[tuple[str, str]], dict[str, str]]:
    """Single-read re-verification of the referenced bytes (precondition 6).

    Reads every referenced input and contract file EXACTLY ONCE and rebuilds
    the full-run preflight digest from exactly those bytes using the real
    ``PreflightSnapshot.compute_digest`` (constructed field-for-field like
    ``build_preflight_snapshot``, so the digest computation cannot drift). Any
    change to the job configuration, provider routes, referenced input bytes
    or contract bytes therefore changes the digest and is refused. Returns
    ``(digest, input_texts, system_messages)`` where ``input_texts`` are the
    verified ``(label, text)`` pairs in declared order and ``system_messages``
    are the compiled verified contract texts per stage id.
    """
    config = preflight_configuration_payload(job)
    specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
    if job.synthesis is not None:
        specs += (job.synthesis,)
    input_snapshots: list[FileSnapshot] = []
    input_texts: list[tuple[str, str]] = []
    for item in job.inputs:
        data = _read_operation_bytes(item.path, "input")
        input_snapshots.append(
            FileSnapshot(
                path=str(item.path),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
        input_texts.append((item.label, _decode_operation_bytes(data, item.path, "input")))
    contract_snapshots: list[FileSnapshot] = []
    system_messages: dict[str, str] = {}
    for spec in specs:
        data = _read_operation_bytes(spec.system_prompt_path, "contract")
        contract_snapshots.append(
            FileSnapshot(
                path=str(spec.system_prompt_path),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
        text = _decode_operation_bytes(data, spec.system_prompt_path, "contract")
        try:
            system_messages[spec.id] = compile_worker_system_message(text)
        except ValidationError as exc:
            raise ApprovalInvalidatedError(
                f"approved contract bytes changed after approval and no longer parse for "
                f"stage {spec.id!r}: {exc}"
            ) from exc
    snapshot = PreflightSnapshot(
        job_name=config["job_name"],
        schema_version=config["schema_version"],
        output_runs_dir=config["output_runs_dir"],
        execution_limits=dict(config["execution_limits"]),
        provider_routes={key: dict(route) for key, route in config["provider_routes"].items()},
        worker_specs=tuple(dict(spec) for spec in config["worker_specs"]),
        synthesis_spec=None
        if config["synthesis_spec"] is None
        else dict(config["synthesis_spec"]),
        inputs=tuple(input_snapshots),
        contracts=tuple(contract_snapshots),
        system_messages=system_messages,
        preflight_digest="",
    )
    return snapshot.compute_digest(), input_texts, system_messages


def _provider_routes_from_resolved_job(resolved: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Route records re-derived from the persisted job.resolved.json.

    Field-for-field the same derivation as :func:`declared_provider_routes`,
    but read from the RESOLVED document recorded inside the run directory, so
    the route identity is checked against what the run actually recorded.
    """
    declared: set[str] = set()
    workers = resolved.get("workers")
    if isinstance(workers, list):
        for item in workers:
            if isinstance(item, dict) and isinstance(item.get("provider"), str):
                declared.add(item["provider"])
    synthesis = resolved.get("synthesis")
    if isinstance(synthesis, dict) and isinstance(synthesis.get("provider"), str):
        declared.add(synthesis["provider"])
    provider_configs = resolved.get("providers")
    routes: dict[str, dict[str, Any]] = {}
    for provider_id in sorted(declared):
        if provider_id == "local_openai":
            config = (
                provider_configs.get("local_openai")
                if isinstance(provider_configs, dict)
                else None
            )
            if not isinstance(config, dict):
                raise ValidationError(
                    "job.resolved.json records no local_openai provider configuration"
                )
            api_key_env = config.get("api_key_env")
            if api_key_env is not None and not isinstance(api_key_env, str):
                raise ValidationError("job.resolved.json api_key_env must be a string or null")
            routes[provider_id] = {
                "kind": "local_openai",
                "base_url": config.get("base_url"),
                "api_key_env_name": api_key_env,
                "api_key_source": "environment" if api_key_env is not None else "inline",
            }
        elif provider_id == "openrouter":
            routes[provider_id] = {
                "kind": "openrouter",
                "base_url": _OPENROUTER_BASE_URL,
                "api_key_env_name": _OPENROUTER_API_KEY_ENV_NAME,
                "api_key_source": "environment",
            }
        else:
            raise ValidationError(
                f"job.resolved.json declares unsupported provider id: {provider_id!r}"
            )
    return routes


def _operation_provider(providers: Mapping[str, Provider], provider_id: str) -> Provider:
    """The one provider object this operation dispatches through."""
    provider = providers.get(provider_id)
    if provider is None or not callable(getattr(provider, "complete", None)):
        raise ApprovalInvalidatedError(
            f"provider mapping does not provide a usable provider for {provider_id!r}"
        )
    return provider


def _require_operation_provider_route(
    *,
    resolved: dict[str, Any],
    job: Job,
    provider_id: str,
    provider: Provider,
    snapshot_route: dict[str, Any] | None,
) -> dict[str, Any]:
    """Live route == recorded route == frozen route == provider object.

    The route derived from the job, the route recorded in job.resolved.json
    and the route bound in the operation snapshot must be identical, and the
    concrete provider instance must match the route kind-specifically (the
    M0.3a assertion-7 discipline). Only the requested MODEL may differ.
    """
    live_routes = declared_provider_routes(job)
    if provider_id not in live_routes:
        raise ApprovalInvalidatedError(f"job declares no provider route for {provider_id!r}")
    route = live_routes[provider_id]
    recorded_routes = _provider_routes_from_resolved_job(resolved)
    if canonical_json(recorded_routes) != canonical_json(live_routes):
        raise ApprovalInvalidatedError(
            "provider route recorded in job.resolved.json does not match the job's "
            "declared provider route"
        )
    if snapshot_route is None or snapshot_route != route:
        raise ApprovalInvalidatedError(
            f"operation snapshot provider route does not match the frozen route for "
            f"provider {provider_id!r}"
        )
    kind = route.get("kind")
    expected_class = _PROVIDER_KIND_CLASSES.get(kind) if isinstance(kind, str) else None
    if expected_class is None or not isinstance(provider, expected_class):
        raise ApprovalInvalidatedError(
            f"provider {provider_id!r} instance does not match the approved kind {kind!r}"
        )
    if getattr(provider, "base_url", None) != route.get("base_url"):
        raise ApprovalInvalidatedError(
            f"provider {provider_id!r} base_url does not match the approved route"
        )
    if hasattr(provider, "api_key_env") and getattr(provider, "api_key_env") != route.get(
        "api_key_env_name"
    ):
        raise ApprovalInvalidatedError(
            f"provider {provider_id!r} api_key_env does not match the approved route"
        )
    return route


def _require_operation_snapshot_binding(
    *,
    snapshot: OperationSnapshot,
    approval: ApprovalRecord,
    operation: str,
    scope: str,
    run_id: str,
    stage_id: str,
    model: str,
    live_digest: str,
) -> int:
    """Binding assertions shared by both M0.3b operations (state machine §4).

    Verifies the operation snapshot's identity and self-integrity, the
    approval's digest identity, scope and target ({run_id, stage_id,
    attempt_number}), and that the job/bytes digest re-derived from disk still
    equals the snapshot's frozen preflight digest. Returns the approved
    attempt number.
    """
    if not isinstance(snapshot, OperationSnapshot):
        raise ApprovalInvalidatedError("the operation requires a frozen OperationSnapshot")
    if snapshot.operation != operation:
        raise ApprovalInvalidatedError(
            f"operation snapshot declares operation {snapshot.operation!r}; expected {operation!r}"
        )
    if snapshot.target_run_id != run_id:
        raise ApprovalInvalidatedError(
            f"operation snapshot target_run_id {snapshot.target_run_id!r} does not match "
            f"the run id {run_id!r}"
        )
    if snapshot.stage_id != stage_id:
        raise ApprovalInvalidatedError(
            f"operation snapshot stage_id {snapshot.stage_id!r} does not match the "
            f"operation target {stage_id!r}"
        )
    if snapshot.model != model:
        raise ApprovalInvalidatedError(
            f"operation snapshot requested model {snapshot.model!r} does not match the "
            f"operation model {model!r}"
        )
    if snapshot.compute_digest() != snapshot.operation_digest:
        raise ApprovalInvalidatedError(
            "operation snapshot payload does not match its own operation_digest"
        )
    if approval.preflight_digest != snapshot.preflight_digest:
        raise ApprovalInvalidatedError(
            "approval preflight_digest does not match the operation snapshot digest"
        )
    if live_digest != snapshot.preflight_digest:
        raise ApprovalInvalidatedError(
            "job configuration or referenced bytes changed after the operation snapshot "
            "was prepared"
        )
    if operation == "worker_regeneration" and (
        snapshot.dependency_attempts or snapshot.dependency_digests
    ):
        # §2.2: a regeneration binds no dependency attempts/digests.
        raise ApprovalInvalidatedError(
            "worker regeneration operation snapshot must bind no dependency "
            "attempts/digests"
        )
    if approval.scope != scope:
        raise ApprovalInvalidatedError(
            f"approval scope {approval.scope!r} does not match the {scope!r} operation"
        )
    target = approval.target if isinstance(approval.target, dict) else {}
    if target.get("run_id") != run_id:
        raise ApprovalInvalidatedError(
            f"approval target run_id {target.get('run_id')!r} does not match the run id {run_id!r}"
        )
    if target.get("stage_id") != stage_id:
        raise ApprovalInvalidatedError(
            f"approval target stage_id {target.get('stage_id')!r} does not match the "
            f"operation target {stage_id!r}"
        )
    attempt = target.get("attempt_number")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ApprovalInvalidatedError(
            f"approval target attempt_number must be a positive integer; got {attempt!r}"
        )
    if snapshot.attempt_number != attempt:
        raise ApprovalInvalidatedError(
            "operation snapshot attempt_number does not match the approval target "
            "attempt_number"
        )
    return attempt


def _require_binding_attempt_number(approved_attempt: int, derived_attempt: int) -> int:
    """Execution step 1: the disk-derived next attempt number is BINDING."""
    if derived_attempt != approved_attempt:
        raise ApprovalInvalidatedError(
            f"approved attempt number {approved_attempt} does not match the disk-derived "
            f"next attempt number {derived_attempt}; the operation refuses to renumber"
        )
    return derived_attempt


def _require_approval_not_consumed(run_dir: Path, approval: ApprovalRecord) -> None:
    """One-shot probe (§3.1): typed refusal, nothing written, no request."""
    if approval_consumed(run_dir, approval.approval_id):
        raise ApprovalInvalidatedError(f"approval already consumed: {approval.approval_id}")


def _materialize_default_selection(run_dir: Path, run_id: str, stage_order: list[str]) -> None:
    """Materialize the implicit selection once a sibling attempt will exist.

    CAMPAIGN_EVIDENCE_EVOLUTION.md §2.2: a v2 directory holding ``.att2+``
    files without selection.json is malformed and fails closed. When a run has
    never written selection.json (every stage implicitly selects attempt 1),
    the operation that claims a sibling attempt materializes exactly that
    implicit selection — the SELECTION itself is unchanged by the execution.
    An existing selection.json is left byte-identical.
    """
    if (run_dir / "selection.json").is_file():
        return
    write_selection(run_dir, run_id, {stage_id: 1 for stage_id in stage_order})


def _per_attempt_usage_map(attempts: list[StageRecord]) -> dict[str, dict[str, Any]]:
    """The serialized ``per_attempt`` shape of ``usage_document_v2`` (F-07 input)."""
    return {
        f"{record.id}.att{record.attempt_number}": {
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "total_tokens": record.total_tokens,
            "cost_usd": None if record.known_cost_usd is None else str(record.known_cost_usd),
            "cost_known": record.known_cost_usd is not None,
        }
        for record in attempts
    }


def _persist_operation_usage(dirs: RunDirs, stage_order: list[str]) -> dict[str, Any]:
    """Re-persist the v2 usage document from disk (cumulative + derived cache).

    ``per_attempt``/``cumulative_spend`` cover EVERY executed attempt recorded
    on disk (any new sibling included, immediately); ``selected_spend`` and the
    legacy ``aggregate`` mirror are re-derived from ``per_attempt`` plus the
    CURRENT selection.json — never read from the stored cache.
    """
    attempts = load_attempt_records(dirs.root, stage_order)
    selection = read_selection(dirs.root)
    return persist_usage_v2(
        dirs,
        attempts,
        selected_attempts=selection,
        stage_ids=stage_order,
    )


def _terminalize_operation_cancellation(
    *,
    dirs: RunDirs,
    events: EventWriter,
    stage_order: list[str],
    record: StageRecord,
    attempt_number: int,
    operation_finished_event: str,
) -> None:
    """Replace the transient hard-kill marker after handled operation cancellation."""
    record.state = StageState.FAILED
    record.error_type = "cancelled"
    record.error_message = "stage cancelled by operator"
    record.provider_side_outcome_unknown = (
        record.provider_side_outcome_unknown or record.started_at is not None
    )
    record.ended_at = record.ended_at or now_iso()
    persist_stage_attempt(dirs, record, attempt_number=attempt_number)
    events.write("stage_failed", record.id, error_type="cancelled")
    _persist_operation_usage(dirs, stage_order)
    events.write(
        operation_finished_event,
        record.id,
        attempt_number=attempt_number,
        state=record.state.value,
        error_type=record.error_type,
    )


async def regenerate_worker(
    job: Job,
    providers: Mapping[str, Provider],
    *,
    run_dir: Path,
    run_id: str,
    stage_id: str,
    model: str,
    snapshot: OperationSnapshot,
    approval: ApprovalRecord,
) -> StageRecord:
    """Explicit sibling worker regeneration on an existing evidence-v2 run.

    REGENERATION_AND_STALE_SYNTHESIS.md §2 (M0.3b TASK A). Appends
    ``stages/<stage_id>.att<N>.json``/``.md`` as a NEW sibling attempt: the
    original attempt is never modified, the provider route is locked (only the
    requested model may differ, §2.1), the selection is unchanged by the
    execution itself (regeneration never auto-selects, §2.2), and usage.json's
    cumulative/per-attempt accounting includes the new attempt immediately.
    This is never an automatic retry or resume.

    Preconditions, all verified from disk BEFORE any provider call and every
    refusal writing nothing: (0) ``evidence_version >= 2`` (M-6: version 1
    evidence is read-only); (1) run directory + ``stage_id`` in stage_order;
    (2) job.resolved.json canonical match; (3) the target is a worker, never
    the synthesis stage; (4) approval scope ``worker_regeneration:<stage_id>``
    with digest identity and target binding; (5) provider route identity
    (recorded, frozen, and object-level); (6) referenced bytes still match the
    snapshot; (7) the approval is unspent. Execution re-derives the binding
    attempt number N from disk (a mismatch raises ApprovalInvalidatedError —
    never renumbered), claims the attempt exclusively, drives it queued →
    running → terminal on that SAME attempt, and returns the StageRecord.
    """
    validate_run_id(run_id)
    validate_stage_id(stage_id, "regeneration target stage id")
    if not isinstance(model, str) or not model.strip():
        raise ValidationError("regeneration requires a non-empty requested model string")
    if not isinstance(providers, Mapping):
        raise Bots5Error("providers must be a mapping of provider IDs to providers")

    stage_order = _require_v2_operation_target(run_dir, run_id)
    if stage_id not in stage_order:
        raise ValidationError(f"stage {stage_id!r} is not declared in run.json stage_order")

    resolved = read_json(run_dir / "job.resolved.json")
    if not isinstance(resolved, dict):
        raise ValidationError("job.resolved.json must be a JSON object")
    _require_resolved_job_matches(job, resolved)

    spec = next((worker for worker in job.workers if worker.id == stage_id), None)
    if spec is None:
        if job.synthesis is not None and job.synthesis.id == stage_id:
            raise ValidationError(
                f"stage {stage_id!r} is the synthesis stage; worker regeneration never "
                "targets synthesis (use rerun_synthesis)"
            )
        raise ValidationError(f"stage {stage_id!r} is not a declared worker of the supplied job")

    live_digest, input_texts, system_messages = _rebuild_live_preflight_digest(job)
    approved_attempt = _require_operation_snapshot_binding(
        snapshot=snapshot,
        approval=approval,
        operation="worker_regeneration",
        scope=f"{_WORKER_REGENERATION_SCOPE_PREFIX}{stage_id}",
        run_id=run_id,
        stage_id=stage_id,
        model=model,
        live_digest=live_digest,
    )

    if snapshot.user_message != render_worker_user_message(input_texts):
        raise ApprovalInvalidatedError(
            "operation snapshot user message does not match the verified input bytes"
        )
    if snapshot.system_message != system_messages[stage_id]:
        raise ApprovalInvalidatedError(
            "operation snapshot system message does not match the verified contract bytes"
        )
    expected_pricing = build_pricing_evidence(
        [{"id": stage_id, "provider": spec.provider, "model": model,
          "max_output_tokens": spec.max_output_tokens,
          "compiled_prompt_text": snapshot.system_message + "\n" + snapshot.user_message}],
        declared_provider_routes(job), approval.pricing_evidence,
    )
    if snapshot.provider_route is not None and snapshot.provider_route.get("kind") != "local_openai" and approval.pricing_evidence is None:
        raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
    if canonical_json(expected_pricing) != canonical_json(approval.pricing_evidence):
        raise ApprovalInvalidatedError("approval pricing evidence does not match regeneration route/model/ceiling")

    provider = _operation_provider(providers, spec.provider)
    _require_operation_provider_route(
        resolved=resolved,
        job=job,
        provider_id=spec.provider,
        provider=provider,
        snapshot_route=snapshot.provider_route,
    )

    # Zero-reread dispatch binding (§4.8): the frozen messages must be exactly
    # what the already-verified bytes compile/render to; dispatch then uses
    # only the frozen strings.
    if snapshot.user_message != render_worker_user_message(input_texts):
        raise ApprovalInvalidatedError(
            "operation snapshot user message does not match the verified input bytes"
        )
    if snapshot.system_message != system_messages[stage_id]:
        raise ApprovalInvalidatedError(
            "operation snapshot system message does not match the verified contract bytes"
        )

    _require_approval_not_consumed(run_dir, approval)

    derived_attempt = next_attempt_number(run_dir, stage_id)
    _require_binding_attempt_number(approved_attempt, derived_attempt)

    dirs = _operation_run_dirs(run_dir)
    events = EventWriter(dirs.events, run_id)
    consume_approval(
        dirs.root,
        approval.approval_id,
        {**approval.to_dict(), "consumed_at": now_iso()},
    )

    record = StageRecord(id=stage_id, provider=spec.provider, requested_model=model)
    record.preflight_digest = snapshot.preflight_digest
    # Exclusive-create claim of the sibling attempt namespace (§3.1, N-5); the
    # queued → running → terminal transitions below update THIS attempt only.
    persist_stage_attempt(dirs, record, attempt_number=derived_attempt, create=True)
    _materialize_default_selection(dirs.root, run_id, stage_order)

    events.write(
        "worker_regeneration_started",
        stage_id,
        attempt_number=derived_attempt,
        model=model,
    )
    try:
        await _execute_stage(
            spec=spec,
            record=record,
            system_message=snapshot.system_message,
            user_message=snapshot.user_message,
            provider=provider,
            semaphore=asyncio.Semaphore(1),
            writers=_Writers(dirs, v2=True),
            events=events,
            attempt_number=derived_attempt,
            model_override=model,
        )
    except asyncio.CancelledError:
        _terminalize_operation_cancellation(
            dirs=dirs,
            events=events,
            stage_order=stage_order,
            record=record,
            attempt_number=derived_attempt,
            operation_finished_event="worker_regeneration_finished",
        )
        raise
    _persist_operation_usage(dirs, stage_order)
    events.write(
        "worker_regeneration_finished",
        stage_id,
        attempt_number=derived_attempt,
        state=record.state.value,
    )
    return record


async def rerun_synthesis(
    job: Job,
    providers: Mapping[str, Provider],
    *,
    run_dir: Path,
    run_id: str,
    snapshot: OperationSnapshot,
    approval: ApprovalRecord,
) -> StageRecord:
    """Explicit synthesis rerun on an existing evidence-v2 run.

    REGENERATION_AND_STALE_SYNTHESIS.md §5 (M0.3b TASK B). Appends
    ``stages/<synthesis>.att<M>.json``/``.md`` as a NEW synthesis attempt: the
    dispatch consumes the FROZEN snapshot user message verbatim (no re-render,
    no re-read), bound to the exact selected dependency attempts and output
    digests approved at preflight time (F-01). Earlier synthesis attempts are
    never modified. On normal completion the new attempt becomes the selected
    one (event ``attempt_selected``), the usage selected cache is refreshed
    from the derived value, and result.md mirrors the new attempt output; on
    failure the selection is unchanged. Rerun is never triggered automatically
    by regeneration, staleness detection, timeouts or shutdown.

    Preconditions (all before any provider call, refusals writing nothing):
    (0) ``evidence_version >= 2`` (M-6); (1) approval scope ``synthesis_rerun``
    with digest identity and target binding; (2) every declared dependency's
    currently selected attempt is SUCCEEDED with completion_complete True;
    (3) the pre-synthesis known-cost gate against the DERIVED selected worker
    cost (never cumulative spend, never a stored cache); (4) the approval is
    unspent; (5) selection/bytes binding — the currently selected dependency
    attempts and the sha256 of their output bytes equal the snapshot's
    ``dependency_attempts``/``dependency_digests``, else
    ApprovalInvalidatedError BEFORE dispatch.
    """
    validate_run_id(run_id)
    if not isinstance(providers, Mapping):
        raise Bots5Error("providers must be a mapping of provider IDs to providers")
    if job.synthesis is None:
        raise ValidationError("the supplied job declares no synthesis stage; nothing to rerun")
    synth = job.synthesis
    validate_stage_id(synth.id, "synthesis stage id")

    stage_order = _require_v2_operation_target(run_dir, run_id)
    if synth.id not in stage_order:
        raise ValidationError(
            f"synthesis stage {synth.id!r} is not declared in run.json stage_order"
        )

    resolved = read_json(run_dir / "job.resolved.json")
    if not isinstance(resolved, dict):
        raise ValidationError("job.resolved.json must be a JSON object")
    _require_resolved_job_matches(job, resolved)

    live_digest, _input_texts, system_messages = _rebuild_live_preflight_digest(job)
    approved_attempt = _require_operation_snapshot_binding(
        snapshot=snapshot,
        approval=approval,
        operation="synthesis_rerun",
        scope=_SYNTHESIS_RERUN_SCOPE,
        run_id=run_id,
        stage_id=synth.id,
        model=synth.model,
        live_digest=live_digest,
    )

    provider = _operation_provider(providers, synth.provider)
    _require_operation_provider_route(
        resolved=resolved,
        job=job,
        provider_id=synth.provider,
        provider=provider,
        snapshot_route=snapshot.provider_route,
    )

    dirs = _operation_run_dirs(run_dir)
    selection = read_selection(dirs.root)

    # Precondition 2: dependency gate with the existing gate semantics.
    blocked: list[str] = []
    for dep in synth.depends_on:
        if dep not in stage_order:
            raise ValidationError(f"declared dependency {dep!r} is not in run.json stage_order")
        attempt = selection.get(dep, 1)
        meta = read_json(dirs.root / "stages" / f"{dep}.att{attempt}.json")
        if not isinstance(meta, dict):
            raise ValidationError(
                f"dependency attempt document must be a JSON object: {dep}.att{attempt}"
            )
        completion = meta.get("completion")
        complete = completion.get("complete") if isinstance(completion, dict) else None
        if meta.get("state") != StageState.SUCCEEDED.value or complete is not True:
            blocked.append(
                f"{dep} (selected attempt {attempt}: state={meta.get('state')!r}, "
                f"completion_complete={complete!r})"
            )
    if blocked:
        raise ValidationError(
            "synthesis rerun refused: declared dependencies are not normally complete: "
            + "; ".join(blocked)
        )

    # Precondition 3: known-cost gate on the DERIVED selected worker cost.
    threshold = job.execution.stop_before_synthesis_if_known_cost_exceeds_usd
    if threshold is not None:
        derived = derive_selected_spend(
            _per_attempt_usage_map(load_attempt_records(dirs.root, stage_order)),
            selection,
            [worker.id for worker in job.workers],
        )
        derived_sum = Decimal(derived["cost_usd_known_sum"])
        if derived_sum > threshold:
            raise ValidationError(
                f"synthesis rerun refused: derived selected worker cost {derived_sum} "
                f"exceeds synthesis gate {threshold}"
            )

    # Precondition 4: one-shot probe.
    _require_approval_not_consumed(dirs.root, approval)

    # Precondition 5: selection/bytes binding (F-01) — BEFORE dispatch.
    binding_attempts: dict[str, int] = {}
    binding_digests: dict[str, str] = {}
    dependency_texts: list[tuple[str, str]] = []
    for dep in synth.depends_on:
        attempt = selection.get(dep, 1)
        output_path = dirs.root / "stages" / f"{dep}.att{attempt}.md"
        data = _read_operation_bytes(output_path, "dependency output")
        binding_attempts[dep] = attempt
        binding_digests[dep] = hashlib.sha256(data).hexdigest()
        dependency_texts.append(
            (dep, _decode_operation_bytes(data, output_path, "dependency output"))
        )
    if snapshot.dependency_attempts != binding_attempts:
        raise ApprovalInvalidatedError(
            "selected dependency attempts differ from the approved operation snapshot "
            "binding (selection/bytes binding)"
        )
    if snapshot.dependency_digests != binding_digests:
        raise ApprovalInvalidatedError(
            "selected dependency output bytes differ from the approved operation snapshot "
            "binding (selection/bytes binding)"
        )
    if snapshot.user_message != render_synthesis_user_message(dependency_texts):
        raise ApprovalInvalidatedError(
            "frozen synthesis user message does not match the approved dependency bytes"
        )
    if snapshot.system_message != system_messages[synth.id]:
        raise ApprovalInvalidatedError(
            "frozen synthesis system message does not match the verified contract bytes"
        )
    expected_pricing = build_pricing_evidence(
        [{"id": synth.id, "provider": synth.provider, "model": synth.model,
          "max_output_tokens": synth.max_output_tokens,
          "compiled_prompt_text": snapshot.system_message + "\n" + snapshot.user_message}],
        declared_provider_routes(job), approval.pricing_evidence,
    )
    if snapshot.provider_route is not None and snapshot.provider_route.get("kind") != "local_openai" and approval.pricing_evidence is None:
        raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
    if canonical_json(expected_pricing) != canonical_json(approval.pricing_evidence):
        raise ApprovalInvalidatedError("approval pricing evidence does not match synthesis route/model/ceiling")

    # Execution step 1: M is the binding attempt number.
    derived_attempt = next_attempt_number(dirs.root, synth.id)
    _require_binding_attempt_number(approved_attempt, derived_attempt)

    events = EventWriter(dirs.events, run_id)
    consume_approval(
        dirs.root,
        approval.approval_id,
        {**approval.to_dict(), "consumed_at": now_iso()},
    )

    # Execution step 2: exclusive-create claim carrying the FROZEN provenance
    # (copied from the snapshot, never re-derived) — persisted before dispatch,
    # so a crash cannot leave a dispatched attempt without provenance (M-2).
    record = StageRecord(
        id=synth.id,
        provider=synth.provider,
        requested_model=synth.model,
        consumed_dependencies=dict(snapshot.dependency_attempts),
        dependency_digests=dict(snapshot.dependency_digests),
    )
    record.preflight_digest = snapshot.preflight_digest
    persist_stage_attempt(dirs, record, attempt_number=derived_attempt, create=True)
    _materialize_default_selection(dirs.root, run_id, stage_order)

    events.write(
        "synthesis_rerun_started",
        synth.id,
        attempt_number=derived_attempt,
        model=synth.model,
    )
    try:
        output_text = await _execute_stage(
            spec=synth,
            record=record,
            system_message=snapshot.system_message,
            user_message=snapshot.user_message,
            provider=provider,
            semaphore=asyncio.Semaphore(1),
            writers=_Writers(dirs, v2=True),
            events=events,
            attempt_number=derived_attempt,
        )
    except asyncio.CancelledError:
        _terminalize_operation_cancellation(
            dirs=dirs,
            events=events,
            stage_order=stage_order,
            record=record,
            attempt_number=derived_attempt,
            operation_finished_event="synthesis_rerun_finished",
        )
        raise

    # §4 write ordering: completion write (per_attempt + cumulative) first,
    # then selection.json, then the selected-cache refresh. On failure the
    # selection is unchanged and only the completion write happens.
    _persist_operation_usage(dirs, stage_order)
    if output_text is not None and _stage_completed_successfully(record):
        current = read_selection(dirs.root)
        previous = current.get(synth.id, 1)
        write_selection(dirs.root, run_id, {**current, synth.id: derived_attempt})
        events.write(
            "attempt_selected",
            synth.id,
            attempt_number=derived_attempt,
            previous_attempt=previous,
        )
        persist_result(dirs, output_text)
        _persist_operation_usage(dirs, stage_order)
    events.write(
        "synthesis_rerun_finished",
        synth.id,
        attempt_number=derived_attempt,
        state=record.state.value,
    )
    return record
