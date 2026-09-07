"""Deterministic, headless context construction.

The context builder is intentionally a small core port.  It does not know
about Qt, SQLAlchemy, providers, or filesystem paths.  Callers give it an
already resolved request lineage and an exact accounting adapter; the result
is an immutable plan and the exact wire representation owned by that adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence


class ContextBuildError(ValueError):
    """A context cannot be built without violating the deterministic policy."""


@dataclass(frozen=True, slots=True)
class ContextSource:
    """One candidate context item and its immutable selection decision."""

    source_id: str
    kind: str
    role: str
    content: str
    state: str = "complete"
    eligible: bool = True
    selected: bool = True
    reason: str = "selected"
    representation_id: str | None = None
    representation_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.kind or not self.role:
            raise ContextBuildError("context source identity is required")
        if type(self.content) is not str:
            raise ContextBuildError("context source content must be text")
        if self.selected and not self.eligible:
            raise ContextBuildError(
                f"selected context source is ineligible: {self.source_id}"
            )


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Closed accounting evidence captured in a frozen context plan."""

    limit: int
    provenance: str
    semantics: str
    adapter_id: str
    adapter_version: str
    output_reserve: int
    envelope_overhead: int
    input_units: int
    total_units: int
    headroom: int

    def __post_init__(self) -> None:
        if type(self.limit) is not int or self.limit <= 0:
            raise ContextBuildError("context window must be a positive integer")
        if type(self.output_reserve) is not int or self.output_reserve < 0:
            raise ContextBuildError("output reserve must be a nonnegative integer")
        if type(self.envelope_overhead) is not int or self.envelope_overhead < 0:
            raise ContextBuildError("envelope overhead must be a nonnegative integer")
        if self.total_units != self.input_units + self.envelope_overhead + self.output_reserve:
            raise ContextBuildError("context budget accounting is inconsistent")
        if self.headroom != self.limit - self.total_units:
            raise ContextBuildError("context budget headroom is inconsistent")
        if self.headroom < 0:
            raise ContextBuildError("context budget exceeds the context window")


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """Canonical frozen context state and exact adapter-owned wire payload."""

    version: int
    sources: tuple[ContextSource, ...]
    included_sources: tuple[str, ...]
    excluded_sources: tuple[str, ...]
    canonical_representation: str
    canonical_digest: str
    wire_representation: bytes
    budget: ContextBudget
    input_counts: dict[str, int] = field(default_factory=dict)
    parent_id: str | None = None
    lineage_id: str | None = None

    def __post_init__(self) -> None:
        if self.version != 3:
            raise ContextBuildError("unsupported context plan version")
        source_ids = tuple(source.source_id for source in self.sources)
        if len(source_ids) != len(set(source_ids)):
            raise ContextBuildError("context source identities must be unique")
        if any(not source.selected and not source.reason for source in self.sources):
            raise ContextBuildError("excluded context sources require a reason")
        if hashlib.sha256(self.canonical_representation.encode("utf-8")).hexdigest() != self.canonical_digest:
            raise ContextBuildError("context canonical digest does not match representation")
        if tuple(source.source_id for source in self.sources if source.selected) != self.included_sources:
            raise ContextBuildError("context inclusion order is inconsistent")
        excluded = tuple(source.source_id for source in self.sources if not source.selected)
        if excluded != self.excluded_sources:
            raise ContextBuildError("context exclusion order is inconsistent")
        try:
            canonical = json.loads(
                self.canonical_representation,
                object_pairs_hook=_duplicate_rejecting_object,
            )
        except (TypeError, ValueError) as exc:
            raise ContextBuildError("context canonical representation is invalid") from exc
        _validate_canonical_structure(
            canonical,
            sources=tuple(_source_dict(source) for source in self.sources),
            included_sources=self.included_sources,
            excluded_sources=self.excluded_sources,
            wire_digest=hashlib.sha256(self.wire_representation).hexdigest(),
            representation=self.canonical_representation,
        )
        _validate_budget(self.budget)


class ContextAccountingAdapter(Protocol):
    """Exact token/units accounting and serialization owned by one adapter."""

    adapter_id: str
    adapter_version: str
    semantics: str

    def encode(self, messages: Sequence[dict[str, str]], envelope: dict[str, object]) -> bytes:
        ...

    def measure(self, wire_representation: bytes) -> int:
        ...


@dataclass(frozen=True, slots=True)
class DeterministicJsonAdapter:
    """Small deterministic adapter suitable for tests and local fake use.

    It deliberately counts declared UTF-8 code points, not as a model token
    authority.  Production adapters must provide their own exact semantics.
    """

    adapter_id: str = "bots5.deterministic-json"
    adapter_version: str = "1"
    semantics: str = "UTF-8 code points in canonical JSON wire envelope"

    def encode(self, messages: Sequence[dict[str, str]], envelope: dict[str, object]) -> bytes:
        return json.dumps(
            {"messages": list(messages), "envelope": envelope},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def measure(self, wire_representation: bytes) -> int:
        return len(wire_representation.decode("utf-8"))


def _source_dict(source: ContextSource) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "kind": source.kind,
        "role": source.role,
        "content": source.content,
        "state": source.state,
        "eligible": source.eligible,
        "selected": source.selected,
        "reason": source.reason,
        "representation_id": source.representation_id,
        "representation_digest": source.representation_digest,
    }


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("context canonical representation contains duplicate keys")
        result[key] = value
    return result


