from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .models import CostSummary, StageRecord


def aggregate_cost(stages: list[StageRecord] | tuple[StageRecord, ...]) -> CostSummary:
    known = [stage.known_cost_usd for stage in stages if stage.known_cost_usd is not None]
    unknown = tuple(stage.id for stage in stages if stage.known_cost_usd is None)
    known_sum = sum(known, Decimal("0"))
    if not stages:
        status = "known"
        complete = True
    elif len(unknown) == len(stages):
        status = "unknown"
        complete = False
    elif unknown:
        status = "partial"
        complete = False
    elif known_sum == 0:
        status = "zero"
        complete = True
    else:
        status = "known"
        complete = True
    return CostSummary(
        known_sum_usd=known_sum,
        status=status,
        unknown_stage_ids=unknown,
        complete=complete,
    )


def usage_document(stages: list[StageRecord] | tuple[StageRecord, ...]) -> dict[str, Any]:
    per_stage: dict[str, Any] = {}
    prompt_known_sum = 0
    completion_known_sum = 0
    reasoning_known_sum = 0
    total_known_sum = 0
    for stage in stages:
        per_stage[stage.id] = {
            "prompt_tokens": stage.prompt_tokens,
            "completion_tokens": stage.completion_tokens,
            "reasoning_tokens": stage.reasoning_tokens,
            "total_tokens": stage.total_tokens,
            "cost_usd": None if stage.known_cost_usd is None else str(stage.known_cost_usd),
            "cost_known": stage.known_cost_usd is not None,
        }
        prompt_known_sum += stage.prompt_tokens or 0
        completion_known_sum += stage.completion_tokens or 0
        reasoning_known_sum += stage.reasoning_tokens or 0
        total_known_sum += stage.total_tokens or 0

    cost = aggregate_cost(stages)
    return {
        "stages": per_stage,
        "aggregate": {
            "prompt_tokens_known_sum": prompt_known_sum,
            "completion_tokens_known_sum": completion_known_sum,
            "reasoning_tokens_known_sum": reasoning_known_sum,
            "total_tokens_known_sum": total_known_sum,
            "cost_usd_known_sum": str(cost.known_sum_usd),
            "cost_status": cost.status,
            "cost_complete": cost.complete,
            "unknown_cost_stage_ids": list(cost.unknown_stage_ids),
        },
    }


# --- Phase 10 M0.2: dual accounting (F-07, CAMPAIGN_EVIDENCE_EVOLUTION.md §4) -
# The two legacy functions above are untouched. Everything below is additive:
# a read-time derivation of the SELECTED spend from the flat per-attempt map,
# a staleness check for the stored selected-spend cache, and the v2 usage
# document builder (cumulative over every attempt + selected cache mirror).

def _spend_summary(rows: list[tuple[str, Decimal | None, int | None]]) -> dict[str, Any]:
    """Summarize spend rows with EXACTLY the status rules of ``aggregate_cost``.

    Each row is ``(label, known_cost_usd_or_None, total_tokens_or_None)``.
    Decision table mirrored verbatim from ``aggregate_cost`` (which itself is
    untouched): no rows -> known/complete; all unknown -> unknown/incomplete;
    some unknown -> partial/incomplete; zero known sum -> zero/complete; else
    known/complete. Unknown cost is never interpolated. Non-integer token
    values (corrupt entries) contribute 0 rather than raising.
    """
    known = [cost for _label, cost, _tokens in rows if cost is not None]
    unknown = tuple(label for label, cost, _tokens in rows if cost is None)
    known_sum = sum(known, Decimal("0"))
    if not rows:
        status = "known"
        complete = True
    elif len(unknown) == len(rows):
        status = "unknown"
        complete = False
    elif unknown:
        status = "partial"
        complete = False
    elif known_sum == 0:
        status = "zero"
        complete = True
    else:
        status = "known"
        complete = True
    total_tokens_known_sum = sum(
        tokens
        for _label, _cost, tokens in rows
        if isinstance(tokens, int) and not isinstance(tokens, bool)
    )
    return {
        "cost_usd_known_sum": str(known_sum),
        "cost_status": status,
        "cost_complete": complete,
        "unknown_cost_stage_ids": list(unknown),
        "total_tokens_known_sum": total_tokens_known_sum,
    }


def _entry_known_cost_usd(entry: Any) -> Decimal | None:
    """The known cost of one ``per_attempt`` entry, or None when unknown.

    Works on the SERIALIZED entry shape ``usage_document_v2`` writes (and that
    a reader loads back from usage.json): ``cost_known`` bool + ``cost_usd``
    decimal string. A missing/false ``cost_known``, a null cost, or a value
    that is not a parseable decimal is unknown — corruption never becomes
    invented money.
    """
    if not isinstance(entry, dict) or not entry.get("cost_known"):
        return None
    value = entry.get("cost_usd")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def derive_selected_spend(per_attempt: dict, selected_attempts: dict, stage_ids: list) -> dict:
    """Derive the SELECTED cost summary (F-07 read-time authority).

    Pure and disk-independent. ``per_attempt`` is the flat attempt map keyed
    ``"<stage_id>.att<N>"`` (the shape ``usage_document_v2`` writes and readers
    load back); ``selected_attempts`` maps stage_id -> selected attempt number,
    with unlisted stages defaulting to attempt 1 exactly like
    ``storage.read_selection``. ``stage_ids`` are the declared stage ids in
    order. Status rules are the SAME as ``aggregate_cost``; a stage whose
    selected attempt has no ``per_attempt`` entry counts as unknown for that
    stage. This derivation — never the stored ``selected_spend`` cache — is
    the authority for the pre-synthesis cost gate and UI projections.
    """
    rows: list[tuple[str, Decimal | None, int | None]] = []
    for stage_id in stage_ids:
        attempt = selected_attempts.get(stage_id, 1)
        entry = per_attempt.get(f"{stage_id}.att{attempt}")
        rows.append(
            (
                stage_id,
                _entry_known_cost_usd(entry),
                entry.get("total_tokens") if isinstance(entry, dict) else None,
            )
        )
    return _spend_summary(rows)


