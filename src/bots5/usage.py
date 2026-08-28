from __future__ import annotations

from decimal import Decimal
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