def _validate_budget(budget: ContextBudget) -> None:
    if type(budget.limit) is not int or budget.limit <= 0:
        raise ContextBuildError("context budget limit is invalid")
    if type(budget.output_reserve) is not int or budget.output_reserve < 0:
        raise ContextBuildError("context budget output reserve is invalid")
    if type(budget.envelope_overhead) is not int or budget.envelope_overhead < 0:
        raise ContextBuildError("context budget envelope overhead is invalid")
    if type(budget.input_units) is not int or budget.input_units < 0:
        raise ContextBuildError("context budget input units are invalid")
    if type(budget.total_units) is not int or budget.total_units < 0:
        raise ContextBuildError("context budget total units are invalid")
    if budget.total_units != budget.input_units + budget.envelope_overhead + budget.output_reserve:
        raise ContextBuildError("context budget accounting is inconsistent")
    if budget.headroom != budget.limit - budget.total_units:
        raise ContextBuildError("context budget headroom is inconsistent")


def _validate_canonical_structure(
    canonical: object,
    *,
    sources: tuple[dict[str, object], ...],
    included_sources: tuple[str, ...],
    excluded_sources: tuple[str, ...],
    wire_digest: str,
    representation: str,
) -> None:
    if not isinstance(canonical, dict) or set(canonical) != {
        "version", "sources", "included_sources", "excluded_sources", "envelope", "wire_sha256",
    }:
        raise ContextBuildError("context canonical representation is not the closed schema")
    if canonical["version"] != 3 or canonical["sources"] != list(sources):
        raise ContextBuildError("context canonical representation does not match sources")
    if canonical["included_sources"] != list(included_sources):
        raise ContextBuildError("context canonical representation does not match inclusion")
    if canonical["excluded_sources"] != list(excluded_sources):
        raise ContextBuildError("context canonical representation does not match exclusion")
    if (
        type(canonical["wire_sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", canonical["wire_sha256"]) is None
        or canonical["wire_sha256"] != wire_digest
    ):
        raise ContextBuildError("context canonical wire digest is invalid")
    canonical_text = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if canonical_text != representation:
        raise ContextBuildError("context canonical representation is not canonical")


class ContextBuilder:
    """Build a deterministic whole-turn suffix with explicit exclusions."""

    def __init__(self, adapter: ContextAccountingAdapter | None = None) -> None:
        self.adapter = adapter or DeterministicJsonAdapter()

    def build(
        self,
        *,
        current_user: ContextSource,
        historical_turns: Sequence[Sequence[ContextSource]] = (),
        selected_attachments: Sequence[ContextSource] = (),
        bots_required_instructions: Sequence[ContextSource] = (),
        envelope: dict[str, object] | None = None,
        context_window: int,
        context_window_provenance: str,
        output_reserve: int,
        parent_id: str | None = None,
        lineage_id: str | None = None,
    ) -> ContextPlan:
        if not context_window_provenance or type(context_window_provenance) is not str:
            raise ContextBuildError("context window provenance is required")
        if type(context_window) is not int or context_window <= 0:
            raise ContextBuildError("context window must be a positive integer")
        if type(output_reserve) is not int or output_reserve < 0:
            raise ContextBuildError("output reserve must be a nonnegative integer")
        if not current_user.selected or current_user.kind != "current_user":
            raise ContextBuildError("current user is mandatory context")
        for source in (*bots_required_instructions, *selected_attachments):
            if not source.selected:
                raise ContextBuildError(f"mandatory context source was excluded: {source.source_id}")
            if not source.eligible:
                raise ContextBuildError(f"selected context source is ineligible: {source.source_id}")

        envelope = dict(envelope or {})
        mandatory = [*bots_required_instructions, *selected_attachments, current_user]
        identities = [source.source_id for source in mandatory]
        for turn in historical_turns:
            identities.extend(source.source_id for source in turn)
        if len(identities) != len(set(identities)):
            raise ContextBuildError("context source identities must be unique")
        complete_turns: list[tuple[ContextSource, ...]] = []
        for turn in historical_turns:
            values = tuple(turn)
            if not values:
                continue
            if any(not source.selected for source in values):
                raise ContextBuildError("historical context sources must start selected")
            # Complete turns are preferred.  A visible partial/aborted turn is
            # admissible only because its actual lifecycle state is retained
            # in each source; failed empty attempts are never history.
            if any(source.state != "complete" for source in values) and all(
                not source.content for source in values
            ):
                continue
            complete_turns.append(values)

        def compose(turns: Sequence[Sequence[ContextSource]]) -> tuple[ContextSource, ...]:
            return tuple(source for turn in turns for source in turn) + tuple(mandatory)

        turns = list(complete_turns)
        excluded: list[ContextSource] = []
        excluded_turn_count = 0
        while True:
            candidates = compose(turns)
            messages = tuple({"role": item.role, "content": item.content} for item in candidates)
            wire = self.adapter.encode(messages, envelope)
            wire_units = self.adapter.measure(wire)
            # The adapter owns the envelope representation.  Overhead is the
            # measured wire envelope without message content, measured exactly.
            empty_wire = self.adapter.encode((), envelope)
            envelope_overhead = self.adapter.measure(empty_wire)
            input_units = max(0, wire_units - envelope_overhead)
            total = input_units + envelope_overhead + output_reserve
            if total <= context_window:
                break
            if not turns:
                raise ContextBuildError(
                    "mandatory context exceeds context window before dispatch"
                )
            excluded_turn_count += 1
            excluded.extend(turns.pop(0))

        # Keep every candidate exactly once.  Excluded whole turns are marked
        # in this frozen source list; appending them again would manufacture
        # historical duplicate identities and make a valid suffix unverifiable.
        all_sources = tuple(source for turn in complete_turns for source in turn) + tuple(mandatory)
        included = tuple(source.source_id for source in compose(turns))
        excluded_ids = tuple(source.source_id for source in excluded)
        sources: list[ContextSource] = []
        included_set = set(included)
        for source in all_sources:
            if source.source_id in included_set:
                sources.append(source)
            else:
                sources.append(ContextSource(**{**_source_dict(source), "selected": False, "reason": "excluded_oldest_complete_turn"}))
        canonical = json.dumps(
            {
                "version": 3,
                "sources": [_source_dict(source) for source in sources],
                "included_sources": included,
                "excluded_sources": excluded_ids,
                "envelope": envelope,
                "wire_sha256": hashlib.sha256(wire).hexdigest(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        budget = ContextBudget(
            limit=context_window,
            provenance=context_window_provenance,
            semantics=self.adapter.semantics,
            adapter_id=self.adapter.adapter_id,
            adapter_version=self.adapter.adapter_version,
            output_reserve=output_reserve,
            envelope_overhead=envelope_overhead,
            input_units=input_units,
            total_units=total,
            headroom=context_window - total,
        )
        return ContextPlan(
            version=3,
            sources=tuple(sources),
            included_sources=included,
            excluded_sources=excluded_ids,
            canonical_representation=canonical,
            canonical_digest=digest,
            wire_representation=wire,
            budget=budget,
            input_counts={
                "history_turns_considered": len(complete_turns),
                "history_turns_included": len(turns),
                "history_turns_excluded": excluded_turn_count,
                "mandatory_sources": len(mandatory),
            },
            parent_id=parent_id,
            lineage_id=lineage_id,
        )


def phase6_snapshot(
    *,
    attempt_id: str,
    chat_id: str,
    user_message_id: str,
    backend_id: str,
    model: str,
    provider_id: str | None,
    prompt: str,
    plan: ContextPlan,
    frozen_fields: dict[str, object] | None = None,
) -> str:
    """Serialize the closed v3 snapshot used by the persistence boundary."""
    payload = {
        "snapshot_version": 3,
        "attempt_id": attempt_id,
        "chat_id": chat_id,
        "user_message_id": user_message_id,
        "backend_id": backend_id,
        "model": model,
        "provider_id": provider_id,
        "prompt": prompt,
        "context_plan": {
            "version": plan.version,
            "sources": [_source_dict(source) for source in plan.sources],
            "included_sources": list(plan.included_sources),
            "excluded_sources": list(plan.excluded_sources),
            "canonical_representation": plan.canonical_representation,
            "canonical_digest": plan.canonical_digest,
            "wire_representation_sha256": hashlib.sha256(plan.wire_representation).hexdigest(),
            "budget": {
                "limit": plan.budget.limit,
                "provenance": plan.budget.provenance,
                "semantics": plan.budget.semantics,
                "adapter_id": plan.budget.adapter_id,
                "adapter_version": plan.budget.adapter_version,
                "output_reserve": plan.budget.output_reserve,
                "envelope_overhead": plan.budget.envelope_overhead,
                "input_units": plan.budget.input_units,
                "total_units": plan.budget.total_units,
                "headroom": plan.budget.headroom,
            },
            "input_counts": dict(plan.input_counts),
            "parent_id": plan.parent_id,
            "lineage_id": plan.lineage_id,
        },
    }
    if frozen_fields:
        for key, value in frozen_fields.items():
            if key in payload and key not in {"provider_id", "model", "prompt"}:
                raise ContextBuildError(f"duplicate Phase 6 snapshot field: {key}")
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
