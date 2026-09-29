"""Qt-free campaign bridge: the single desktop/campaign seam (Phase 10 M1.0).

This module is the ONLY seam through which the desktop observes or operates the
campaign engine (MUTATION_FENCE.json "add"; DESKTOP_SURFACE_AND_LIFECYCLE.md
§2.1). It is deliberately Qt-free: no PySide6, no qasync, no desktop imports —
directly unit-testable with plain ``asyncio``.

Authority discipline (non-negotiable; PHASE10_CAMPAIGN_DESKTOP_DESIGN.md §0):

- The engine stays the only producer of campaign evidence. This bridge never
  writes campaign evidence itself except the explicit selection command
  (``CampaignBridge.select_attempt``), which goes through the engine's own
  storage helpers (``storage.write_selection`` + ``EventWriter``). There is no
  campaign event bus, no SQLite shadow state, no second truth: every read goes
  to the run directory.
- Zero-spend work (``load_job`` / ``validate`` / ``prepare_*``) constructs no
  provider, contacts no provider and writes nothing, so an abandoned approval
  leaves zero run-directory bytes (PREFLIGHT_APPROVAL_STATE_MACHINE.md §1).
- Providers are constructed in exactly one place (``approve_and_*``) and only
  after approval, through an injectable factory that defaults to the real
  route rules of ``cli._build_providers`` (duplicated here, never imported —
  cli is a headless surface, not a library).
- The projection (``CampaignProjection`` / ``project_run``) is built ONLY from
  durable filesystem state and obeys the desktop-must-NEVER rules
  (DESKTOP_SURFACE_AND_LIFECYCLE.md §4): live cost is the known subtotal plus
  the explicit unknown set — never a fabricated accrual (D-5); a durable
  ``running`` stage with a ``started_at`` is provider-side-outcome-unknown
  (D-4); a post-dispatch failure is unknown unless the durable record proves a
  definitive rejection (D-9); nothing here ever auto-retries (D-6); a durable
  ``cancelled_pending`` reads as interrupted/uncertain (N-4).
- Polling is bounded and read-only (§3): ``POLL_INTERVAL_MS``/``POLL_MAX_INTERVAL_MS``
  match the Phase 9 import-queue discipline; a projection reads a fixed, small
  set of run-directory files, and ``projection_async`` performs that read off
  the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import ApprovalInvalidatedError, Bots5Error, StorageError, ValidationError
from ..events import EventWriter, now_iso
from ..manifest import load_job, validate_referenced_files
from ..models import (
    ApprovalRecord,
    Job,
    OperationSnapshot,
    PreflightSnapshot,
    RunResult,
    RunState,
    StageRecord,
    StageState,
    SynthesisSpec,
    WorkerSpec,
)
from ..paths import locate_run_dir, validate_run_id, validate_stage_id
from ..providers.base import Provider
from ..providers.openai_compatible import OpenAICompatibleProvider
from ..providers.openrouter import OpenRouterProvider
from ..rendering import render_synthesis_user_message, render_worker_user_message
from ..runner import (
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
from ..storage import (
    EVIDENCE_VERSION_V2,
    attempt_paths,
    load_stage_view,
    new_run_id,
    next_attempt_number,
    read_json,
    read_selection,
    reconstruct_run_state,
    write_selection,
)
from ..usage import _spend_summary, derive_selected_spend, selected_cache_is_stale

__all__ = [
    "CampaignBridge",
    "CampaignProjection",
    "PreparedOperation",
    "StageProjection",
    "ProviderFactory",
    "POLL_INTERVAL_MS",
    "POLL_MAX_INTERVAL_MS",
    "project_run",
]

# Bounded polling contract (DESKTOP_SURFACE_AND_LIFECYCLE.md §3): the dock owns
# the timer; the bridge carries the same timings so every poller using this
# seam is bounded identically. A single projection reads a fixed small set of
# run-directory files and is bounded by construction well under the max.
POLL_INTERVAL_MS = 250
POLL_MAX_INTERVAL_MS = 1000

_FULL_RUN_SCOPE = "full_run"

# D-9 reader rule: durable ``error_type`` values that PROVE a known
# provider-side outcome despite dispatch. ``ProviderResponseError`` means a
# response WAS received (errors.py sets ``definitive_rejection = True`` for it).
# ``ProviderHttpError`` is deliberately absent: its status code (the 4xx
# excluding 408/429 distinction) is not durably recorded, so the reader
# fail-safes to unknown exactly like the engine's unclassified default.
_DEFINITIVE_DURABLE_ERROR_TYPES = frozenset({"ProviderResponseError"})

_TERMINAL_RUN_STATES = frozenset(
    state.value for state in RunState if state is not RunState.RUNNING
)

ProviderFactory = Callable[[Job], Mapping[str, Provider]]


# --- zero-spend preflight helpers (mirrors of the headless CLI form) ---------


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


def _default_provider_factory(job: Job) -> dict[str, Provider]:
    """The real provider construction, with the SAME route rules as
    ``cli._build_providers`` (duplicated verbatim; cli is never imported).

    Called by the bridge only inside ``approve_and_*`` — never during
    load/validate/prepare — so no provider object exists before approval.
    """
    provider_ids = {worker.provider for worker in job.workers}
    if job.synthesis is not None:
        provider_ids.add(job.synthesis.provider)
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


# --- projection --------------------------------------------------------------

# Durable truth types for the projection. Everything is optional/None when the
# durable record does not say; nothing is interpolated.


@dataclass(frozen=True)
class StageProjection:
    """What the UI may render about ONE stage (its selected attempt).

    Built only from the durable attempt metadata on disk; ``None`` means the
    durable record does not say. ``attempt_number`` is the SELECTED attempt
    (selection.json, default 1); ``available_attempts`` lists every attempt
    recorded on disk for the stage.
    """

    stage_id: str
    state: str | None
    requested_model: str | None
    attempt_number: int
    duration_seconds: float | None
    total_tokens: int | None
    cost_usd: str | None
    cost_known: bool
    completion_complete: bool | None
    error_type: str | None
    provider_side_outcome_unknown: bool
    output_path: str | None
    available_attempts: tuple[int, ...]


@dataclass(frozen=True)
class CampaignProjection:
    """Immutable view of one run directory, built ONLY from durable state.

    ``live_cost`` is the explicit live-cost field (D-5): the KNOWN subtotal
    plus the explicit unknown set over the selected attempts. No accrual,
    interpolation or token-dollar projection is ever fabricated; provider cost
    lands only at terminal completion. ``cumulative_spend`` (v2) is the
    financial truth across every attempt; ``selected_spend`` (v2) is the
    derived selected-pipeline spend (the authority, never the stored cache).

    ``display_state`` applies the §4.1 display classification: a durable
    ``running`` run WITHOUT a hosted live task is ``interrupted_uncertain`` —
    never success, failure, or resumable. ``hosted``/``is_running`` reflect the
    bridge's own live-task hosting (not filesystem evidence) and exist so the
    display rule stays honest.
    """

    run_id: str
    run_dir: Path
    run_state: str | None
    stage_order: tuple[str, ...]
    stages: tuple[StageProjection, ...]
    aggregate_cost: dict[str, Any]
    cumulative_spend: dict[str, Any]
    selected_spend: dict[str, Any]
    live_cost: dict[str, Any]
    synthesis_freshness: str | None
    integrity_warnings: tuple[str, ...]
    is_running: bool
    provider_side_outcome_unknown_stage_ids: tuple[str, ...]
    display_state: str
    hosted: bool
    evidence_version: int
    synthesis_freshness_report: dict[str, Any] | None


def _meta_str(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) else None


def _provider_side_outcome_unknown(
    *,
    state: str | None,
    started_at: str | None,
    error_type: str | None,
    stored_unknown: bool,
    failure_present: bool,
) -> bool:
    """The D-4/D-9 truth rules applied to one durable stage record.

    - D-4: a durable ``running`` stage with a ``started_at`` is UNKNOWN even
      when the stored flag is false by omission (the flag is only written on
      error branches). Never assert the provider stopped, billed zero, or
      produced nothing.
    - D-9: a post-dispatch failure is UNKNOWN unless the durable record proves
      a definitive rejection. ``started_at`` is set strictly before
      ``request_sent`` in this engine, so a failure without ``started_at``
      never reached a provider; a durable ``ProviderResponseError`` proves the
      response was received (definitive); every other post-dispatch failure —
      including ``ProviderHttpError`` (status not durably recorded),
      ``ProviderTimeoutError``, ``internal_error`` and ``request_timeout`` —
      stays conservatively UNKNOWN and is never auto-retried (D-6).
    - A durable ``cancelled_pending`` (state failed, N-4 hard-kill window)
      follows the same dispatch rule: interrupted/uncertain, never a normal
      outcome.
    """
    if state == "running":
        return True if started_at else stored_unknown
    if state == "failed":
        if not failure_present:
            return bool(started_at)
        if stored_unknown:
            return True
        if not started_at:
            return False
        if error_type in _DEFINITIVE_DURABLE_ERROR_TYPES:
            return False
        return True
    return stored_unknown


def _stage_projection(
    stage_id: str,
    meta: dict[str, Any] | None,
    selected_attempt: int,
    available_attempts: tuple[int, ...],
) -> StageProjection:
    if meta is None:
        # Transient pre-claim window: run.json exists but the engine has not
        # claimed this attempt yet (the claim happens synchronously inside the
        # hosted task, so a poller normally never sees this). Nothing was
        # dispatched; nothing is known. Nothing is fabricated.
        return StageProjection(
            stage_id=stage_id,
            state=StageState.QUEUED.value,
            requested_model=None,
            attempt_number=selected_attempt,
            duration_seconds=None,
            total_tokens=None,
            cost_usd=None,
            cost_known=False,
            completion_complete=None,
            error_type=None,
            provider_side_outcome_unknown=False,
            output_path=None,
            available_attempts=available_attempts,
        )
    usage = meta.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    completion = meta.get("completion")
    completion = completion if isinstance(completion, dict) else None
    failure = meta.get("failure")
    failure = failure if isinstance(failure, dict) else None
    state = _meta_str(meta, "state")
    started_at = _meta_str(meta, "started_at")
    error_type = None if failure is None else _meta_str(failure, "type")
    stored_unknown = False if failure is None else failure.get(
        "provider_side_outcome_unknown", False
    )
    if not isinstance(stored_unknown, bool):
        stored_unknown = False
    completion_complete = None if completion is None else completion.get("complete")
    if completion_complete is not None and not isinstance(completion_complete, bool):
        completion_complete = None
    total_tokens = usage.get("total_tokens")
    if isinstance(total_tokens, bool) or not isinstance(total_tokens, int):
        total_tokens = None
    duration = meta.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        duration = None
    cost_known = meta.get("cost_known") is True
    cost_usd = _meta_str(meta, "cost_usd") if cost_known else None
    return StageProjection(
        stage_id=stage_id,
        state=state,
        requested_model=_meta_str(meta, "requested_model"),
        attempt_number=selected_attempt,
        duration_seconds=float(duration) if duration is not None else None,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        cost_known=cost_known,
        completion_complete=completion_complete,
        error_type=error_type,
        provider_side_outcome_unknown=_provider_side_outcome_unknown(
            state=state,
            started_at=started_at,
            error_type=error_type,
            stored_unknown=stored_unknown,
            failure_present=failure is not None,
        ),
        output_path=_meta_str(meta, "output_path"),
        available_attempts=available_attempts,
    )


def _display_state(run_state: str | None, hosted: bool) -> str:
    """§4.1 display classification for the RUN level."""
    if run_state in _TERMINAL_RUN_STATES:
        return run_state
    if run_state == "running":
        return "running" if hosted else "interrupted_uncertain"
    # Unknown/absent durable state is never success/failure: interrupted.
    return "interrupted_uncertain"


def _durable_known_cost(meta: dict[str, Any]) -> Decimal | None:
    """The known cost of one durable attempt record, or None when unknown.

    Corruption never becomes invented money: an absent/false ``cost_known``,
    a null cost, or an unparseable decimal is UNKNOWN.
    """
    if meta.get("cost_known") is not True:
        return None
    value = meta.get("cost_usd")
    if not isinstance(value, str):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _durable_total_tokens(meta: dict[str, Any]) -> int | None:
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("total_tokens")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _selected_attempt_meta(
    run_dir: Path, stage_id: str, version: int, selected_attempt: int
) -> dict[str, Any] | None:
    if version >= EVIDENCE_VERSION_V2:
        meta_rel, _output_rel = attempt_paths(stage_id, selected_attempt)
        meta_path = run_dir / meta_rel
    else:
        meta_path = run_dir / "stages" / f"{stage_id}.json"
    if not meta_path.is_file():
        return None
    return read_json(meta_path)


def project_run(run_dir: Path, *, hosted: bool = False) -> CampaignProjection:
    """Build the immutable :class:`CampaignProjection` for one run directory.

    Bounded, read-only, fail-closed: version/selection/malformed-layout
    validation is delegated to the engine's own ``reconstruct_run_state``.
    ``hosted`` tells the display rule whether a LIVE task currently hosts this
    run (bridge context, not filesystem evidence).
    """
    run_dir = Path(run_dir).resolve(strict=False)
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    # Engine reader: validates evidence version, selection.json and the
    # malformed-v2-layout guard, and classifies synthesis freshness (O-1).
    state_map = reconstruct_run_state(run_dir)
    run = read_json(run_dir / "run.json")
    if not isinstance(run, dict):
        raise ValidationError(f"run document must be a JSON object: {run_dir / 'run.json'}")
    version = int(state_map["evidence_version"])
    run_id = state_map.get("run_id") or run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValidationError(f"run.json carries no usable run_id: {run_dir / 'run.json'}")
    raw_state = run.get("state")
    run_state = raw_state if isinstance(raw_state, str) else None
    order_raw = run.get("stage_order")
    stage_order = tuple(
        stage_id for stage_id in order_raw if isinstance(stage_id, str)
    ) if isinstance(order_raw, list) else ()
    selection = read_selection(run_dir) if version >= EVIDENCE_VERSION_V2 else {}

    warnings: list[str] = []
    stages: list[StageProjection] = []
    per_stage_state: dict[str, Any] = state_map.get("stages", {})
    for stage_id in stage_order:
        entry = per_stage_state.get(stage_id, {})
        selected = entry.get("selected_attempt", 1)
        if isinstance(selected, bool) or not isinstance(selected, int):
            selected = 1
        available_raw = entry.get("available_attempts") or []
        available = tuple(
            number for number in available_raw if isinstance(number, int) and not isinstance(number, bool)
        )
        meta = _selected_attempt_meta(run_dir, stage_id, version, selected)
        stages.append(_stage_projection(stage_id, meta, selected, available))

    # Cost truth: read the durable usage document; the derived selected spend
    # (F-07) is the authority and the stored cache is only a cache.
    usage_path = run_dir / "usage.json"
    usage_doc = read_json(usage_path) if usage_path.is_file() else None
    if usage_doc is not None and not isinstance(usage_doc, dict):
        raise ValidationError(f"usage document must be a JSON object: {usage_path}")
    aggregate_cost = (
        usage_doc.get("aggregate")
        if isinstance(usage_doc, dict) and isinstance(usage_doc.get("aggregate"), dict)
        else {}
    )
    cumulative_spend = (
        usage_doc.get("cumulative_spend")
        if isinstance(usage_doc, dict) and isinstance(usage_doc.get("cumulative_spend"), dict)
        else {}
    )
    selected_spend: dict[str, Any] = {}
    if version >= EVIDENCE_VERSION_V2 and isinstance(usage_doc, dict):
        per_attempt = usage_doc.get("per_attempt")
        if isinstance(per_attempt, dict):
            derived = derive_selected_spend(per_attempt, selection, list(stage_order))
            if selected_cache_is_stale(usage_doc.get("selected_spend"), derived):
                warnings.append(
                    "usage.json selected_spend cache disagrees with the derived selected "
                    "spend; the derived value is authoritative"
                )
            selected_spend = derived

    # Live cost (D-5): known subtotal + explicit unknown set over the SELECTED
    # attempts of this projection, with exactly the engine's status rules
    # (usage._spend_summary mirrors aggregate_cost verbatim). Never an accrual.
    cost_rows: list[tuple[str, Decimal | None, int | None]] = []
    for stage in stages:
        meta = _selected_attempt_meta(run_dir, stage.stage_id, version, stage.attempt_number)
        cost_rows.append(
            (
                stage.stage_id,
                None if meta is None else _durable_known_cost(meta),
                None if meta is None else _durable_total_tokens(meta),
            )
        )
    cost_summary = _spend_summary(cost_rows)
    live_cost = {
        "known_subtotal_usd": cost_summary["cost_usd_known_sum"],
        "unknown_stage_ids": list(cost_summary["unknown_cost_stage_ids"]),
        "status": cost_summary["cost_status"],
        "complete": cost_summary["cost_complete"],
        "total_tokens_known_sum": cost_summary["total_tokens_known_sum"],
        "basis": (
            "known subtotal plus explicit unknown set over the selected attempts; "
            "provider cost lands only at terminal completion; no accrual is fabricated"
        ),
    }

    # Synthesis freshness (mechanical, disk-only) + its integrity warnings.
    freshness_report: dict[str, Any] | None = None
    for entry in per_stage_state.values():
        report = entry.get("synthesis_freshness")
        if isinstance(report, dict):
            freshness_report = report
            break
    if freshness_report is not None:
        for warning in freshness_report.get("warnings") or []:
            if isinstance(warning, str):
                warnings.append(warning)

    unknown_stage_ids = tuple(
        stage.stage_id for stage in stages if stage.provider_side_outcome_unknown
    )
    return CampaignProjection(
        run_id=run_id,
        run_dir=run_dir,
        run_state=run_state,
        stage_order=stage_order,
        stages=tuple(stages),
        aggregate_cost=dict(aggregate_cost),
        cumulative_spend=dict(cumulative_spend),
        selected_spend=dict(selected_spend),
        live_cost=live_cost,
        synthesis_freshness=(
            None
            if freshness_report is None
            else freshness_report.get("classification")
        ),
        integrity_warnings=tuple(warnings),
        is_running=bool(hosted and run_state == "running"),
        provider_side_outcome_unknown_stage_ids=unknown_stage_ids,
        display_state=_display_state(run_state, hosted),
        hosted=bool(hosted),
        evidence_version=version,
        synthesis_freshness_report=freshness_report,
    )


# --- prepared operations -----------------------------------------------------


@dataclass(frozen=True)
class PreparedOperation:
    """The frozen output of one zero-spend ``prepare_*`` call.

    Exposes the frozen snapshot (``PreflightSnapshot`` for a full run,
    ``OperationSnapshot`` for regeneration/rerun), the one-shot
    ``ApprovalRecord`` DRAFT bound to its digest, and a human-readable
    preflight summary for the approval surface. Preparing wrote nothing and
    constructed no provider; approval consumption happens engine-side at
    execution (approvals/<id>.json, exclusive create).
    """

    operation: str
    scope: str
    actor: str
    job: Job
    job_path: Path | None
    run_id: str
    run_dir: Path
    stage_id: str | None
    model: str | None
    attempt_number: int | None
    snapshot: PreflightSnapshot | OperationSnapshot
    approval: ApprovalRecord
    provider_routes: dict[str, dict[str, Any]]
    summary: str


@dataclass(frozen=True)
class _HostedRun:
    """What the bridge currently hosts (for truthful cancel verification)."""

    operation: str
    run_id: str
    run_dir: Path
    stage_id: str | None
    attempt_number: int | None


# --- the bridge --------------------------------------------------------------


class CampaignBridge:
    """Qt-free operator seam over the campaign engine for ONE runs directory.

    Construction takes the runs directory (explicit operator-supplied path,
    HSF-3) and optional poll timings. Loading/validating/preparing is
    zero-spend and side-effect-free; providers are constructed only inside
    ``approve_and_*``, after the operator's approval, and the consequential
    work is hosted as an asyncio task on the caller's running loop.
    """

    def __init__(
        self,
        runs_dir: str | Path,
        *,
        provider_factory: ProviderFactory | None = None,
        poll_interval_ms: int = POLL_INTERVAL_MS,
        poll_max_interval_ms: int = POLL_MAX_INTERVAL_MS,
    ) -> None:
        self._runs_dir = Path(runs_dir).resolve(strict=False)
        self._provider_factory: ProviderFactory = (
            provider_factory if provider_factory is not None else _default_provider_factory
        )
        if isinstance(poll_interval_ms, bool) or not isinstance(poll_interval_ms, int) or poll_interval_ms <= 0:
            raise ValidationError("poll_interval_ms must be a positive integer")
        if (
            isinstance(poll_max_interval_ms, bool)
            or not isinstance(poll_max_interval_ms, int)
            or poll_max_interval_ms < poll_interval_ms
        ):
            raise ValidationError("poll_max_interval_ms must be an integer >= poll_interval_ms")
        self.poll_interval_ms = poll_interval_ms
        self.poll_max_interval_ms = poll_max_interval_ms
        self.poll_interval_seconds = poll_interval_ms / 1000
        self.poll_max_seconds = poll_max_interval_ms / 1000
        self._job: Job | None = None
        self._job_path: Path | None = None
        self._current_run_id: str | None = None
        self._current_run_dir: Path | None = None
        self._task: asyncio.Task[RunResult | StageRecord] | None = None
        self._hosted: _HostedRun | None = None
        self._consumed_approval_ids: set[str] = set()
        self._last_result: RunResult | StageRecord | None = None
        self._last_error: BaseException | None = None
        self._last_cancelled: bool = False
        self._closed: bool = False

    # -- accessors ------------------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    @property
    def job_path(self) -> Path | None:
        return self._job_path

    @property
    def current_run_id(self) -> str | None:
        return self._current_run_id

    @property
    def current_run_dir(self) -> Path | None:
        return self._current_run_dir

    @property
    def is_busy(self) -> bool:
        """True while a hosted campaign task is still running (single-flight)."""
        return self._task is not None and not self._task.done()

    # -- zero-spend load / validate -------------------------------------------

    def _job_summary(
        self, job: Job, job_path: Path, *, content_sha256: str | None = None
    ) -> dict[str, Any]:
        return {
            "job_name": job.name,
            "job_path": str(job_path),
            "schema_version": job.schema_version,
            "workers": [
                {"id": worker.id, "provider": worker.provider, "model": worker.model}
                for worker in job.workers
            ],
            "synthesis": None
            if job.synthesis is None
            else {
                "id": job.synthesis.id,
                "provider": job.synthesis.provider,
                "model": job.synthesis.model,
                "depends_on": list(job.synthesis.depends_on),
            },
            "inputs": [{"label": item.label, "path": str(item.path)} for item in job.inputs],
            "runs_dir": str(job.output.runs_dir),
            "execution": {
                "max_parallelism": job.execution.max_parallelism,
                "run_timeout_seconds": job.execution.run_timeout_seconds,
                "stop_before_synthesis_if_known_cost_exceeds_usd": (
                    None
                    if job.execution.stop_before_synthesis_if_known_cost_exceeds_usd is None
                    else str(job.execution.stop_before_synthesis_if_known_cost_exceeds_usd)
                ),
            },
            "content_sha256": content_sha256,
            "zero_spend": True,
        }

    def load_job(self, path: str | Path) -> dict[str, Any]:
        """Load (parse + schema-validate) a job from an explicit path.

        Zero spend: no provider is constructed, no run directory is created
        and nothing is written. Typed failures surface verbatim.
        """
        job_path = Path(path).resolve(strict=False)
        job = load_job(job_path)
        self._job = job
        self._job_path = job_path
        digest = None
        try:
            digest = hashlib.sha256(job_path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        return self._job_summary(job, job_path, content_sha256=digest)

    def validate(self) -> dict[str, Any]:
        """Zero-spend validation: ``manifest.load_job`` + ``validate_referenced_files``.

        Re-reads the loaded job from disk and validates every referenced input
        and contract file. Constructs no provider, contacts nothing, creates
        no run directory (the same side-effect-free validator pair as
        ``bots5 validate``).
        """
        if self._job_path is None:
            raise ValidationError("no job loaded; call load_job first")
        job = load_job(self._job_path)
        validate_referenced_files(job)
        self._job = job
        digest = None
        try:
            digest = hashlib.sha256(self._job_path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        return self._job_summary(job, self._job_path, content_sha256=digest)

    # -- current run adoption (explicit path/id only, HSF-3) ------------------

    def adopt_run(self, target: str | Path) -> Path:
        """Adopt an EXISTING run by explicit run id or explicit directory path.

        No enumeration, no discovery: the operator supplies the exact target.
        Reads run.json only to prove the run exists (via later operations);
        nothing is written.
        """
        if isinstance(target, Path):
            run_dir = target.resolve(strict=False)
            run_id = run_dir.name
            validate_run_id(run_id)
            if not run_dir.is_dir():
                raise ValidationError(f"run not found: {run_dir}")
        else:
            run_id = str(target)
            run_dir = locate_run_dir(run_id, self._runs_dir)
            if not run_dir.is_dir():
                raise ValidationError(f"run not found: {run_dir}")
        self._current_run_id = run_id
        self._current_run_dir = run_dir
        return run_dir

    # -- zero-spend prepare ----------------------------------------------------

    def _require_job(self, action: str) -> Job:
        if self._job is None or self._job_path is None:
            raise ValidationError(f"{action} requires a loaded job; call load_job first")
        return self._job

    def _require_current_run(self, action: str) -> tuple[str, Path]:
        if self._current_run_id is None or self._current_run_dir is None:
            raise ValidationError(
                f"{action} requires a current run; call adopt_run (or approve_and_start) first"
            )
        return self._current_run_id, self._current_run_dir

    def _require_open(self, action: str) -> None:
        if self._closed:
            raise Bots5Error(f"{action} refused: the campaign bridge is closed")

    def _require_idle(self, action: str) -> None:
        if self.is_busy:
            raise Bots5Error(
                f"{action} refused: a campaign task is already hosted "
                f"(run {self._hosted.run_id if self._hosted else '?'})"
            )

    def _validate_prepared_pricing(self, prepared: PreparedOperation) -> None:
        snapshot = prepared.snapshot
        routes = prepared.provider_routes
        if isinstance(snapshot, PreflightSnapshot):
            user = render_worker_user_message([
                (item.label, _decode_operation_text(_read_operation_bytes(item.path), item.path))
                for item in prepared.job.inputs
            ])
            specs: tuple[WorkerSpec | SynthesisSpec, ...] = prepared.job.workers
            if prepared.job.synthesis is not None:
                specs += (prepared.job.synthesis,)
            stages = [{"id": spec.id, "provider": spec.provider, "model": spec.model,
                       "max_output_tokens": spec.max_output_tokens,
                       "compiled_prompt_text": snapshot.system_messages[spec.id] + "\n" + user}
                      for spec in specs]
        else:
            spec = next((item for item in (*prepared.job.workers,
                         *((prepared.job.synthesis,) if prepared.job.synthesis else ()))
                         if item.id == snapshot.stage_id), None)
            if spec is None:
                raise ApprovalInvalidatedError("prepared approval stage no longer exists")
            stages = [{"id": spec.id, "provider": spec.provider, "model": snapshot.model,
                       "max_output_tokens": spec.max_output_tokens,
                       "compiled_prompt_text": (snapshot.system_message or "") + "\n" +
                       (snapshot.user_message or "")}]
        paid = any(routes.get(stage["provider"], {}).get("kind") != "local_openai" for stage in stages)
        if paid and prepared.approval.pricing_evidence is None:
            raise ApprovalInvalidatedError("paid approval requires complete operator pricing evidence")
        expected = build_pricing_evidence(stages, routes, prepared.approval.pricing_evidence)
        if expected != prepared.approval.pricing_evidence:
            raise ApprovalInvalidatedError("approval pricing evidence does not match current routes/models/ceilings")

    def _consume_approval(self, prepared: PreparedOperation) -> None:
        """Bridge-level one-shot guard; the durable one-shot is engine-side.

        Replaying the same prepared operation is refused with the typed error
        before anything happens (PREFLIGHT_APPROVAL_STATE_MACHINE.md §3.1: an
        approval is consumed once and never re-armed).
        """
        approval_id = prepared.approval.approval_id
        if approval_id in self._consumed_approval_ids:
            raise ApprovalInvalidatedError(f"approval already consumed: {approval_id}")
        self._consumed_approval_ids.add(approval_id)

    def _full_run_summary(
        self, *, job: Job, run_id: str, snapshot: PreflightSnapshot, approval: ApprovalRecord
    ) -> str:
        lines = [
            "operation: full_run",
            f"job: {job.name} (schema_version={job.schema_version})",
            f"job_path: {self._job_path}",
            f"run_id: {run_id} (the run directory is created only when the approval is consumed)",
            f"runs_dir: {job.output.runs_dir}",
            "stages:",
        ]
        specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
        if job.synthesis is not None:
            specs = specs + (job.synthesis,)
        for spec in specs:
            line = (
                f"  - {spec.id}: provider={spec.provider} model={spec.model} "
                f"temperature={spec.temperature} max_output_tokens={spec.max_output_tokens} "
                f"timeout_seconds={spec.timeout_seconds}"
            )
            if isinstance(spec, SynthesisSpec):
                line += f" depends_on={','.join(spec.depends_on)}"
            lines.append(line)
        lines.append(f"execution_limits: {json.dumps(snapshot.execution_limits, sort_keys=True)}")
        lines.append(f"provider_routes: {json.dumps(snapshot.provider_routes, sort_keys=True)}")
        lines.append(f"pricing_evidence: {json.dumps(approval.pricing_evidence, sort_keys=True)}")
        if approval.pricing_evidence is not None:
            for entry in approval.pricing_evidence["entries"]:
                lines.append(
                    f"pricing: route={entry['provider']} rates="
                    f"input={entry['input_usd_per_1m']} output={entry['output_usd_per_1m']} USD/1M "
                    f"source={entry['rate_source']!r} observed_at={entry['observed_at']} "
                    f"conservative_upper_bound_usd={entry['conservative_upper_bound_usd']} "
                    f"basis={entry['basis']}"
                )
        lines.append(f"preflight_digest: {snapshot.preflight_digest}")
        lines.append(
            f"approval_draft: {approval.approval_id} approved_by={approval.approved_by!r} "
            f"scope={approval.scope}"
        )
        lines.append(
            "spend_notice: approving constructs the providers and starts the run; "
            "preparing wrote nothing and constructed no provider"
        )
        return "\n".join(lines)

    def _operation_summary(
        self,
        *,
        operation: str,
        snapshot: OperationSnapshot,
        approval: ApprovalRecord,
        scope: str,
    ) -> str:
        lines = [
            f"operation: {operation}",
            f"run_id: {snapshot.target_run_id}",
            f"stage_id: {snapshot.stage_id}",
            f"model: {snapshot.model}",
            f"attempt: {snapshot.attempt_number}",
            f"provider_route: {json.dumps(snapshot.provider_route, sort_keys=True)}",
            f"pricing_evidence: {json.dumps(approval.pricing_evidence, sort_keys=True)}",
            f"scope: {scope}",
            f"preflight_digest: {snapshot.preflight_digest}",
            f"operation_digest: {snapshot.operation_digest}",
        ]
        if approval.pricing_evidence is not None:
            for entry in approval.pricing_evidence["entries"]:
                lines.append(
                    f"pricing: route={entry['provider']} rates=input={entry['input_usd_per_1m']} "
                    f"output={entry['output_usd_per_1m']} USD/1M source={entry['rate_source']!r} "
                    f"observed_at={entry['observed_at']} "
                    f"conservative_upper_bound_usd={entry['conservative_upper_bound_usd']} "
                    f"basis={entry['basis']}"
                )
        if snapshot.dependency_attempts:
            lines.append(
                f"dependency_attempts: {json.dumps(snapshot.dependency_attempts, sort_keys=True)}"
            )
        lines.append(
            f"approval_draft: {approval.approval_id} approved_by={approval.approved_by!r} "
            f"scope={approval.scope}"
        )
        lines.append(
            "spend_notice: approving constructs the provider and executes this ONE "
            "operation; preparing wrote nothing and constructed no provider"
        )
        return "\n".join(lines)

    def prepare_full_run(
        self, actor: str, pricing_evidence: dict[str, Any] | None = None
    ) -> PreparedOperation:
        """Zero-spend full-run preflight: frozen snapshot + approval draft.

        Constructs NO provider and writes NOTHING (no run directory, no
        preflight.json — that is written by the engine only once the run is
        approved and started). The draft run id is generated in memory and
        bound into the approval target so the engine's identity assertion
        binds exactly the run this approval may start.
        """
        self._require_open("prepare_full_run")
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError("prepare_full_run requires a non-empty actor label")
        job = self._require_job("prepare_full_run")
        validate_referenced_files(job)
        run_id = new_run_id(job.name)
        snapshot = build_preflight_snapshot(job)
        worker_user = render_worker_user_message([
            (item.label, _decode_operation_text(_read_operation_bytes(item.path), item.path))
            for item in job.inputs
        ])
        stage_specs: tuple[WorkerSpec | SynthesisSpec, ...] = job.workers
        if job.synthesis is not None:
            stage_specs += (job.synthesis,)
        pricing_stages = [
            {
                "id": spec.id,
                "provider": spec.provider,
                "model": spec.model,
                "max_output_tokens": spec.max_output_tokens,
                "compiled_prompt_text": snapshot.system_messages[spec.id] + "\n" + worker_user,
            }
            for spec in stage_specs
        ]
        pricing = build_pricing_evidence(pricing_stages, snapshot.provider_routes, pricing_evidence)
        approval = ApprovalRecord(
            approval_id=_new_approval_id(),
            approved_at=now_iso(),
            approved_by=actor.strip(),
            preflight_digest=snapshot.preflight_digest,
            scope=_FULL_RUN_SCOPE,
            target={"run_id": run_id, "stage_id": None, "attempt_number": None},
            pricing_evidence=pricing,
        )
        summary = self._full_run_summary(
            job=job, run_id=run_id, snapshot=snapshot, approval=approval
        )
        return PreparedOperation(
            operation="full_run",
            scope=_FULL_RUN_SCOPE,
            actor=actor.strip(),
            job=job,
            job_path=self._job_path,
            run_id=run_id,
            run_dir=job.output.runs_dir / run_id,
            stage_id=None,
            model=None,
            attempt_number=None,
            snapshot=snapshot,
            approval=approval,
            provider_routes={key: dict(route) for key, route in snapshot.provider_routes.items()},
            summary=summary,
        )

    def prepare_regeneration(
        self, stage_id: str, model: str, actor: str,
        pricing_evidence: dict[str, Any] | None = None,
    ) -> PreparedOperation:
        """Zero-spend worker-regeneration preflight on the current run.

        Binds the run id, the worker id, the exact next attempt number (derived
        from disk), the requested model, the frozen provider route and the
        frozen rendered worker payload. Writes nothing; constructs no provider.
        The engine re-verifies every binding against disk before dispatch, so
        a stale snapshot is refused before any provider call.
        """
        self._require_open("prepare_regeneration")
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError("prepare_regeneration requires a non-empty actor label")
        if not isinstance(model, str) or not model.strip():
            raise ValidationError("regeneration requires a non-empty requested model string")
        job = self._require_job("prepare_regeneration")
        validate_referenced_files(job)
        run_id, run_dir = self._require_current_run("prepare_regeneration")
        validate_stage_id(stage_id, "regeneration target stage id")
        stage_order = _require_v2_operation_target(run_dir, run_id)
        if stage_id not in stage_order:
            raise ValidationError(f"stage {stage_id!r} is not declared in run.json stage_order")
        spec = next((worker for worker in job.workers if worker.id == stage_id), None)
        if spec is None:
            if job.synthesis is not None and job.synthesis.id == stage_id:
                raise ValidationError(
                    f"stage {stage_id!r} is the synthesis stage; worker regeneration never "
                    "targets synthesis (use prepare_synthesis_rerun)"
                )
            raise ValidationError(
                f"stage {stage_id!r} is not a declared worker of the supplied job"
            )
        model = model.strip()
        user_message = render_worker_user_message(
            [
                (item.label, _decode_operation_text(_read_operation_bytes(item.path), item.path))
                for item in job.inputs
            ]
        )
        preflight = build_preflight_snapshot(job)
        route = declared_provider_routes(job).get(spec.provider)
        if route is None:
            raise ValidationError(f"job declares no provider route for {spec.provider!r}")
        snapshot = OperationSnapshot(
            operation="worker_regeneration",
            target_run_id=run_id,
            stage_id=stage_id,
            attempt_number=next_attempt_number(run_dir, stage_id),
            model=model,
            provider_route=dict(route),
            system_message=preflight.system_messages[stage_id],
            user_message=user_message,
            dependency_attempts={},
            dependency_digests={},
            preflight_digest=preflight.preflight_digest,
            operation_digest="",
        )
        snapshot = replace(snapshot, operation_digest=snapshot.compute_digest())
        pricing = build_pricing_evidence(
            [{"id": stage_id, "provider": spec.provider, "model": model,
              "max_output_tokens": spec.max_output_tokens,
              "compiled_prompt_text": snapshot.system_message + "\n" + snapshot.user_message}],
            declared_provider_routes(job), pricing_evidence,
        )
        scope = f"{_WORKER_REGENERATION_SCOPE_PREFIX}{stage_id}"
        approval = ApprovalRecord(
            approval_id=_new_approval_id(),
            approved_at=now_iso(),
            approved_by=actor.strip(),
            preflight_digest=snapshot.preflight_digest,
            scope=scope,
            target={
                "run_id": run_id,
                "stage_id": stage_id,
                "attempt_number": snapshot.attempt_number,
            },
            pricing_evidence=pricing,
        )
        summary = self._operation_summary(
            operation="worker_regeneration", snapshot=snapshot, approval=approval, scope=scope
        )
        return PreparedOperation(
            operation="worker_regeneration",
            scope=scope,
            actor=actor.strip(),
            job=job,
            job_path=self._job_path,
            run_id=run_id,
            run_dir=run_dir,
            stage_id=stage_id,
            model=model,
            attempt_number=snapshot.attempt_number,
            snapshot=snapshot,
            approval=approval,
            provider_routes={key: dict(value) for key, value in declared_provider_routes(job).items()},
            summary=summary,
        )

    def prepare_synthesis_rerun(
        self, actor: str, pricing_evidence: dict[str, Any] | None = None
    ) -> PreparedOperation:
        """Zero-spend synthesis-rerun preflight on the current run.

        Binds each selected dependency attempt number AND the sha256 of that
        attempt's exact output bytes, plus the exact rendered synthesis user
        message built from those bytes (F-01). Writes nothing; constructs no
        provider.
        """
        self._require_open("prepare_synthesis_rerun")
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError("prepare_synthesis_rerun requires a non-empty actor label")
        job = self._require_job("prepare_synthesis_rerun")
        validate_referenced_files(job)
        if job.synthesis is None:
            raise ValidationError(
                "the supplied job declares no synthesis stage; nothing to rerun"
            )
        run_id, run_dir = self._require_current_run("prepare_synthesis_rerun")
        synth = job.synthesis
        stage_order = _require_v2_operation_target(run_dir, run_id)
        if synth.id not in stage_order:
            raise ValidationError(
                f"synthesis stage {synth.id!r} is not declared in run.json stage_order"
            )
        selection = read_selection(run_dir)
        dependency_attempts: dict[str, int] = {}
        dependency_digests: dict[str, str] = {}
        dependency_texts: list[tuple[str, str]] = []
        for dep in synth.depends_on:
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
        route = declared_provider_routes(job).get(synth.provider)
        if route is None:
            raise ValidationError(f"job declares no provider route for {synth.provider!r}")
        snapshot = OperationSnapshot(
            operation="synthesis_rerun",
            target_run_id=run_id,
            stage_id=synth.id,
            attempt_number=next_attempt_number(run_dir, synth.id),
            model=synth.model,
            provider_route=dict(route),
            system_message=preflight.system_messages[synth.id],
            user_message=user_message,
            dependency_attempts=dependency_attempts,
            dependency_digests=dependency_digests,
            preflight_digest=preflight.preflight_digest,
            operation_digest="",
        )
        snapshot = replace(snapshot, operation_digest=snapshot.compute_digest())
        pricing = build_pricing_evidence(
            [{"id": synth.id, "provider": synth.provider, "model": synth.model,
              "max_output_tokens": synth.max_output_tokens,
              "compiled_prompt_text": snapshot.system_message + "\n" + snapshot.user_message}],
            declared_provider_routes(job), pricing_evidence,
        )
        approval = ApprovalRecord(
            approval_id=_new_approval_id(),
            approved_at=now_iso(),
            approved_by=actor.strip(),
            preflight_digest=snapshot.preflight_digest,
            scope=_SYNTHESIS_RERUN_SCOPE,
            target={
                "run_id": run_id,
                "stage_id": synth.id,
                "attempt_number": snapshot.attempt_number,
            },
            pricing_evidence=pricing,
        )
        summary = self._operation_summary(
            operation="synthesis_rerun", snapshot=snapshot, approval=approval,
            scope=_SYNTHESIS_RERUN_SCOPE,
        )
        return PreparedOperation(
            operation="synthesis_rerun",
            scope=_SYNTHESIS_RERUN_SCOPE,
            actor=actor.strip(),
            job=job,
            job_path=self._job_path,
            run_id=run_id,
            run_dir=run_dir,
            stage_id=synth.id,
            model=synth.model,
            attempt_number=snapshot.attempt_number,
            snapshot=snapshot,
            approval=approval,
            provider_routes={key: dict(value) for key, value in declared_provider_routes(job).items()},
            summary=summary,
        )

    # -- approval + hosting (the only provider construction) ------------------

    def _host(
        self,
        coro: Coroutine[Any, Any, RunResult | StageRecord],
        *,
        name: str,
        hosted: _HostedRun,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise Bots5Error(
                "approve_and_* must be called while an event loop is running; "
                "the desktop loop hosts the campaign task"
            ) from exc
        task = loop.create_task(coro, name=name)
        self._task = task
        self._hosted = hosted
        task.add_done_callback(self._observe_done)

    def _observe_done(self, task: asyncio.Task[RunResult | StageRecord]) -> None:
        """Observe the hosted task's outcome exactly once (never unobserved)."""
        if self._task is task:
            self._task = None
        try:
            self._last_result = task.result()
            self._last_error = None
            self._last_cancelled = False
        except asyncio.CancelledError:
            self._last_result = None
            self._last_error = None
            self._last_cancelled = True
        except Exception as exc:
            self._last_result = None
            self._last_error = exc
            self._last_cancelled = False

    def approve_and_start(self, prepared: PreparedOperation) -> None:
        """Consume the full-run approval and host the campaign run.

        Providers are constructed ONLY here, only after approval, through the
        injected factory (default: the real cli route rules). The engine
        performs the snapshot/approval assertions, consumes the durable
        one-shot marker, writes preflight.json and creates the run tree.
        Refusals happen engine-side before any provider request and leave
        zero run-directory bytes.
        """
        self._require_open("approve_and_start")
        if prepared.operation != "full_run":
            raise ValidationError(
                f"approve_and_start requires a full_run operation; got {prepared.operation!r}"
            )
        self._require_idle("approve_and_start")
        self._validate_prepared_pricing(prepared)
        self._consume_approval(prepared)
        providers = self._provider_factory(prepared.job)
        self._current_run_id = prepared.run_id
        self._current_run_dir = prepared.run_dir
        self._host(
            run_job(
                prepared.job,
                providers,
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            ),
            name=f"bots5-campaign-{prepared.run_id}",
            hosted=_HostedRun(
                operation="full_run",
                run_id=prepared.run_id,
                run_dir=prepared.run_dir,
                stage_id=None,
                attempt_number=None,
            ),
        )

    def approve_and_regenerate(self, prepared: PreparedOperation) -> None:
        """Consume the regeneration approval and host the sibling attempt.

        Regeneration NEVER auto-selects (engine-side §2.2): the selection is
        unchanged by this execution; switching is the explicit
        ``select_attempt`` operator command.
        """
        self._require_open("approve_and_regenerate")
        if prepared.operation != "worker_regeneration":
            raise ValidationError(
                "approve_and_regenerate requires a worker_regeneration operation; "
                f"got {prepared.operation!r}"
            )
        self._require_idle("approve_and_regenerate")
        self._validate_prepared_pricing(prepared)
        self._consume_approval(prepared)
        providers = self._provider_factory(prepared.job)
        self._current_run_id = prepared.run_id
        self._current_run_dir = prepared.run_dir
        assert prepared.stage_id is not None and prepared.model is not None
        self._host(
            regenerate_worker(
                prepared.job,
                providers,
                run_dir=prepared.run_dir,
                run_id=prepared.run_id,
                stage_id=prepared.stage_id,
                model=prepared.model,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            ),
            name=(
                f"bots5-campaign-regen-{prepared.stage_id}"
                f"-att{prepared.attempt_number}-{prepared.run_id}"
            ),
            hosted=_HostedRun(
                operation="worker_regeneration",
                run_id=prepared.run_id,
                run_dir=prepared.run_dir,
                stage_id=prepared.stage_id,
                attempt_number=prepared.attempt_number,
            ),
        )

    def approve_and_rerun_synthesis(self, prepared: PreparedOperation) -> None:
        """Consume the rerun approval and host the new synthesis attempt.

        The dispatch consumes the frozen snapshot messages verbatim, bound to
        the exact selected dependency attempts and output digests approved at
        preflight time; on success the engine selects the new attempt and
        mirrors result.md. Never triggered automatically by anything.
        """
        self._require_open("approve_and_rerun_synthesis")
        if prepared.operation != "synthesis_rerun":
            raise ValidationError(
                f"approve_and_rerun_synthesis requires a synthesis_rerun operation; "
                f"got {prepared.operation!r}"
            )
        self._require_idle("approve_and_rerun_synthesis")
        self._validate_prepared_pricing(prepared)
        self._consume_approval(prepared)
        providers = self._provider_factory(prepared.job)
        self._current_run_id = prepared.run_id
        self._current_run_dir = prepared.run_dir
        self._host(
            rerun_synthesis(
                prepared.job,
                providers,
                run_dir=prepared.run_dir,
                run_id=prepared.run_id,
                snapshot=prepared.snapshot,
                approval=prepared.approval,
            ),
            name=(
                f"bots5-campaign-rerun-{prepared.stage_id}"
                f"-att{prepared.attempt_number}-{prepared.run_id}"
            ),
            hosted=_HostedRun(
                operation="synthesis_rerun",
                run_id=prepared.run_id,
                run_dir=prepared.run_dir,
                stage_id=prepared.stage_id,
                attempt_number=prepared.attempt_number,
            ),
        )

    # -- explicit selection -----------------------------------------------------

    def select_attempt(self, stage_id: str, attempt_number: int) -> None:
        """Explicit "Make current": move the durable selection to one attempt.

        Writes ``selection.json`` through the engine's storage helper and
        appends an ``attempt_selected`` event. The target attempt must exist
        on disk (fail closed). Regeneration never auto-selects; this is the
        only selection command the desktop has, and staleness re-derives
        mechanically from the durable evidence on the next projection.
        """
        self._require_open("select_attempt")
        validate_stage_id(stage_id, "selection stage id")
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
            raise ValidationError(
                f"attempt number must be a positive integer; got {attempt_number!r}"
            )
        run_id, run_dir = self._require_current_run("select_attempt")
        stage_order = _require_v2_operation_target(run_dir, run_id)
        if stage_id not in stage_order:
            raise ValidationError(f"stage {stage_id!r} is not declared in run.json stage_order")
        meta_rel, _output_rel = attempt_paths(stage_id, attempt_number)
        meta_path = run_dir / meta_rel
        if not meta_path.is_file():
            raise ValidationError(f"attempt does not exist: {meta_path}")
        selection = read_selection(run_dir)
        previous = selection.get(stage_id, 1)
        write_selection(run_dir, run_id, {**selection, stage_id: attempt_number})
        EventWriter(run_dir / "events.jsonl", run_id).write(
            "attempt_selected",
            stage_id,
            attempt_number=attempt_number,
            previous_attempt=previous,
        )

    # -- projection (bounded, read-only) ----------------------------------------

    def _resolve_projection_dir(self, run_dir: str | Path | None) -> Path:
        if run_dir is not None:
            return Path(run_dir).resolve(strict=False)
        if self._current_run_dir is not None:
            return self._current_run_dir
        raise ValidationError(
            "projection requires a current run; call adopt_run or pass an explicit "
            "run directory"
        )

    def _hosts(self, run_dir: Path) -> bool:
        hosted = self._hosted
        return (
            hosted is not None
            and hosted.run_dir == run_dir
            and self._task is not None
            and not self._task.done()
        )

    def projection(self, run_dir: str | Path | None = None) -> CampaignProjection:
        """Bounded, read-only filesystem projection of a run directory.

        Reads a fixed small set of run-directory files (run.json, the selected
        attempt metadata, selection.json, usage.json, dependency outputs for
        the freshness digests) and is bounded by construction far below
        ``poll_max_interval_ms``; it never mutates anything. Raises the typed
        engine errors on a missing/malformed run (fail closed — never guess).
        """
        target = self._resolve_projection_dir(run_dir)
        return project_run(target, hosted=self._hosts(target))

    async def projection_async(self, run_dir: str | Path | None = None) -> CampaignProjection:
        """Same bounded projection, read OFF the event loop (never blocks it)."""
        target = self._resolve_projection_dir(run_dir)
        hosted = self._hosts(target)
        return await asyncio.to_thread(project_run, target, hosted=hosted)

    def read_stage_output(
        self,
        stage_id: str,
        attempt_number: int | None = None,
        run_dir: str | Path | None = None,
    ) -> str | None:
        """Read one attempt's durable output text through the engine reader.

        This is the desktop's inspection seam for "expandable persisted output
        per attempt" (DESKTOP_SURFACE_AND_LIFECYCLE.md section 3): the bridge
        goes through ``storage.load_stage_view`` exactly as the headless
        ``inspect`` verb does, so the desktop never parses run files itself and
        there is still exactly one reader. It is strictly read-only and returns
        ``None`` when the attempt has no output artifact. A missing or malformed
        attempt raises the typed engine error (fail closed, never guess).
        """
        target = self._resolve_projection_dir(run_dir)
        _meta, output = load_stage_view(target, stage_id, attempt_number)
        return output

    async def read_stage_output_async(
        self,
        stage_id: str,
        attempt_number: int | None = None,
        run_dir: str | Path | None = None,
    ) -> str | None:
        """Same read, performed OFF the event loop."""
        target = self._resolve_projection_dir(run_dir)
        return await asyncio.to_thread(
            self.read_stage_output, stage_id, attempt_number, target
        )

    # -- hosted task lifecycle ---------------------------------------------------

    async def run_to_completion(self) -> RunResult | StageRecord:
        """Await the hosted campaign task and return its engine result."""
        task = self._task
        if task is not None:
            return await task
        if self._last_error is not None:
            raise self._last_error
        if self._last_cancelled:
            raise Bots5Error(
                "the hosted campaign task was cancelled; the durable record is terminal"
            )
        if self._last_result is not None:
            return self._last_result
        raise ValidationError("no hosted campaign task to await")

    def _verify_durable_terminal(self, hosted: _HostedRun) -> None:
        """D-7 follow-through: cancellation must leave a durable terminal record.

        Verified from disk, never fabricated: if the terminal write did not
        land, the typed StorageError forces the caller to keep displaying
        interrupted/uncertain instead of claiming a clean cancellation.
        """
        if hosted.operation == "full_run":
            if not hosted.run_dir.is_dir():
                # Cancelled before the run tree existed: nothing was created,
                # nothing was spent.
                return
            run = read_json(hosted.run_dir / "run.json")
            if run.get("state") == "running":
                raise StorageError(
                    "cancellation terminalization did not land; the run stays "
                    "interrupted/uncertain (never displayed as a clean outcome)"
                )
            return
        # Regeneration / rerun: the parent run.json is untouched; the hosted
        # attempt must not be durably running.
        if hosted.stage_id is None or hosted.attempt_number is None:
            return
        meta_rel, _output_rel = attempt_paths(hosted.stage_id, hosted.attempt_number)
        meta_path = hosted.run_dir / meta_rel
        if not meta_path.is_file():
            return  # never claimed: nothing was dispatched
        meta = read_json(meta_path)
        if meta.get("state") == "running":
            raise StorageError(
                "cancellation terminalization did not land for the hosted attempt; "
                "it stays interrupted/uncertain"
            )

    async def cancel(self) -> None:
        """Request cancellation of the hosted task and await the terminal record.

        The engine terminalizes durably BEFORE the cancellation propagates
        (run-level CANCELLED, stage-level cause exactly "cancelled"); this
        method awaits that task and then verifies the terminal record from
        disk. Idempotent when nothing is hosted. Never retries provider work.
        """
        hosted = self._hosted
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if hosted is not None:
            self._verify_durable_terminal(hosted)

    async def close(self) -> None:
        """Drain: never leave an unobserved task, never silently retry work.

        Awaits the hosted task to its natural end (whatever the durable
        outcome is), observes its outcome, and clears the hosting state.
        Explicit cancellation remains the separate ``cancel()`` operator act.
        Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is None:
            return
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # Observed during drain and retained; close never retries
                # provider work and never fabricates a successful outcome.
                if self._last_error is None:
                    self._last_error = exc
        else:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # already observed by the done callback
        if self._task is task:
            self._task = None
