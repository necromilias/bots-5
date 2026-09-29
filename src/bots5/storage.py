from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ApprovalInvalidatedError, StorageError, ValidationError
from .manifest import job_to_dict
from .models import Job, RunState, StageRecord, StageState
from .paths import validate_run_id, validate_stage_id
from .usage import usage_document, usage_document_v2


@dataclass(frozen=True)
class RunDirs:
    root: Path
    stages: Path
    events: Path


# --- Phase 10 evidence v2 (CAMPAIGN_EVIDENCE_EVOLUTION.md) -------------------
# Version marker: run.json "evidence_version"; absence means version 1.
EVIDENCE_VERSION_V2 = 2
# selection.json schema (CAMPAIGN_EVIDENCE_EVOLUTION.md §2.3).
SELECTION_SCHEMA_VERSION = 1
# Flat attempt grammar: stages/<stage_id>.att<N>.json / .md for N >= 1. The
# greedy base keeps stage ids that themselves contain ".att<digits>" distinct
# (declared-id resolution never infers stage roles from filename shapes).
_ATTEMPT_FILE_RE = re.compile(r"^(?P<base>.+)\.att(?P<num>[0-9]+)\.(?:json|md)$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# Synthesis freshness outcomes (REGENERATION_AND_STALE_SYNTHESIS.md §4.2 with
# the O-1 ordering rule: NOT_APPLICABLE is decided before any provenance
# evaluation; LEGACY_UNVERIFIED applies only to v1 evidence).
SYNTHESIS_FRESHNESS_FRESH = "FRESH"
SYNTHESIS_FRESHNESS_STALE = "STALE"
SYNTHESIS_FRESHNESS_UNVERIFIABLE = "UNVERIFIABLE"
SYNTHESIS_FRESHNESS_NOT_APPLICABLE = "NOT_APPLICABLE"
SYNTHESIS_FRESHNESS_LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
SYNTHESIS_FRESHNESS_OUTCOMES = (
    SYNTHESIS_FRESHNESS_FRESH,
    SYNTHESIS_FRESHNESS_STALE,
    SYNTHESIS_FRESHNESS_UNVERIFIABLE,
    SYNTHESIS_FRESHNESS_NOT_APPLICABLE,
    SYNTHESIS_FRESHNESS_LEGACY_UNVERIFIED,
)


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
    _atomic_write(path, _dumps_json(data, path))


def _dumps_json(data: Any, path: Path) -> str:
    try:
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise StorageError(f"cannot serialize JSON artifact {path}: {exc}") from None


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


def _validate_attempt_number(attempt_number: int) -> None:
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise ValidationError(
            f"attempt number must be a positive integer; got {attempt_number!r}"
        )


def attempt_paths(stage_id: str, attempt_number: int) -> tuple[Path, Path]:
    """Pure helper: relative (metadata, output) paths of one v2 attempt.

    v2 attempts live flat inside ``stages/`` as ``<stage_id>.att<N>.json`` and
    ``<stage_id>.att<N>.md`` (N >= 1) so the output containment predicate in
    :func:`load_stage_view` is untouched. The v1 legacy single-attempt files
    ``<stage_id>.json`` / ``<stage_id>.md`` are never written by v2 code paths.
    """
    validate_stage_id(stage_id)
    _validate_attempt_number(attempt_number)
    meta_rel = Path("stages") / f"{stage_id}.att{attempt_number}.json"
    output_rel = Path("stages") / f"{stage_id}.att{attempt_number}.md"
    return meta_rel, output_rel


def _exclusive_write_json(path: Path, payload: str) -> None:
    """Create ``path`` exclusively (O_CREAT|O_EXCL) and fsync it.

    Used where the first writer must win durably (new attempt metadata, one-shot
    approval markers). A temp-file + ``os.replace`` strategy is NOT exclusive:
    the final name can be claimed twice. The write happens directly on the
    exclusively opened descriptor, so a losing writer never creates the file.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(
            f"cannot create directory: {path.parent}: {exc.strerror or exc}"
        ) from None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise StorageError(f"refusing to overwrite existing artifact: {path}") from None
    except OSError as exc:
        raise StorageError(f"cannot create artifact: {path}: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageError(f"cannot write artifact: {path}: {exc.strerror or exc}") from None
    _fsync_dir(path.parent)


def persist_stage_attempt(
    dirs: RunDirs,
    stage: StageRecord,
    *,
    attempt_number: int,
    text: str | None = None,
    create: bool = False,
) -> None:
    """Persist one evidence-v2 attempt of ``stage``.

    ``create=True`` claims the attempt namespace with exclusive-create semantics
    and refuses to overwrite an existing attempt (StorageError); nothing is
    written when the claim fails. ``create=False`` is the ordinary atomic
    in-place update of the SAME attempt's metadata (the queued → running →
    terminal transition) and requires the attempt file to already exist.
    """
    validate_stage_id(stage.id)
    _validate_attempt_number(attempt_number)
    meta_rel, output_rel = attempt_paths(stage.id, attempt_number)
    meta_path = dirs.stages / meta_rel.name
    stage.attempt_number = attempt_number
    if text is not None:
        stage.output_path = str(output_rel)
    payload = _dumps_json(stage.to_dict(), meta_path)
    if create:
        # Claim the namespace first: an overwrite refusal must leave the run
        # directory byte-identical, so the metadata claim precedes any output.
        _exclusive_write_json(meta_path, payload)
        if text is not None:
            atomic_write_text(dirs.root / output_rel, text)
    else:
        # The existence precondition precedes every write: a create=False call
        # on a missing attempt is caller error and must leave the run
        # directory byte-identical, not strand an output artifact with no
        # metadata advertising it.
        if not meta_path.is_file():
            raise StorageError(f"attempt file does not exist: {meta_path}")
        if text is not None:
            # The metadata write is what advertises the terminal state and its
            # output_path, so the output is written FIRST (D-12 / V-6): a
            # failure between the two writes must leave the metadata
            # un-advanced (still queued/running) rather than durably claiming
            # a succeeded stage whose artifact cannot be read.
            atomic_write_text(dirs.root / output_rel, text)
        _atomic_write(meta_path, payload)


def persist_usage(dirs: RunDirs, stages: list[StageRecord] | tuple[StageRecord, ...]) -> dict[str, Any]:
    doc = usage_document(stages)
    atomic_write_json(dirs.root / "usage.json", doc)
    return doc


def persist_usage_v2(
    dirs: RunDirs,
    attempt_records: list[StageRecord] | tuple[StageRecord, ...],
    *,
    selected_attempts: dict[str, int],
    stage_ids: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Write the evidence-v2 usage document (dual accounting) atomically.

    Writes :func:`usage.usage_document_v2` (cumulative over every executed
    attempt + the F-07 derived selected summary) to ``usage.json`` via
    ``atomic_write_json`` and returns the document. The stored selected
    summary is a cache: readers re-derive it from ``per_attempt`` +
    ``selection.json`` and treat the derived value as the authority.
    ``persist_usage`` above keeps its exact legacy signature and behaviour.
    """
    doc = usage_document_v2(
        attempt_records,
        selected_attempts=selected_attempts,
        stage_ids=stage_ids,
        run_id=dirs.root.name,
    )
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


