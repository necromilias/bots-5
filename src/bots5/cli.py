from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from .errors import ApprovalInvalidatedError, Bots5Error, ValidationError
from .events import now_iso
from .manifest import load_job, validate_referenced_files
from .models import ApprovalRecord, Job, OperationSnapshot, RunResult, StageRecord
from .paths import locate_run_dir
from .providers.base import Provider
from .providers.openai_compatible import OpenAICompatibleProvider
from .providers.openrouter import OpenRouterProvider
from .rendering import render_synthesis_user_message, render_worker_user_message
from .runner import (
    _SYNTHESIS_RERUN_SCOPE,
    _WORKER_REGENERATION_SCOPE_PREFIX,
    _require_v2_operation_target,
    build_preflight_snapshot,
    build_pricing_evidence,
    declared_provider_routes,
    regenerate_worker,
    rerun_synthesis,
    run_job,
)
from .storage import (
    EVIDENCE_VERSION_V2,
    load_run_view,
    load_stage_view,
    next_attempt_number,
    read_json,
    read_selection,
    reconstruct_run_state,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bots5")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a job without API calls")
    validate.add_argument("job", type=Path)

    run = sub.add_parser("run", help="execute a validated job")
    run.add_argument("job", type=Path)

    status = sub.add_parser("status", help="show a persisted run")
    status.add_argument("run_id")
    status.add_argument("--runs-dir", type=Path, default=None)

    inspect = sub.add_parser("inspect", help="show a persisted stage")
    inspect.add_argument("run_id")
    inspect.add_argument("stage_id")
    inspect.add_argument("--runs-dir", type=Path, default=None)
    inspect.add_argument(
        "--attempt",
        type=int,
        default=None,
        metavar="N",
        help="read exactly this evidence-v2 attempt (default: the selected attempt)",
    )

    regenerate = sub.add_parser(
        "regenerate",
        help="explicit sibling worker regeneration (preflight-only without consent)",
    )
    regenerate.add_argument("run_id")
    regenerate.add_argument("stage_id")
    regenerate.add_argument("--model", required=True, help="requested model for the new attempt")
    regenerate.add_argument("--job", type=Path, required=True, help="the run's original job file")
    regenerate.add_argument("--runs-dir", type=Path, default=None)
    regenerate_consent = regenerate.add_mutually_exclusive_group()
    regenerate_consent.add_argument(
        "--approve",
        action="store_true",
        help="approve in-process and execute (requires --actor LABEL); without consent "
        "the verb is preflight-only and spends nothing",
    )
    regenerate_consent.add_argument(
        "--approval",
        type=Path,
        default=None,
        help="execute with a pre-built ApprovalRecord JSON",
    )
    regenerate.add_argument("--actor", default=None, help="operator label recorded with --approve")
    regenerate.add_argument("--pricing", type=Path, default=None,
                            help="JSON operator pricing entries for paid routes")

    rerun = sub.add_parser(
        "rerun-synthesis",
        help="explicit synthesis rerun (preflight-only without consent)",
    )
    rerun.add_argument("run_id")
    rerun.add_argument("--job", type=Path, required=True, help="the run's original job file")
    rerun.add_argument("--runs-dir", type=Path, default=None)
    rerun_consent = rerun.add_mutually_exclusive_group()
    rerun_consent.add_argument(
        "--approve",
        action="store_true",
        help="approve in-process and execute (requires --actor LABEL); without consent "
        "the verb is preflight-only and spends nothing",
    )
    rerun_consent.add_argument(
        "--approval",
        type=Path,
        default=None,
        help="execute with a pre-built ApprovalRecord JSON",
    )
    rerun.add_argument("--actor", default=None, help="operator label recorded with --approve")
    rerun.add_argument("--pricing", type=Path, default=None,
                       help="JSON operator pricing entries for paid routes")

    return parser


def _cost_text(stage: dict) -> str:
    return stage.get("cost_usd") if stage.get("cost_known") else "?"


def _completion_text(stage: dict) -> str:
    completion = stage.get("completion")
    if completion is None:
        return "not_available"
    state = "complete" if completion.get("complete") is True else "incomplete"
    return f"{state} finish_reason={completion.get('finish_reason')!r}"


