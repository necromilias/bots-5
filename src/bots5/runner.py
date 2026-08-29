from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path

from .errors import Bots5Error, ProviderError, StorageError
from .events import EventWriter, now_iso
from .manifest import validate_referenced_files
from .models import Job, RunResult, RunState, StageRecord, StageState, SynthesisSpec, WorkerSpec
from .prompts import compile_worker_system_message
from .providers.base import CompletionRequest, CompletionResult, Provider
from .rendering import render_synthesis_user_message, render_worker_user_message
from .storage import (
    RunDirs,
    create_run_tree,
    new_run_id,
    persist_resolved_job,
    persist_result,
    persist_run,
    persist_stage,
    persist_usage,
)
from .usage import aggregate_cost


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


def _best_effort_internal_failure(
    *,
    dirs: RunDirs,
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
        if record.state in (StageState.QUEUED, StageState.RUNNING):
            record.state = StageState.FAILED
            record.error_type = "internal_error"
            record.error_message = message
            record.ended_at = now_iso()
            try:
                persist_stage(dirs, record)
            except StorageError:
                pass
    try:
        events.write("run_failed", error_type="internal_error", message=message)
    except StorageError:
        pass
    try:
        persist_usage(dirs, records)
    except StorageError:
        pass
    try:
        persist_run(
            dirs,
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
    dirs: RunDirs,
    events: EventWriter,
    run_id: str,
    started_at: str,
    records: list[StageRecord],
    run_timeout_seconds: float,
    exc: BaseException,
    synthesis_skipped_reason: str | None,
) -> None:
    _best_effort_internal_failure(
        dirs=dirs,
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
    dirs: RunDirs,
    events: EventWriter,
) -> str | None:
    started_monotonic: float | None = None
    try:
        async with semaphore:
            record.state = StageState.RUNNING
            record.started_at = now_iso()
            started_monotonic = time.monotonic()
            persist_stage(dirs, record)
            events.write("stage_started", record.id)
            request = CompletionRequest(
                model=spec.model,
                system=system_message,
                user=user_message,
                temperature=spec.temperature,
                max_output_tokens=spec.max_output_tokens,
                timeout_seconds=spec.timeout_seconds,
            )
            events.write("request_sent", record.id, model=spec.model)
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
                persist_stage(dirs, record)
                events.write("stage_failed", record.id, error_type=record.error_type)
                return None
            except ProviderError as exc:
                record.state = StageState.FAILED
                record.error_type = type(exc).__name__
                record.error_message = _failure_message(exc)
                record.ended_at = now_iso()
                record.duration_seconds = (
                    None if started_monotonic is None else time.monotonic() - started_monotonic
                )
                persist_stage(dirs, record)
                events.write("stage_failed", record.id, error_type=record.error_type)
                return None
            except Exception as exc:
                record.state = StageState.FAILED
                record.error_type = "internal_error"
                record.error_message = _failure_message(exc)
                record.ended_at = now_iso()
                record.duration_seconds = (
                    None if started_monotonic is None else time.monotonic() - started_monotonic
                )
                persist_stage(dirs, record)
                events.write("stage_failed", record.id, error_type=record.error_type)
                return None

            _apply_result(record, result)
            record.state = StageState.SUCCEEDED
            record.ended_at = now_iso()
            if record.duration_seconds == 0.0 and started_monotonic is not None:
                record.duration_seconds = time.monotonic() - started_monotonic
            persist_stage(dirs, record, result.output_text)
            events.write("stage_succeeded", record.id)
            return result.output_text

    except asyncio.CancelledError:
        if record.state not in (StageState.SUCCEEDED, StageState.FAILED):
            record.state = StageState.FAILED
            record.error_type = "run_timed_out"
            record.error_message = "stage did not finish before overall run timeout"
            record.provider_side_outcome_unknown = started_monotonic is not None
            record.ended_at = now_iso()
            if started_monotonic is not None:
                record.duration_seconds = time.monotonic() - started_monotonic
            try:
                persist_stage(dirs, record)
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


async def run_job(job: Job, provider: Provider, *, run_id: str | None = None) -> RunResult:
    validate_referenced_files(job)
    specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
    if job.synthesis is not None:
        specs += (job.synthesis,)
    system_messages = {
        spec.id: compile_worker_system_message(_read_utf8(spec.system_prompt_path)) for spec in specs
    }
    input_texts = [(item.label, _read_utf8(item.path)) for item in job.inputs]
    worker_user = render_worker_user_message(input_texts)

    run_id = run_id or new_run_id(job.name)
    dirs = create_run_tree(job.output.runs_dir, run_id)
    events = EventWriter(dirs.events, run_id)
    started_at = now_iso()
    records = _all_stage_records(job)
    record_by_id = {record.id: record for record in records}
    synthesis_skipped_reason: str | None = None

    try:
        persist_resolved_job(dirs, job)
        persist_usage(dirs, records)
        persist_run(
            dirs,
            run_id=run_id,
            state=RunState.RUNNING,
            started_at=started_at,
            ended_at=None,
            stages=records,
            run_timeout_seconds=job.execution.run_timeout_seconds,
        )
        events.write("run_started")
        for record in records:
            persist_stage(dirs, record)
            events.write("stage_queued", record.id)
    except Exception as exc:
        _raise_after_best_effort_failure(
            dirs=dirs,
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
                    provider=provider,
                    semaphore=semaphore,
                    dirs=dirs,
                    events=events,
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
            persist_stage(dirs, synth_record)
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
            persist_stage(dirs, synth_record)
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
            persist_stage(dirs, synth_record)
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
        synthesis_user = render_synthesis_user_message(dependencies)
        synthesis_output = await _execute_stage(
            spec=synth,
            record=synth_record,
            system_message=system_messages[synth.id],
            user_message=synthesis_user,
            provider=provider,
            semaphore=semaphore,
            dirs=dirs,
            events=events,
        )
        if synthesis_output is not None and synth_record.state == StageState.SUCCEEDED:
            persist_result(dirs, synthesis_output)

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
                persist_stage(dirs, record)
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
                persist_stage(dirs, synth_record)

        final_state = RunState.TIMED_OUT
        events.write("run_timed_out")
    except Exception as exc:
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        _raise_after_best_effort_failure(
            dirs=dirs,
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
        persist_usage(dirs, records)
        if final_state == RunState.SUCCEEDED:
            events.write("run_succeeded")
        elif final_state == RunState.FAILED:
            events.write("run_failed")
        persist_run(
            dirs,
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
            dirs=dirs,
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