def persist_run_v2(
    dirs: RunDirs,
    *,
    run_id: str,
    state: RunState,
    started_at: str,
    ended_at: str | None,
    stages: list[StageRecord] | tuple[StageRecord, ...],
    run_timeout_seconds: float,
    synthesis_skipped_reason: str | None = None,
    evidence_version: int = EVIDENCE_VERSION_V2,
) -> None:
    """Evidence-v2 run.json writer (Phase 10 M0.3a).

    Writes EXACTLY the :func:`persist_run` document shape plus the single
    ``evidence_version`` version marker (CAMPAIGN_EVIDENCE_EVOLUTION.md §1:
    ``run.json`` gains exactly one key; absence of the key means version 1 and
    every pre-existing key keeps its meaning and shape). :func:`persist_run`
    itself is untouched so legacy v1 writers stay byte-identical.
    """
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
            "evidence_version": evidence_version,
        },
    )


def persist_result(dirs: RunDirs, text: str) -> None:
    atomic_write_text(dirs.root / "result.md", text)


# --- selection.json (authoritative stage_id -> selected attempt) -------------


def read_selection(run_dir: Path) -> dict[str, int]:
    """Read selection.json; absent file -> {} (every stage defaults to attempt 1)."""
    path = run_dir / "selection.json"
    if not path.is_file():
        return {}
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise ValidationError(f"selection document must be a JSON object: {path}")
    schema_version = doc.get("schema_version")
    if schema_version != SELECTION_SCHEMA_VERSION:
        raise ValidationError(
            f"unsupported selection schema_version: {schema_version!r} in {path}"
        )
    raw = doc.get("selected_attempts", {})
    if not isinstance(raw, dict):
        raise ValidationError(f"selected_attempts must be a JSON object: {path}")
    selected: dict[str, int] = {}
    for stage_id, attempt in raw.items():
        if not isinstance(stage_id, str):
            raise ValidationError(f"selection keys must be stage id strings: {path}")
        validate_stage_id(stage_id, context="selection stage id")
        _validate_attempt_number(attempt)
        selected[stage_id] = attempt
    return selected