def selected_cache_is_stale(stored_selected: dict, derived_selected: dict) -> bool:
    """True when the stored selected-spend cache disagrees with the derivation.

    Compares exactly the pinned fields (CAMPAIGN_EVIDENCE_EVOLUTION.md §4):
    ``cost_usd_known_sum``, ``cost_status``, ``cost_complete`` and
    ``unknown_cost_stage_ids``. The derived value always wins; a disagreement
    is reported as an integrity warning, never silently trusted. Anything
    malformed (non-dict, non-list ids, non-string ids) counts as stale —
    fail-safe, so the caller re-derives.
    """
    if not isinstance(stored_selected, dict) or not isinstance(derived_selected, dict):
        return True
    stored_sum = stored_selected.get("cost_usd_known_sum")
    derived_sum = derived_selected.get("cost_usd_known_sum")
    if (None if stored_sum is None else str(stored_sum)) != (
        None if derived_sum is None else str(derived_sum)
    ):
        return True
    if stored_selected.get("cost_status") != derived_selected.get("cost_status"):
        return True
    if stored_selected.get("cost_complete") != derived_selected.get("cost_complete"):
        return True
    stored_unknown = stored_selected.get("unknown_cost_stage_ids")
    derived_unknown = derived_selected.get("unknown_cost_stage_ids")
    if not isinstance(stored_unknown, list) or not isinstance(derived_unknown, list):
        return True
    if not all(isinstance(item, str) for item in stored_unknown) or not all(
        isinstance(item, str) for item in derived_unknown
    ):
        return True
    return sorted(stored_unknown) != sorted(derived_unknown)


def usage_document_v2(
    attempt_records: list[StageRecord] | tuple[StageRecord, ...],
    *,
    selected_attempts: dict,
    stage_ids: list,
    run_id: str,
) -> dict[str, Any]:
    """Evidence-v2 usage document with dual accounting (CAMPAIGN_EVIDENCE_EVOLUTION.md §4).

    ``attempt_records`` is one :class:`StageRecord` per EXECUTED attempt
    (possibly several per stage). The document has exactly the frozen §4 shape:

    - ``cumulative_spend`` aggregates EVERY attempt record (financial truth);
      its ``unknown_cost_stage_ids`` lists the flat attempt keys
      ``"<stage_id>.att<N>"`` whose cost is unknown, so each unidentified spend
      is individually addressable;
    - ``selected_spend`` is the F-07 read-time derivation
      (:func:`derive_selected_spend`) — the authority, never a stored cache;
    - ``per_attempt`` holds the serialized per-attempt usage (legacy entry
      shape: tokens, ``cost_usd`` decimal string, ``cost_known``);
    - ``stages`` mirrors the legacy per-stage shape of ``usage_document`` for
      the SELECTED attempt of each declared stage; a declared stage whose
      selected attempt has no executed record is omitted (nothing is
      fabricated);
    - ``aggregate`` mirrors the derived ``selected_spend`` for legacy consumers.

    ``run_id`` is accepted for call-site binding symmetry with the other v2
    documents (``storage.persist_usage_v2`` passes the run directory name); the
    frozen §4 ``usage.json`` shape carries no ``run_id`` key, so it is not
    serialized. Unknown cost is never interpolated and no token-dollar
    projection is invented.
    """
    per_attempt: dict[str, Any] = {}
    cumulative_rows: list[tuple[str, Decimal | None, int | None]] = []
    for record in attempt_records:
        key = f"{record.id}.att{record.attempt_number}"
        per_attempt[key] = {
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "total_tokens": record.total_tokens,
            "cost_usd": None if record.known_cost_usd is None else str(record.known_cost_usd),
            "cost_known": record.known_cost_usd is not None,
        }
        cumulative_rows.append((key, record.known_cost_usd, record.total_tokens))

    # Legacy per-stage shape for the SELECTED attempt of each declared stage,
    # built by the untouched ``usage_document`` itself so the shape cannot
    # drift from v1. A selected attempt without an executed record is omitted.
    selected_records: list[StageRecord] = []
    for stage_id in stage_ids:
        record: StageRecord | None = None
        for candidate in attempt_records:
            if candidate.id == stage_id and candidate.attempt_number == selected_attempts.get(stage_id, 1):
                record = candidate  # last match wins: the most current snapshot
        if record is not None:
            selected_records.append(record)
    stages_shape = usage_document(selected_records)["stages"]

    selected_spend = derive_selected_spend(per_attempt, selected_attempts, stage_ids)
    return {
        "evidence_version": 2,
        "cumulative_spend": _spend_summary(cumulative_rows),
        "selected_spend": selected_spend,
        "per_attempt": per_attempt,
        "stages": stages_shape,
        "aggregate": dict(selected_spend),
    }