def _print_result(result: RunResult) -> None:
    print(f"run_id: {result.run_id}")
    print(f"state: {result.state.value}")
    for stage in result.stages:
        cost = "?" if stage.known_cost_usd is None else str(stage.known_cost_usd)
        completion = (
            "not_available"
            if stage.completion_complete is None
            else (
                f"{'complete' if stage.completion_complete else 'incomplete'} "
                f"finish_reason={stage.finish_reason!r}"
            )
        )
        print(
            f"{stage.id}: state={stage.state.value} completion={completion} cost={cost}"
        )


def _cmd_validate(path: Path) -> int:
    job = load_job(path)
    validate_referenced_files(job)
    print(f"OK: {job.name}")
    return 0


def _declared_provider_ids(job: Job) -> set[str]:
    ids = {worker.provider for worker in job.workers}
    if job.synthesis is not None:
        ids.add(job.synthesis.provider)
    return ids


def _build_providers(job: Job) -> dict[str, Provider]:
    provider_ids = _declared_provider_ids(job)
    providers: dict[str, Provider] = {}
    if "openrouter" in provider_ids:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise Bots5Error("OPENROUTER_API_KEY is not set")
        providers["openrouter"] = OpenRouterProvider(api_key)
    if "local_openai" in provider_ids:
        config = job.providers.local_openai
        if config is None:
            raise Bots5Error("local_openai provider configuration is missing")
        providers["local_openai"] = OpenAICompatibleProvider(
            config.base_url,
            api_key_env=config.api_key_env,
        )
    return providers


def _cmd_run(path: Path) -> int:
    job = load_job(path)
    validate_referenced_files(job)
    providers = _build_providers(job)
    result = asyncio.run(run_job(job, providers))
    _print_result(result)
    return result.exit_code


def _cmd_status(run_id: str, runs_dir: Path | None) -> int:
    run_dir = locate_run_dir(run_id, runs_dir)
    run, stages, usage = load_run_view(run_dir)
    # Phase 10: attempt/staleness markers are computed BEFORE anything is
    # printed, so a malformed evidence-v2 directory fails closed without
    # printing a partial view. Version 1 output stays byte-identical.
    markers: list[str] = []
    if run.get("evidence_version", 1) >= EVIDENCE_VERSION_V2:
        markers = _v2_status_markers(run_dir, stages)
    print(f"run_id: {run['run_id']}")
    print(f"state: {run['state']}")
    for stage in stages:
        failure = stage.get("failure")
        failure_text = ""
        if failure:
            failure_text = f" failure={failure.get('type')}: {failure.get('message')}"
        usage_s = stage.get("usage") or {}
        print(
            f"{stage['stage_id']}: model={stage['requested_model']} state={stage['state']} "
            f"duration={stage.get('duration_seconds')} "
            f"tokens={usage_s.get('total_tokens')} cost={_cost_text(stage)} "
            f"completion={_completion_text(stage)} "
            f"output={stage.get('output_path')}{failure_text}"
        )
    agg = usage.get("aggregate", {})
    print(
        "aggregate_cost: "
        f"{agg.get('cost_usd_known_sum')} "
        f"status={agg.get('cost_status')} "
        f"complete={agg.get('cost_complete')}"
    )
    for line in markers:
        print(line)
    return 0 if run.get("state") == "succeeded" else 1


def _v2_status_markers(run_dir: Path, stages: list[dict[str, Any]]) -> list[str]:
    """Appended evidence-v2 markers (CAMPAIGN_EVIDENCE_EVOLUTION.md §6.4).

    Disk-only reconstruction: per-stage selected attempt plus, for the
    synthesis stage, the freshness classification with any evidence-integrity
    warning (Mick O-1: NOT_APPLICABLE is never reported as an integrity
    defect).
    """
    state = reconstruct_run_state(run_dir)
    lines: list[str] = []
    for stage in stages:
        entry = state["stages"].get(stage["stage_id"])
        if entry is not None:
            lines.append(f"{stage['stage_id']}: attempt={entry['selected_attempt']}")
    for entry in state["stages"].values():
        freshness = entry.get("synthesis_freshness")
        if not freshness:
            continue
        lines.append(f"synthesis_freshness: {freshness['classification']}")
        for warning in freshness.get("warnings", []):
            lines.append(f"integrity_warning: {warning}")
    return lines


