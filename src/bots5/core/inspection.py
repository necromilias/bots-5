"""Read-only, display-safe historical inspection projections.

The projection is deliberately owned by the core.  Qt receives only these
typed values and never interprets persisted request snapshot versions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from bots5.domain.models import Attachment, Chat, GenerationAttempt, Message


_LEGACY_CORE_SNAPSHOT_KEYS = frozenset(
    {"attempt_id", "chat_id", "user_message_id", "backend_id", "model", "prompt"}
)


@dataclass(frozen=True, slots=True)
class InspectionField:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class InspectionProjection:
    chat_id: str
    selected_message_id: str | None
    historical_leaf_message_id: str | None
    status: str
    fields: tuple[InspectionField, ...]


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request snapshot key")
        result[key] = value
    return result


def _value(value: object, unavailable: str = "not recorded") -> str:
    if value is None:
        return unavailable
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_snapshot_is_authoritative(
    snapshot: Mapping[str, object],
    attempt: GenerationAttempt,
    user_content: str | None,
) -> bool:
    """Recognize only the original core request shape without rendering it.

    The versionless Phase 1/2 record was a flat serialization of the core
    request.  It has no settings or provider-configuration provenance.  Keep
    that historical boundary closed: unknown keys and nested values are not
    evidence that may be displayed by the Inspector.
    """
    if not set(snapshot) <= _LEGACY_CORE_SNAPSHOT_KEYS:
        return False
    expected = {
        "attempt_id": attempt.id,
        "chat_id": attempt.chat_id,
        "user_message_id": attempt.user_message_id,
        "backend_id": attempt.backend_id,
        "model": attempt.model,
    }
    if any(snapshot.get(key) != value for key, value in expected.items() if key in snapshot):
        return False
    prompt = snapshot.get("prompt")
    return (
        "prompt" not in snapshot
        or (type(prompt) is str and (user_content is None or prompt == user_content))
    )


def _snapshot_fields(attempt: GenerationAttempt, user_content: str | None) -> tuple[str, tuple[InspectionField, ...]]:
    try:
        snapshot = json.loads(attempt.request_snapshot, object_pairs_hook=_pairs)
    except (TypeError, ValueError):
        return "corrupt request snapshot", ()
    if not isinstance(snapshot, dict):
        return "corrupt request snapshot", ()
    version = snapshot.get("snapshot_version")
    if version in (2, 3):
        try:
            # Persistence owns historical snapshot validation.  Import it only
            # once the core projection is actually requested: importing the
            # rooted VFS must remain possible before the core package has
            # completed application initialization.
            from bots5.infrastructure.persistence.phase3_validation import (
                validate_request_snapshot,
            )

            validate_request_snapshot(
                attempt_id=attempt.id, chat_id=attempt.chat_id,
                user_message_id=attempt.user_message_id, backend_id=attempt.backend_id,
                model=attempt.model, provider_id=attempt.provider_id,
                request_snapshot=attempt.request_snapshot,
                user_message_content=user_content,
            )
        except ValueError:
            return "corrupt request snapshot", ()
    elif version is None:
        # Versionless Phase 3 records have an independently closed validator.
        # The pre-Phase-3 core request schema is the only other known legacy
        # format; it is intentionally flat and exposes no configuration.
        from bots5.infrastructure.persistence.phase3_validation import (
            is_phase3_record,
            validate_request_snapshot,
        )

        try:
            is_phase3 = is_phase3_record(
                backend_id=attempt.backend_id,
                provider_id=attempt.provider_id,
                snapshot=snapshot,
            )
            if is_phase3:
                validate_request_snapshot(
                    attempt_id=attempt.id, chat_id=attempt.chat_id,
                    user_message_id=attempt.user_message_id, backend_id=attempt.backend_id,
                    model=attempt.model, provider_id=attempt.provider_id,
                    request_snapshot=attempt.request_snapshot,
                    user_message_content=user_content,
                    phase3=True,
                )
            elif not _legacy_snapshot_is_authoritative(snapshot, attempt, user_content):
                return "legacy-limited request snapshot", ()
        except ValueError:
            return "corrupt request snapshot", ()
    else:
        # No versioned v1 schema was shipped.  Newer and otherwise unknown
        # versions are data, not trusted historical provenance.
        return "unsupported request snapshot", ()

    fields: list[InspectionField] = [InspectionField("Snapshot", "legacy/v1" if version is None else f"v{version}")]
    for name, key in (
        ("Frozen provider", "provider_id"), ("Frozen model", "model"),
        ("Connection", "connection_name"), ("Connection ID", "connection_id"),
        ("Model entry", "model_entry_id"), ("Settings", "effective_settings"),
        ("Settings provenance", "settings_provenance"),
        ("Capabilities", "capabilities"), ("Capability provenance", "capability_provenance"),
        ("Manual overrides", "manual_overrides"),
    ):
        if key in snapshot:
            fields.append(InspectionField(name, _value(snapshot[key])))
    if version == 3:
        plan = snapshot.get("context_plan")
        if not isinstance(plan, Mapping):
            return "corrupt request snapshot", ()
        for name, key in (
            ("Context digest", "canonical_digest"), ("Context included", "included_sources"),
            ("Context excluded", "excluded_sources"), ("Context parent", "parent_id"),
            ("Context lineage", "lineage_id"), ("Context budget", "budget"),
            ("Context input counts", "input_counts"),
        ):
            if key in plan:
                fields.append(InspectionField(name, _value(plan[key])))
        sources = plan.get("sources")
        if isinstance(sources, list):
            safe_sources = []
            for source in sources:
                if isinstance(source, Mapping):
                    safe_sources.append({key: source.get(key) for key in (
                        "source_id", "kind", "role", "state", "eligible", "selected", "reason",
                        "representation_id", "representation_digest",
                    )})
            fields.append(InspectionField("Context sources", _safe_json(safe_sources)))
    return "available", tuple(fields)


def _attachment_fields(prefix: str, attachments: tuple[Attachment, ...]) -> tuple[InspectionField, ...]:
    if not attachments:
        return (InspectionField(prefix, "none"),)
    return tuple(
        InspectionField(
            f"{prefix} {index}",
            _safe_json({
                "id": item.id, "filename": item.filename, "source_kind": item.source_kind,
                "source_name": item.source_name, "blob_digest": item.blob_digest,
                "text_representation_id": item.text_representation_id, "text_digest": item.text_digest,
                "ineligibility_reason": item.ineligibility_reason,
            }),
        )
        for index, item in enumerate(attachments, 1)
    )


def build_inspection_projection(
    *, chat: Chat, message: Message | None, historical_leaf_message_id: str | None,
    revision_count: int, attempts: tuple[GenerationAttempt, ...],
    user_content_by_attempt: Mapping[str, str | None],
    message_attachments: tuple[Attachment, ...],
    attempt_attachments: Mapping[str, tuple[Attachment, ...]],
    import_provenance: Mapping[str, str] | None = None,
) -> InspectionProjection:
    import_provenance = import_provenance or {}
    fields = [
        InspectionField("Chat", chat.title), InspectionField("Chat ID", chat.id),
        InspectionField("Chat revision", str(chat.revision)),
        InspectionField("Active head", chat.head_message_id or "none"),
        InspectionField("Historical leaf", historical_leaf_message_id or "active head"),
        InspectionField("Import provenance", "recorded" if import_provenance else "native"),
        InspectionField("Export provenance", "not recorded"),
    ]
    fields.extend(
        InspectionField(f"Import {name}", value)
        for name, value in sorted(import_provenance.items())
    )
    if message is None:
        fields.append(InspectionField("Message", "No message selected"))
        for attempt in attempts:
            fields.extend((InspectionField("History attempt", attempt.id), InspectionField("History provider/model", f"{attempt.provider_id or attempt.backend_id} / {attempt.model}")))
        return InspectionProjection(chat.id, None, historical_leaf_message_id, "available", tuple(fields))
    fields.extend((
        InspectionField("Message ID", message.id), InspectionField("Lineage", message.lineage_id or message.id),
        InspectionField("Revision", f"{message.revision} of {revision_count}"),
        InspectionField("Parent", message.parent_id or "none"), InspectionField("State", message.state.value),
    ))
    fields.extend(_attachment_fields("Message attachment", message_attachments))
    relevant = tuple(item for item in attempts if item.user_message_id == message.id or item.assistant_message_id == message.id)
    if not relevant:
        fields.append(InspectionField("Generation", "not applicable"))
    status = "available"
    for index, attempt in enumerate(relevant, 1):
        prefix = f"Attempt {index}"
        fields.extend((
            InspectionField(f"{prefix} ID", attempt.id), InspectionField(f"{prefix} state", attempt.state.value),
            InspectionField(f"{prefix} request ID", attempt.request_id or "not recorded"),
            InspectionField(f"{prefix} provider/model", f"{attempt.provider_id or attempt.backend_id} / {attempt.model}"),
            InspectionField(f"{prefix} returned model", attempt.returned_model or "not recorded"),
            InspectionField(f"{prefix} finish", attempt.finish_reason or "not recorded"),
            InspectionField(f"{prefix} started", attempt.started_at.isoformat()),
            InspectionField(f"{prefix} ended", attempt.ended_at.isoformat() if attempt.ended_at else "running"),
            InspectionField(f"{prefix} error type", attempt.error_type or "none"),
            InspectionField(f"{prefix} error", attempt.error_message or "none"),
            InspectionField(f"{prefix} tokens", _safe_json({"prompt": attempt.prompt_tokens, "completion": attempt.completion_tokens, "reasoning": attempt.reasoning_tokens, "total": attempt.total_tokens, "cost_usd": str(attempt.known_cost_usd) if attempt.known_cost_usd is not None else None, "remote_outcome_unknown": attempt.remote_outcome_unknown})),
        ))
        snapshot_status, snapshot_fields = _snapshot_fields(attempt, user_content_by_attempt.get(attempt.id))
        if snapshot_status != "available":
            status = snapshot_status
        fields.append(InspectionField(f"{prefix} snapshot", snapshot_status))
        fields.extend(InspectionField(f"{prefix} {item.name}", item.value) for item in snapshot_fields)
        fields.extend(_attachment_fields(f"{prefix} attachment", attempt_attachments.get(attempt.id, ())))
    return InspectionProjection(chat.id, message.id, historical_leaf_message_id, status, tuple(fields))