def write_selection(run_dir: Path, run_id: str, selected: dict[str, int]) -> None:
    """Atomically write selection.json (schema_version, run_id, updated_at, selected_attempts)."""
    validate_run_id(run_id)
    clean: dict[str, int] = {}
    for stage_id, attempt in selected.items():
        validate_stage_id(stage_id, context="selection stage id")
        _validate_attempt_number(attempt)
        clean[stage_id] = attempt
    atomic_write_json(
        run_dir / "selection.json",
        {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "run_id": run_id,
            "updated_at": now_iso(),
            "selected_attempts": clean,
        },
    )


def selected_attempt(run_dir: Path, stage_id: str) -> int:
    """The authoritative selected attempt for ``stage_id`` (1 when unlisted)."""
    validate_stage_id(stage_id)
    return read_selection(run_dir).get(stage_id, 1)


# --- Phase 10 M0.3b: attempt-record reload + next-attempt derivation ---------
# Additive helpers for the explicit regeneration / rerun engine entry points
# (REGENERATION_AND_STALE_SYNTHESIS.md §2/§5). They are disk-only readers; the
# legacy v1 writer/reader behaviour above is untouched.


def _stage_record_from_dict(data: dict[str, Any]) -> StageRecord:
    """Rebuild a :class:`StageRecord` from its own ``to_dict()`` serialization.

    The exact inverse of ``StageRecord.to_dict`` (models.py): every field is
    type-checked and anything malformed is a hard ValidationError — corrupt
    evidence is never silently coerced into a record that would be rewritten
    back to disk with different bytes.
    """
    if not isinstance(data, dict):
        raise ValidationError("stage record document must be a JSON object")
    for key in ("stage_id", "provider", "requested_model", "state"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValidationError(f"stage record field {key!r} must be a non-empty string")
    validate_stage_id(data["stage_id"], "stage record stage id")
    try:
        state = StageState(data["state"])
    except ValueError:
        raise ValidationError(f"stage record has unknown state: {data['state']!r}") from None

    def optional_string(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(f"stage record field {key!r} must be a string or null")
        return value

    usage = data.get("usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise ValidationError("stage record usage must be a JSON object or null")

    def token(key: str) -> int | None:
        value = usage.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"stage record usage.{key} must be an integer or null")
        return value

    cost_known = data.get("cost_known", False)
    if not isinstance(cost_known, bool):
        raise ValidationError("stage record cost_known must be a boolean")
    cost_value = data.get("cost_usd")
    known_cost: Decimal | None = None
    if cost_known:
        if not isinstance(cost_value, str):
            raise ValidationError(
                "stage record cost_usd must be a decimal string when cost_known is true"
            )
        try:
            known_cost = Decimal(cost_value)
        except InvalidOperation:
            raise ValidationError(
                f"stage record cost_usd is not a decimal number: {cost_value!r}"
            ) from None
    elif cost_value is not None:
        raise ValidationError("stage record cost_usd must be null when cost_known is false")

    completion = data.get("completion")
    finish_reason: str | None = None
    completion_complete: bool | None = None
    if completion is not None:
        if not isinstance(completion, dict):
            raise ValidationError("stage record completion must be a JSON object or null")
        finish_reason = completion.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ValidationError(
                "stage record completion.finish_reason must be a string or null"
            )
        completion_complete = completion.get("complete")
        if completion_complete is not None and not isinstance(completion_complete, bool):
            raise ValidationError(
                "stage record completion.complete must be a boolean or null"
            )

    failure = data.get("failure")
    error_type: str | None = None
    error_message: str | None = None
    provider_side_outcome_unknown = False
    if failure is not None:
        if not isinstance(failure, dict):
            raise ValidationError("stage record failure must be a JSON object or null")
        error_type = failure.get("type")
        if not isinstance(error_type, str) or not error_type:
            raise ValidationError("stage record failure.type must be a non-empty string")
        error_message = failure.get("message")
        if error_message is not None and not isinstance(error_message, str):
            raise ValidationError("stage record failure.message must be a string or null")
        provider_side_outcome_unknown = failure.get("provider_side_outcome_unknown", False)
        if not isinstance(provider_side_outcome_unknown, bool):
            raise ValidationError(
                "stage record failure.provider_side_outcome_unknown must be a boolean"
            )

    output_path = optional_string("output_path")
    attempt_number = data.get("attempt_number", 1)
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
        raise ValidationError(
            f"stage record attempt_number must be a positive integer; got {attempt_number!r}"
        )
    consumed = data.get("consumed_dependencies")
    digests = data.get("dependency_digests")
    if consumed is not None and not isinstance(consumed, dict):
        raise ValidationError("stage record consumed_dependencies must be a JSON object or null")
    if digests is not None and not isinstance(digests, dict):
        raise ValidationError("stage record dependency_digests must be a JSON object or null")
    if consumed or digests:
        malformed = _malformed_provenance_reason(consumed or {}, digests or {})
        if malformed is not None:
            raise ValidationError(f"stage record provenance is malformed: {malformed}")
    preflight_digest = optional_string("preflight_digest")
    duration_seconds = data.get("duration_seconds")
    if duration_seconds is not None and (
        isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
    ):
        raise ValidationError("stage record duration_seconds must be a number or null")
    request_id = optional_string("provider_request_id")

    return StageRecord(
        id=data["stage_id"],
        provider=data["provider"],
        requested_model=data["requested_model"],
        state=state,
        returned_model=optional_string("returned_model"),
        started_at=optional_string("started_at"),
        ended_at=optional_string("ended_at"),
        duration_seconds=(
            float(duration_seconds) if duration_seconds is not None else None
        ),
        prompt_tokens=token("prompt_tokens"),
        completion_tokens=token("completion_tokens"),
        reasoning_tokens=token("reasoning_tokens"),
        total_tokens=token("total_tokens"),
        known_cost_usd=known_cost,
        request_id=request_id,
        output_path=output_path,
        finish_reason=finish_reason,
        completion_complete=completion_complete,
        error_type=error_type,
        error_message=error_message,
        provider_side_outcome_unknown=provider_side_outcome_unknown,
        attempt_number=attempt_number,
        consumed_dependencies=dict(consumed) if consumed else None,
        dependency_digests=dict(digests) if digests else None,
        preflight_digest=preflight_digest,
    )


def load_attempt_records(
    run_dir: Path, stage_order: list[str] | tuple[str, ...] | None = None
) -> list[StageRecord]:
    """Load every evidence-v2 attempt record from disk (M0.3b helper).

    Reads each ``stages/<stage_id>.att<N>.json`` metadata file and rebuilds its
    :class:`StageRecord` (fail-closed on malformed content or an
    ``attempt_number`` that disagrees with the file name). Records are ordered
    by declared ``stage_order`` position, then attempt number, so re-persisted
    usage documents are deterministic. When ``stage_order`` is supplied,
    attempt metadata for an undeclared stage id fails closed (corrupt layout).
    """
    stages_dir = run_dir / "stages"
    found: dict[str, list[tuple[int, Path]]] = {}
    if stages_dir.is_dir():
        for entry in sorted(stages_dir.iterdir()):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            match = _ATTEMPT_FILE_RE.fullmatch(entry.name)
            if match is None:
                continue
            found.setdefault(match.group("base"), []).append((int(match.group("num")), entry))
    if stage_order is not None:
        declared = list(stage_order)
        for stage_id in declared:
            validate_stage_id(stage_id, "run stage_order stage id")
        undeclared = sorted(set(found) - set(declared))
        if undeclared:
            raise ValidationError(
                "attempt metadata exists for undeclared stage id(s): " + ", ".join(undeclared)
            )
        order = declared
    else:
        order = sorted(found)
    records: list[StageRecord] = []
    for stage_id in order:
        for number, path in sorted(found.get(stage_id, []), key=lambda item: item[0]):
            record = _stage_record_from_dict(read_json(path))
            if record.attempt_number != number:
                raise ValidationError(
                    f"attempt metadata attempt_number {record.attempt_number} does not "
                    f"match its file name: {path.name}"
                )
            records.append(record)
    return records


def next_attempt_number(run_dir: Path, stage_id: str) -> int:
    """The next evidence-v2 attempt number for ``stage_id`` (max existing + 1).

    Attempt numbers are per-stage identity, never positions: existing numbers
    are never renumbered or reused, and a stage with no attempts yet derives 1.
    """
    validate_stage_id(stage_id)
    stages_dir = run_dir / "stages"
    existing: set[int] = set()
    if stages_dir.is_dir():
        for entry in stages_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            match = _ATTEMPT_FILE_RE.fullmatch(entry.name)
            if match is not None and match.group("base") == stage_id:
                existing.add(int(match.group("num")))
    return max(existing, default=0) + 1


# --- preflight.json + one-shot approval consumption markers ------------------


def persist_preflight(run_dir: Path, document: dict[str, Any]) -> None:
    if not isinstance(document, dict):
        raise ValidationError("preflight document must be a JSON object")
    atomic_write_json(run_dir / "preflight.json", document)


def load_preflight(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "preflight.json"
    doc = read_json(path)  # absent -> ValidationError
    if not isinstance(doc, dict):
        raise ValidationError(f"preflight document must be a JSON object: {path}")
    return doc


def _approval_marker_path(run_dir: Path, approval_id: str) -> Path:
    validate_stage_id(approval_id, context="approval id")
    return run_dir / "approvals" / f"{approval_id}.json"


def consume_approval(run_dir: Path, approval_id: str, record: dict[str, Any]) -> None:
    """Consume a one-shot approval by exclusively creating its durable marker.

    The marker is written directly through an O_CREAT|O_EXCL descriptor and
    fsynced; a temp-file + os.replace strategy would not be exclusive. If the
    marker already exists the approval is spent and ApprovalInvalidatedError is
    raised without writing anything.
    """
    path = _approval_marker_path(run_dir, approval_id)
    if not isinstance(record, dict):
        raise ValidationError("approval record must be a JSON object")
    payload = _dumps_json(record, path)  # serialize BEFORE claiming the marker
    try:
        # exist_ok=True keeps concurrent consumers race-safe; exclusivity is
        # enforced on the marker file itself, not the directory.
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(
            f"cannot create approvals directory: {path.parent}: {exc.strerror or exc}"
        ) from None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ApprovalInvalidatedError(f"approval already consumed: {approval_id}") from None
    except OSError as exc:
        raise StorageError(f"cannot create approval marker: {path}: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageError(f"cannot write approval marker: {path}: {exc.strerror or exc}") from None
    _fsync_dir(path.parent)


def approval_consumed(run_dir: Path, approval_id: str) -> bool:
    return _approval_marker_path(run_dir, approval_id).is_file()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValidationError(f"artifact not found: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read artifact {path}: {exc}") from None


def _evidence_version(run: dict[str, Any]) -> int:
    """The evidence version declared by a run.json document (absent -> 1)."""
    value = run.get("evidence_version", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"run evidence_version must be an integer; got {value!r}")
    if value < 1:
        raise ValidationError(f"run evidence_version must be >= 1; got {value!r}")
    return value


def _require_supported_version(version: int) -> None:
    if version > EVIDENCE_VERSION_V2:
        raise ValidationError(
            f"unsupported evidence_version {version}; this reader supports up to "
            f"{EVIDENCE_VERSION_V2}"
        )


def _run_dir_evidence_version(run_dir: Path) -> int:
    """Version detection for load_stage_view.

    A missing run.json keeps the exact legacy load_stage_view behaviour (read
    stages/<id>.json directly); a present run.json declares the version.
    """
    run_path = run_dir / "run.json"
    if not run_path.is_file():
        return 1
    return _evidence_version(read_json(run_path))


def _reject_malformed_v2_layout(run_dir: Path, selection: dict[str, int]) -> None:
    """Fail closed: v2 without selection.json but with .att2+ files is malformed.

    Resolution is driven by declared ids + explicit selection only; a directory
    holding sibling attempts without an authoritative selection would force
    filename-shape guesswork, which the design forbids. Temp files (".name.*.tmp")
    never match the attempt grammar.
    """
    if selection:
        return
    stages_dir = run_dir / "stages"
    if not stages_dir.is_dir():
        return
    stray = []
    for entry in sorted(stages_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _ATTEMPT_FILE_RE.fullmatch(entry.name)
        if match is not None and int(match.group("num")) >= 2:
            stray.append(entry.name)
    if stray:
        raise ValidationError(
            "malformed evidence-v2 run directory: selection.json is absent but "
            "sibling attempt files exist: " + ", ".join(stray)
        )


def _read_stage_output(run_dir: Path, meta: dict[str, Any]) -> str | None:
    """Read a stage's output text; containment predicate EXACTLY as before v2."""
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
    return output


def load_run_view(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    run = read_json(run_dir / "run.json")
    usage = read_json(run_dir / "usage.json")
    order = run.get("stage_order", [])
    version = _evidence_version(run)
    _require_supported_version(version)
    stages: list[dict[str, Any]] = []
    if version >= EVIDENCE_VERSION_V2:
        selection = read_selection(run_dir)
        _reject_malformed_v2_layout(run_dir, selection)
        for stage_id in order:
            validate_stage_id(stage_id)
            meta_rel, _output_rel = attempt_paths(stage_id, selection.get(stage_id, 1))
            stages.append(read_json(run_dir / meta_rel))
    else:
        # Version 1: byte-identical behaviour to the pre-v2 reader.
        for stage_id in order:
            validate_stage_id(stage_id)
            stages.append(read_json(run_dir / "stages" / f"{stage_id}.json"))
    return run, stages, usage


def load_stage_view(
    run_dir: Path,
    stage_id: str,
    attempt_number: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    validate_stage_id(stage_id)
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    version = _run_dir_evidence_version(run_dir)
    _require_supported_version(version)
    selection: dict[str, int] = {}
    if version >= EVIDENCE_VERSION_V2:
        selection = read_selection(run_dir)
        _reject_malformed_v2_layout(run_dir, selection)
    if attempt_number is not None:
        # An explicit attempt is read exactly; there is never a fallback to
        # another attempt or to a "latest" scan.
        _validate_attempt_number(attempt_number)
        meta_rel, _output_rel = attempt_paths(stage_id, attempt_number)
        meta = read_json(run_dir / meta_rel)
    elif version >= EVIDENCE_VERSION_V2:
        meta_rel, _output_rel = attempt_paths(stage_id, selection.get(stage_id, 1))
        meta = read_json(run_dir / meta_rel)
    else:
        # Version 1 (or no run.json): byte-identical behaviour.
        meta = read_json(run_dir / "stages" / f"{stage_id}.json")
    output = _read_stage_output(run_dir, meta)
    return meta, output


# --- selection + staleness reconstruction (pure, disk-only) ------------------


def _synthesis_declaration(run_dir: Path) -> tuple[str | None, tuple[str, ...]]:
    """Synthesis stage id + declared dependencies from job.resolved.json (disk only)."""
    job_path = run_dir / "job.resolved.json"
    if not job_path.is_file():
        return None, ()
    job = read_json(job_path)
    if not isinstance(job, dict):
        return None, ()
    synthesis = job.get("synthesis")
    if not isinstance(synthesis, dict):
        return None, ()
    stage_id = synthesis.get("id")
    depends_on = synthesis.get("depends_on", [])
    if not isinstance(stage_id, str) or not isinstance(depends_on, list):
        return None, ()
    validate_stage_id(stage_id, context="synthesis stage id")
    return stage_id, tuple(dep for dep in depends_on if isinstance(dep, str))


def _available_attempts(run_dir: Path, stage_id: str, version: int) -> list[int]:
    """Attempt numbers present on disk for a declared stage (sorted).

    v1 has at most its single legacy attempt (``stages/<id>.json``); v2 attempt
    existence is defined by the attempt metadata file ``<id>.att<N>.json``.
    """
    stages_dir = run_dir / "stages"
    if version < EVIDENCE_VERSION_V2:
        return [1] if (stages_dir / f"{stage_id}.json").is_file() else []
    attempts: set[int] = set()
    if stages_dir.is_dir():
        for entry in stages_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            match = _ATTEMPT_FILE_RE.fullmatch(entry.name)
            if match is not None and match.group("base") == stage_id:
                num = int(match.group("num"))
                if num >= 1:
                    attempts.add(num)
    return sorted(attempts)


def _malformed_provenance_reason(
    consumed_dependencies: dict[str, Any],
    dependency_digests: dict[str, Any],
) -> str | None:
    """Malformed provenance = non-integer attempt numbers or non-hex/wrong-length digests."""
    for dep, attempt in consumed_dependencies.items():
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            return f"malformed consumed_dependencies entry for {dep!r}: {attempt!r}"
    for dep, digest in dependency_digests.items():
        if not isinstance(digest, str) or not _SHA256_HEX_RE.fullmatch(digest):
            return f"malformed dependency_digests entry for {dep!r}: {digest!r}"
    return None


def _synthesis_freshness(
    run_dir: Path,
    run: dict[str, Any],
    version: int,
    selection: dict[str, int],
) -> dict[str, Any] | None:
    """Classify the selected synthesis attempt (O-1 order; three-outcome predicate)."""
    stage_id, depends_on = _synthesis_declaration(run_dir)
    if stage_id is None:
        return None
    if version >= EVIDENCE_VERSION_V2:
        selected = selection.get(stage_id, 1)
        meta_rel, _output_rel = attempt_paths(stage_id, selected)
        meta = read_json(run_dir / meta_rel)  # missing selected attempt -> hard error
    else:
        selected = 1
        meta = read_json(run_dir / "stages" / f"{stage_id}.json")

    report: dict[str, Any] = {
        "stage_id": stage_id,
        "selected_attempt": selected,
        "classification": None,
        "skip_reason": None,
        "integrity_warning": False,
        "warnings": [],
        "dependencies": {},
    }

    # Rule 1 (O-1): a synthesis that was never dispatched is NOT_APPLICABLE
    # before any provenance evaluation; its missing provenance is not a defect
    # and raises no integrity warning. "Never dispatched" is the literal
    # ``skipped`` state *plus* any evidence-v2 attempt that explicitly records
    # ``started_at: null``: a run cancelled before synthesis was reached
    # persists a failed attempt that no provider ever saw, and reporting that
    # as an evidence-integrity problem -- or asserting it was "dispatched" --
    # would be false (REGENERATION_AND_STALE_SYNTHESIS.md: never-dispatched
    # synthesis is NOT_APPLICABLE with no integrity warning). The version guard
    # keeps version-1 evidence on the LEGACY_UNVERIFIED path below.
    never_dispatched = meta.get("state") == "skipped" or (
        version >= EVIDENCE_VERSION_V2
        and "started_at" in meta
        and meta.get("started_at") is None
    )
    if never_dispatched:
        report["classification"] = SYNTHESIS_FRESHNESS_NOT_APPLICABLE
        failure = meta.get("failure")
        if isinstance(failure, dict) and isinstance(failure.get("type"), str):
            report["skip_reason"] = failure["type"]
        elif isinstance(run.get("synthesis_skipped_reason"), str):
            report["skip_reason"] = run["synthesis_skipped_reason"]
        return report

    # Rule 2: version-1 evidence predates provenance; it is never "stale from
    # absent data", it is LEGACY_UNVERIFIED.
    if version < EVIDENCE_VERSION_V2:
        report["classification"] = SYNTHESIS_FRESHNESS_LEGACY_UNVERIFIED
        return report

    # Rule 3: a dispatched v2 attempt must carry provenance; absence or a
    # malformed map is an evidence-integrity defect, never a fabricated
    # "current" and never silently "stale".
    consumed_dependencies = meta.get("consumed_dependencies")
    dependency_digests = meta.get("dependency_digests")
    if not isinstance(consumed_dependencies, dict) or not isinstance(dependency_digests, dict):
        report["classification"] = SYNTHESIS_FRESHNESS_UNVERIFIABLE
        report["integrity_warning"] = True
        report["warnings"].append(
            "dispatched synthesis attempt has absent provenance "
            "(consumed_dependencies/dependency_digests)"
        )
        return report
    missing_consumed = [dep for dep in depends_on if dep not in consumed_dependencies]
    missing_digests = [dep for dep in depends_on if dep not in dependency_digests]
    if missing_consumed or missing_digests:
        report["classification"] = SYNTHESIS_FRESHNESS_UNVERIFIABLE
        report["integrity_warning"] = True
        missing_parts = []
        if missing_consumed:
            missing_parts.append("consumed_dependencies: " + ", ".join(missing_consumed))
        if missing_digests:
            missing_parts.append("dependency_digests: " + ", ".join(missing_digests))
        report["warnings"].append(
            "dispatched synthesis provenance is missing declared dependency binding(s) ("
            + "; ".join(missing_parts)
            + ")"
        )
        return report
    malformed = _malformed_provenance_reason(consumed_dependencies, dependency_digests)
    if malformed is not None:
        report["classification"] = SYNTHESIS_FRESHNESS_UNVERIFIABLE
        report["integrity_warning"] = True
        report["warnings"].append(malformed)
        return report

    # Rule 4: valid provenance -> mechanical predicate against the CURRENT
    # selection and the CURRENT output bytes of each consumed dependency.
    all_fresh = True
    for dep in depends_on:
        dep_selected = selection.get(dep, 1)
        recorded_attempt = consumed_dependencies.get(dep)
        recorded_digest = dependency_digests.get(dep)
        dep_info: dict[str, Any] = {
            "selected_attempt": dep_selected,
            "recorded_attempt": recorded_attempt,
            "output_present": None,
            "digest_match": None,
        }
        fresh = False
        if dep_selected != recorded_attempt:
            # Selection moved on: mechanically stale, no integrity anomaly.
            pass
        else:
            output_path = run_dir / "stages" / f"{dep}.att{dep_selected}.md"
            try:
                data = output_path.read_bytes()
            except OSError:
                dep_info["output_present"] = False
                report["integrity_warning"] = True
                report["warnings"].append(
                    f"selected output bytes missing/unreadable for dependency "
                    f"{dep!r} attempt {dep_selected}"
                )
            else:
                dep_info["output_present"] = True
                actual = hashlib.sha256(data).hexdigest()
                dep_info["digest_match"] = actual == recorded_digest
                if dep_info["digest_match"]:
                    fresh = True
                else:
                    report["integrity_warning"] = True
                    report["warnings"].append(
                        f"dependency digest mismatch for {dep!r} attempt {dep_selected}"
                    )
        dep_info["matches"] = fresh
        report["dependencies"][dep] = dep_info
        if not fresh:
            all_fresh = False
    report["classification"] = (
        SYNTHESIS_FRESHNESS_FRESH if all_fresh else SYNTHESIS_FRESHNESS_STALE
    )
    return report


def reconstruct_run_state(run_dir: Path) -> dict[str, Any]:
    """Reconstruct per-stage selection and synthesis freshness from disk only.

    Returns ``{"run_id", "evidence_version", "stages": {stage_id: {
    "selected_attempt", "available_attempts", ["synthesis_freshness"]}}}``.
    The selected attempt is authoritative (selection.json, default 1); a missing
    selected-attempt file is a hard ValidationError; a v2 directory without
    selection.json that contains .att2+ files fails closed.
    """
    if not run_dir.is_dir():
        raise ValidationError(f"run not found: {run_dir.name}")
    run = read_json(run_dir / "run.json")
    version = _evidence_version(run)
    _require_supported_version(version)
    order = run.get("stage_order", [])
    for stage_id in order:
        validate_stage_id(stage_id)
    selection = read_selection(run_dir) if version >= EVIDENCE_VERSION_V2 else {}
    if version >= EVIDENCE_VERSION_V2:
        _reject_malformed_v2_layout(run_dir, selection)

    stages_state: dict[str, Any] = {}
    for stage_id in order:
        stages_state[stage_id] = {
            "selected_attempt": selection.get(stage_id, 1)
            if version >= EVIDENCE_VERSION_V2
            else 1,
            "available_attempts": _available_attempts(run_dir, stage_id, version),
        }

    synthesis = _synthesis_freshness(run_dir, run, version, selection)
    if synthesis is not None:
        entry = stages_state.setdefault(
            synthesis["stage_id"],
            {
                "selected_attempt": synthesis["selected_attempt"],
                "available_attempts": _available_attempts(
                    run_dir, synthesis["stage_id"], version
                ),
            },
        )
        entry["synthesis_freshness"] = synthesis

    return {
        "run_id": run.get("run_id"),
        "evidence_version": version,
        "stages": stages_state,
    }