def _cmd_inspect(
    run_id: str, stage_id: str, runs_dir: Path | None, attempt: int | None
) -> int:
    run_dir = locate_run_dir(run_id, runs_dir)
    meta, output = load_stage_view(run_dir, stage_id, attempt_number=attempt)
    print(f"stage_id: {meta['stage_id']}")
    print(f"model: {meta['requested_model']}")
    print(f"state: {meta['state']}")
    completion = meta.get("completion")
    if completion is None:
        print("completion: not_available")
        print("finish_reason: None")
    else:
        completion_state = "complete" if completion.get("complete") is True else "incomplete"
        print(f"completion: {completion_state}")
        print(f"finish_reason: {completion.get('finish_reason')!r}")
    print(f"metadata: {meta}")
    if output is not None:
        print("--- output ---")
        print(output, end="" if output.endswith("\n") else "\n")
    failure = meta.get("failure")
    if failure:
        print("--- failure ---")
        print(f"{failure.get('type')}: {failure.get('message')}")
    return 0


# --- Phase 10 M0.4: headless parity verbs (M-5 consent form) -----------------


def _new_approval_id() -> str:
    """A fresh filesystem-safe one-shot approval id (approvals/<id>.json)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"approval-{stamp}-{uuid4().hex[:8]}"


def _read_operation_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"referenced file is unreadable: {path}: {exc}") from None


def _decode_operation_text(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"referenced file is not valid UTF-8: {path}") from exc


def _prepare_operation(
    *,
    operation: str,
    job: Job,
    run_dir: Path,
    run_id: str,
    stage_id: str,
    model: str,
) -> OperationSnapshot:
    """Zero-spend operation preflight: build the frozen OperationSnapshot.

    Reads the job and the run directory only — no provider is constructed, no
    provider request is made and nothing is written (PREFLIGHT_APPROVAL_STATE_
    MACHINE.md REGEN_APPROVED/RERUN_APPROVED preconditions; the spend refusal
    semantics stay engine-side). The engine entry points re-verify every
    binding against disk immediately before dispatch, so a stale snapshot or a
    moved selection is refused before any provider call.
    """
    stage_order = _require_v2_operation_target(run_dir, run_id)
    if stage_id not in stage_order:
        raise ValidationError(f"stage {stage_id!r} is not declared in run.json stage_order")

    dependency_attempts: dict[str, int] = {}
    dependency_digests: dict[str, str] = {}
    if operation == "worker_regeneration":
        spec = next((worker for worker in job.workers if worker.id == stage_id), None)
        if spec is None:
            if job.synthesis is not None and job.synthesis.id == stage_id:
                raise ValidationError(
                    f"stage {stage_id!r} is the synthesis stage; worker regeneration never "
                    "targets synthesis (use rerun-synthesis)"
                )
            raise ValidationError(
                f"stage {stage_id!r} is not a declared worker of the supplied job"
            )
        user_message = render_worker_user_message(
            [(item.label, _decode_operation_text(_read_operation_bytes(item.path), item.path))
             for item in job.inputs]
        )
    else:  # synthesis_rerun
        if job.synthesis is None:
            raise ValidationError("the supplied job declares no synthesis stage; nothing to rerun")
        spec = job.synthesis
        selection = read_selection(run_dir)
        dependency_texts: list[tuple[str, str]] = []
        for dep in spec.depends_on:
            if dep not in stage_order:
                raise ValidationError(
                    f"declared dependency {dep!r} is not in run.json stage_order"
                )
            attempt = selection.get(dep, 1)
            output_path = run_dir / "stages" / f"{dep}.att{attempt}.md"
            data = _read_operation_bytes(output_path)
            dependency_attempts[dep] = attempt
            dependency_digests[dep] = hashlib.sha256(data).hexdigest()
            dependency_texts.append((dep, _decode_operation_text(data, output_path)))
        user_message = render_synthesis_user_message(dependency_texts)

    preflight = build_preflight_snapshot(job)
    route = declared_provider_routes(job).get(spec.provider)
    if route is None:
        raise ValidationError(f"job declares no provider route for {spec.provider!r}")
    snapshot = OperationSnapshot(
        operation=operation,
        target_run_id=run_id,
        stage_id=stage_id,
        attempt_number=next_attempt_number(run_dir, stage_id),
        model=model,
        provider_route=dict(route),
        system_message=preflight.system_messages[stage_id],
        user_message=user_message,
        dependency_attempts=dependency_attempts,
        dependency_digests=dependency_digests,
        preflight_digest=preflight.preflight_digest,
        operation_digest="",
    )
    return replace(snapshot, operation_digest=snapshot.compute_digest())


def _load_approval_record(path: Path) -> ApprovalRecord:
    """Load a pre-built ApprovalRecord JSON (typed refusal, nothing written)."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValidationError(f"approval record must be a JSON object: {path}")
    try:
        return ApprovalRecord.from_dict(data)
    except (KeyError, TypeError) as exc:
        raise ValidationError(f"malformed approval record {path}: {exc}") from None


def _operation_consent(
    *, approve: bool, actor: str | None, approval_path: Path | None
) -> tuple[bool, str | None, ApprovalRecord | None]:
    """Resolve the M-5 headless consent form without touching any run state.

    Returns ``(consented, actor_label, approval)``. ``approval`` is None for
    the ``--approve`` form (it is constructed in-process, bound to the
    just-prepared operation snapshot, by the caller).
    """
    if approval_path is not None:
        record = _load_approval_record(approval_path)
        return True, record.approved_by, record
    if approve:
        if actor is None or not actor.strip():
            raise Bots5Error("--approve requires a non-empty --actor LABEL")
        return True, actor.strip(), None
    return False, None, None


def _load_pricing_input(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValidationError(f"pricing evidence must be a JSON object: {path}")
    return data


def _in_process_approval(
    *, snapshot: OperationSnapshot, scope: str, actor: str,
    pricing_evidence: dict[str, Any] | None = None,
) -> ApprovalRecord:
    """An ApprovalRecord bound to the just-prepared operation snapshot."""
    return ApprovalRecord(
        approval_id=_new_approval_id(),
        approved_at=now_iso(),
        approved_by=actor,
        preflight_digest=snapshot.preflight_digest,
        scope=scope,
        target={
            "run_id": snapshot.target_run_id,
            "stage_id": snapshot.stage_id,
            "attempt_number": snapshot.attempt_number,
        },
        pricing_evidence=pricing_evidence,
    )


def _print_operation_preflight(
    *,
    operation: str,
    run_id: str,
    stage_id: str,
    model: str,
    snapshot: OperationSnapshot,
    scope: str,
    pricing_evidence: dict[str, Any] | None = None,
) -> None:
    route = json.dumps(snapshot.provider_route, sort_keys=True)
    print(f"operation: {operation}")
    print(f"run_id: {run_id}")
    print(f"stage_id: {stage_id}")
    print(f"model: {model}")
    print(f"attempt: {snapshot.attempt_number}")
    print(f"provider_route: {route}")
    print(f"scope: {scope}")
    print(f"pricing_evidence: {json.dumps(pricing_evidence, sort_keys=True)}")
    print(f"preflight_digest: {snapshot.preflight_digest}")


def _print_operation_stage_summary(record: StageRecord) -> None:
    cost = "?" if record.known_cost_usd is None else str(record.known_cost_usd)
    print(f"{record.id}: state={record.state.value} attempt={record.attempt_number} cost={cost}")


def _cmd_regenerate(args: argparse.Namespace) -> int:
    job = load_job(args.job)
    validate_referenced_files(job)
    run_dir = locate_run_dir(args.run_id, args.runs_dir)
    model = args.model
    if not isinstance(model, str) or not model.strip():
        raise ValidationError("regeneration requires a non-empty requested model string")
    stage_id = args.stage_id
    snapshot = _prepare_operation(
        operation="worker_regeneration",
        job=job,
        run_dir=run_dir,
        run_id=args.run_id,
        stage_id=stage_id,
        model=model,
    )
    scope = f"{_WORKER_REGENERATION_SCOPE_PREFIX}{stage_id}"
    consented, actor, approval = _operation_consent(
        approve=args.approve, actor=args.actor, approval_path=args.approval
    )
    pricing_input = _load_pricing_input(args.pricing)
    if pricing_input is None and approval is not None:
        pricing_input = approval.pricing_evidence
    spec = next(worker for worker in job.workers if worker.id == stage_id)
    pricing_stages = [{"id": stage_id, "provider": spec.provider, "model": model,
                       "max_output_tokens": spec.max_output_tokens,
                       "compiled_prompt_text": snapshot.system_message + "\n" + snapshot.user_message}]
    pricing = build_pricing_evidence(pricing_stages, declared_provider_routes(job), pricing_input) if (consented or pricing_input is not None) else None
    if consented and snapshot.provider_route is not None and snapshot.provider_route.get("kind") != "local_openai" and pricing is None:
        raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
    if approval is not None and approval.pricing_evidence != pricing:
        raise ApprovalInvalidatedError("approval pricing evidence does not match the current operation")
    _print_operation_preflight(
        operation="worker_regeneration",
        run_id=args.run_id,
        stage_id=stage_id,
        model=model,
        snapshot=snapshot,
        scope=scope,
        pricing_evidence=pricing,
    )
    if not consented:
        print("consent: none (preflight only; no provider constructed, no request, nothing written)")
        return 0
    if approval is None:
        approval = _in_process_approval(
            snapshot=snapshot, scope=scope, actor=actor or "", pricing_evidence=pricing
        )
    print(f"approval_id: {approval.approval_id}")
    print(f"approved_by: {approval.approved_by}")
    providers = _build_providers(job)
    record = asyncio.run(
        regenerate_worker(
            job,
            providers,
            run_dir=run_dir,
            run_id=args.run_id,
            stage_id=stage_id,
            model=model,
            snapshot=snapshot,
            approval=approval,
        )
    )
    _print_operation_stage_summary(record)
    return 0


def _cmd_rerun_synthesis(args: argparse.Namespace) -> int:
    job = load_job(args.job)
    validate_referenced_files(job)
    if job.synthesis is None:
        raise ValidationError("the supplied job declares no synthesis stage; nothing to rerun")
    run_dir = locate_run_dir(args.run_id, args.runs_dir)
    stage_id = job.synthesis.id
    snapshot = _prepare_operation(
        operation="synthesis_rerun",
        job=job,
        run_dir=run_dir,
        run_id=args.run_id,
        stage_id=stage_id,
        model=job.synthesis.model,
    )
    consented, actor, approval = _operation_consent(
        approve=args.approve, actor=args.actor, approval_path=args.approval
    )
    pricing_input = _load_pricing_input(args.pricing)
    if pricing_input is None and approval is not None:
        pricing_input = approval.pricing_evidence
    spec = job.synthesis
    pricing_stages = [{"id": stage_id, "provider": spec.provider, "model": spec.model,
                       "max_output_tokens": spec.max_output_tokens,
                       "compiled_prompt_text": snapshot.system_message + "\n" + snapshot.user_message}]
    pricing = build_pricing_evidence(pricing_stages, declared_provider_routes(job), pricing_input) if (consented or pricing_input is not None) else None
    if consented and snapshot.provider_route is not None and snapshot.provider_route.get("kind") != "local_openai" and pricing is None:
        raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
    if approval is not None and approval.pricing_evidence != pricing:
        raise ApprovalInvalidatedError("approval pricing evidence does not match the current operation")
    _print_operation_preflight(
        operation="synthesis_rerun",
        run_id=args.run_id,
        stage_id=stage_id,
        model=job.synthesis.model,
        snapshot=snapshot,
        scope=_SYNTHESIS_RERUN_SCOPE,
        pricing_evidence=pricing,
    )
    if not consented:
        print("consent: none (preflight only; no provider constructed, no request, nothing written)")
        return 0
    if approval is None:
        approval = _in_process_approval(
            snapshot=snapshot, scope=_SYNTHESIS_RERUN_SCOPE, actor=actor or "",
            pricing_evidence=pricing,
        )
    print(f"approval_id: {approval.approval_id}")
    print(f"approved_by: {approval.approved_by}")
    providers = _build_providers(job)
    record = asyncio.run(
        rerun_synthesis(
            job,
            providers,
            run_dir=run_dir,
            run_id=args.run_id,
            snapshot=snapshot,
            approval=approval,
        )
    )
    _print_operation_stage_summary(record)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _cmd_validate(args.job)
        if args.command == "run":
            return _cmd_run(args.job)
        if args.command == "status":
            return _cmd_status(args.run_id, args.runs_dir)
        if args.command == "inspect":
            return _cmd_inspect(args.run_id, args.stage_id, args.runs_dir, args.attempt)
        if args.command == "regenerate":
            return _cmd_regenerate(args)
        if args.command == "rerun-synthesis":
            return _cmd_rerun_synthesis(args)
    except Bots5Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
