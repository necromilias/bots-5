"""Core-owned Phase 9 read projections for Transcript v0.1 and Archive v1."""

from __future__ import annotations

import json
import re
import unicodedata
import math
from decimal import Decimal
from html import escape as html_escape
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from bots5.domain.models import AttemptState, Attachment, Chat, GenerationAttempt, Message, MessageState

from .interchange import InterchangeError, canonical_json_bytes, canonical_jsonl_bytes, sha256_hex, strict_json_loads, utc_timestamp


class TranscriptScope(StrEnum):
    ACTIVE_PATH = "active-path"
    FULL_LINEAGE = "full-lineage"


class AttachmentPolicy(StrEnum):
    EMBEDDED = "embedded"
    EXTERNAL_REFERENCE = "external-reference"


class ExportError(ValueError):
    pass


class ArchiveVersionRequired(ExportError):
    """A frozen v1 request would lose provenance or availability truth."""

    def __init__(self, required: int = 2):
        super().__init__(f"Archive version {required} is required for full-fidelity export")
        self.required = required


def select_archive_version(
    *, requested: int | None, has_import_provenance: bool, has_missing_external_reference: bool,
) -> int:
    """Choose the only lossless format before any output publication.

    V1 stays available for wholly native graphs.  The caller supplies facts
    from its coherent store-owned export cut; this pure policy never guesses
    from a migration version or source archive format.
    """
    if requested not in {None, 1, 2}:
        raise ExportError("requested archive version is unsupported")
    requires_v2 = has_import_provenance or has_missing_external_reference
    if requested == 1 and requires_v2:
        raise ArchiveVersionRequired(2)
    return 2 if requires_v2 or requested == 2 else 1


@dataclass(frozen=True, slots=True)
class TranscriptExport:
    content: bytes
    scope: TranscriptScope


@dataclass(frozen=True, slots=True)
class ArchiveLogicalEntry:
    path: str
    media_type: str
    required: bool
    content: bytes


@dataclass(frozen=True, slots=True)
class ArchiveProjection:
    archive_id: str
    created_at: datetime
    chat_id: str
    chat_title: str
    attachment_policy: AttachmentPolicy
    entries: tuple[ArchiveLogicalEntry, ...]
    manifest_base: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ExportAttachment:
    """One attachment relationship as observed in the export source cut."""

    attachment: Attachment
    byte_size: int
    integrity_status: str
    payload: bytes | None = None
    # Imported references retain their sealed safe wire metadata.  A current
    # local backing is only a representation; it must not recast the source
    # filename/source-kind statuses on a later v2 hop.
    source_metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.source_metadata is not None:
            object.__setattr__(self, "source_metadata", _freeze(self.source_metadata))


@dataclass(frozen=True, slots=True)
class ChatExportSource:
    """Immutable, store-owned point-in-time export input."""

    chat: Chat | None
    messages: tuple[Message, ...]
    attempts: tuple[GenerationAttempt, ...]
    message_attachments: Mapping[str, tuple[ExportAttachment, ...]]
    attempt_attachments: Mapping[str, tuple[ExportAttachment, ...]]
    context_plans: Mapping[str, Mapping[str, object]]
    chat_configuration: Mapping[str, object]
    captured_at: datetime
    requires_v2: bool = False
    object_provenance: tuple[Mapping[str, object], ...] = ()
    continuation_history: Mapping[str, object] | None = None
    history_bindings: tuple[Mapping[str, object], ...] = ()
    archived_attempt_provenance: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_attachments", _freeze(self.message_attachments))
        object.__setattr__(self, "attempt_attachments", _freeze(self.attempt_attachments))
        object.__setattr__(self, "context_plans", _freeze(self.context_plans))
        object.__setattr__(self, "chat_configuration", _freeze(self.chat_configuration))
        object.__setattr__(self, "object_provenance", _freeze(self.object_provenance))
        if self.continuation_history is not None:
            object.__setattr__(self, "continuation_history", _freeze(self.continuation_history))
        object.__setattr__(self, "history_bindings", _freeze(self.history_bindings))
        object.__setattr__(self, "archived_attempt_provenance", _freeze(self.archived_attempt_provenance))


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value):
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _export_attachment(value: Attachment | ExportAttachment) -> Attachment:
    return value.attachment if isinstance(value, ExportAttachment) else value


SAFE_REDACTED_METADATA = "[redacted]"
_SAFE_INSTALLATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_PROVIDER_MODEL_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:+@=-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9._:+@=-]{0,127})*$"
)
_SAFE_DISPLAY_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,:;_+()=-]{0,255}$")
_SENSITIVE_METADATA_LABEL = re.compile(
    r"(?<![A-Za-z0-9])(?:api[ _-]?key|authorization|bearer|credential|request[ _-]?(?:id|identifier)|secret|token)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CREDENTIAL_TOKEN_PREFIX = re.compile(
    r"^(?:ak|api|bearer|key|pk|rk|secret|sk|token)[_-][A-Za-z0-9_-]+$", re.IGNORECASE,
)
_HOST_OR_ENDPOINT = re.compile(
    r"^(?:https?:|[A-Za-z]:[\\/]|localhost(?::\d+)?(?:[\\/]|$)|(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:[\\/]|$)|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:[\\/]|$))",
    re.IGNORECASE,
)
_SAFE_CONTEXT_STATE = frozenset({
    "sending", "sent", "failed", "streaming", "complete", "incomplete", "truncated", "aborted",
})
_SAFE_BACKEND_TYPES = frozenset({"fake", "openai_compatible_http"})
_SAFE_PROVIDER_PROFILES = frozenset({"generic", "openrouter"})
_SAFE_CATALOGUE_ORIGINS = frozenset({"manual", "discovered", "manual_confirmed"})
_SAFE_CATALOGUE_AVAILABILITY = frozenset({"available", "unavailable", "stale", "disconnected"})
_SAFE_PROVIDER_IDS = frozenset({"fake", "generic", "local_openai", "openrouter"})
_SAFE_CAPABILITY_KEYS = frozenset({
    "generation.streaming", "request.temperature", "request.max_output_tokens",
    "request.reasoning_effort.none", "limits.context_tokens", "limits.output_tokens",
    "telemetry.usage", "telemetry.reasoning_tokens", "telemetry.cost",
    "telemetry.request_id", "telemetry.returned_model",
})
_SAFE_CONTEXT_REASON = frozenset({
    "selected", "excluded_oldest_complete_turn", "not_text", "invalid_utf8", "contains_nul",
})
_SAFE_FINISH_REASONS = frozenset({"stop", "length"})
SAFE_FAILURE_MESSAGE = "B.O.T.S. recorded a generation failure."
SAFE_NONSTOP_FINISH_REASON = "non-stop"


def _redacted_metadata() -> tuple[str, str]:
    return SAFE_REDACTED_METADATA, "redacted"


def _safe_installation_id(value: object) -> tuple[str, str]:
    """Keep bounded historical domain IDs; they are not provider display text."""
    if type(value) is str and _SAFE_INSTALLATION_ID.fullmatch(value):
        return value, "available"
    return _redacted_metadata()


def _safe_provider_model_id(value: object) -> tuple[str, str]:
    """Keep a closed provider-model identifier, including namespace/model forms."""
    if (
        type(value) is str
        and len(value) <= 256
        and _SAFE_PROVIDER_MODEL_ID.fullmatch(value)
        and not value.casefold().startswith(("http:", "https:"))
        and all(part not in {".", ".."} for part in value.split("/"))
        and not _HOST_OR_ENDPOINT.match(value)
        and not _SENSITIVE_METADATA_LABEL.search(value)
        and not _CREDENTIAL_TOKEN_PREFIX.fullmatch(value)
    ):
        return value, "available"
    return _redacted_metadata()


def _safe_display_metadata(value: object) -> tuple[str, str]:
    """Keep bounded human metadata only when it is neither host nor secret-shaped."""
    if (
        type(value) is str
        and _SAFE_DISPLAY_METADATA.fullmatch(value)
        and not _HOST_OR_ENDPOINT.match(value)
        and not _SENSITIVE_METADATA_LABEL.search(value)
        and not _CREDENTIAL_TOKEN_PREFIX.fullmatch(value)
    ):
        return value, "available"
    return _redacted_metadata()


def _safe_closed_metadata(value: object, allowed: frozenset[str]) -> tuple[str, str]:
    if type(value) is str and value in allowed:
        return value, "available"
    return _redacted_metadata()


def metadata_status_matches(value: object, status: object, projector) -> bool:
    """Whether one status-bearing metadata value is exactly its closed projection."""
    if status == "redacted":
        return value == SAFE_REDACTED_METADATA
    return status == "available" and projector(value) == (value, "available")


def safe_finish_reason(value: object, state: AttemptState | str | None = None) -> str | None:
    """Retain only closed provider outcome facts; never export unknown raw text."""
    if state in {AttemptState.FAILED, AttemptState.ABORTED, "failed", "aborted"}:
        return None
    if value is None:
        return None
    if type(value) is str and value in _SAFE_FINISH_REASONS:
        return value
    return SAFE_NONSTOP_FINISH_REASON


def _safe_returned_model(value: object, requested_model: object) -> str | None:
    """A provider outcome may repeat the frozen model ID, never an opaque outcome ID."""
    if value is None:
        return None
    if value != requested_model:
        return SAFE_REDACTED_METADATA
    return _safe_provider_model_id(value)[0]


def _markdown_text(value: str) -> str:
    """Render one user-controlled metadata value as non-structural Markdown text."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    escaped = html_escape(normalized, quote=False)
    return re.sub(r"([\\\\`*_{}\[\]()#+.!|])", lambda match: "\\" + match.group(1), escaped)


def _inline_code(value: str) -> str:
    """Render a validated one-line value without letting its backticks end the span."""
    longest = max((len(match.group()) for match in re.finditer(r"`+", value)), default=0)
    delimiter = "`" * (longest + 1)
    if value.startswith(("`", " ")) or value.endswith(("`", " ")):
        # Separate a boundary delimiter run from a literal boundary backtick.
        # CommonMark removes the added paired padding while retaining any
        # original leading or trailing space in a nonblank filename.
        return f"{delimiter} {value} {delimiter}"
    return f"{delimiter}{value}{delimiter}"


def _safe_attachment_filename(value: object) -> tuple[str, str]:
    """Project the durable attachment basename without provider-metadata rules."""
    if (
        type(value) is str
        and 0 < len(value) <= 255
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and not any(unicodedata.category(character).startswith("C") for character in value)
    ):
        return value, "available"
    return _redacted_metadata()


def _fence(text: str) -> str:
    marker = "```"
    while marker in text:
        marker += "`"
    return f"{marker}text\n{text}\n{marker}"


def _active_path(chat: Chat, messages: tuple[Message, ...]) -> tuple[Message, ...]:
    if chat.head_message_id is None:
        return ()
    by_id = {item.id: item for item in messages}
    current = chat.head_message_id
    result: list[Message] = []
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise ExportError("message graph contains a cycle")
        seen.add(current)
        message = by_id.get(current)
        if message is None:
            raise ExportError("chat head is not an exported message")
        result.append(message)
        current = message.parent_id
    result.reverse()
    return tuple(result)


def _attachment_metadata(attachment: Attachment | ExportAttachment) -> str:
    captured = attachment if isinstance(attachment, ExportAttachment) else None
    if captured is not None:
        attachment = captured.attachment
    filename, _ = _safe_attachment_filename(attachment.filename)
    availability = "eligible" if attachment.text_representation_id else (
        attachment.ineligibility_reason if attachment.ineligibility_reason in _SAFE_CONTEXT_REASON else "unavailable"
    )
    return (
        f"- {_inline_code(filename)} — SHA-256 {_inline_code(attachment.blob_digest)}, "
        f"bytes: {_markdown_text(str(captured.byte_size if captured is not None else 'not captured'))}, "
        f"integrity/availability: {_markdown_text(str(captured.integrity_status if captured is not None else 'not-verified'))}, "
        f"text status: {_markdown_text(availability)}"
    )


def _attachment_row(value: Attachment | ExportAttachment) -> dict[str, object]:
    attachment = _export_attachment(value)
    if isinstance(value, ExportAttachment) and value.source_metadata is not None:
        metadata = value.source_metadata
        try:
            result = {
                "source_id": attachment.id, "blob_digest": attachment.blob_digest,
                "filename": metadata["filename"], "filename_status": metadata["filename_status"],
                "source_kind": metadata["source_kind"], "source_kind_status": metadata["source_kind_status"],
                "text_representation_id": metadata["text_representation_id"],
                "text_digest": metadata["text_digest"], "text_eligibility": metadata["text_eligibility"],
                "ineligibility_reason": metadata["ineligibility_reason"],
                "created_at": metadata["created_at"], "byte_size": value.byte_size,
                "integrity_status": value.integrity_status,
            }
        except KeyError as exc:
            raise ExportError("imported attachment metadata is incomplete") from exc
        return result
    filename, filename_status = _safe_attachment_filename(attachment.filename)
    source_kind, source_kind_status = _safe_closed_metadata(
        attachment.source_kind, frozenset({"filesystem"}),
    )
    result = {
        "source_id": attachment.id, "blob_digest": attachment.blob_digest,
        "filename": filename, "filename_status": filename_status,
        "source_kind": source_kind, "source_kind_status": source_kind_status,
        "text_representation_id": attachment.text_representation_id,
        "text_digest": attachment.text_digest,
        "text_eligibility": "eligible" if attachment.text_representation_id else "unavailable",
        "ineligibility_reason": (
            attachment.ineligibility_reason
            if attachment.ineligibility_reason in _SAFE_CONTEXT_REASON else None
        ),
        "created_at": utc_timestamp(attachment.created_at),
    }
    if isinstance(value, ExportAttachment):
        result["byte_size"] = value.byte_size
        result["integrity_status"] = value.integrity_status
    return result


def build_transcript(
    *, chat: Chat, messages: tuple[Message, ...], attempts: tuple[GenerationAttempt, ...],
    message_attachments: Mapping[str, tuple[Attachment | ExportAttachment, ...]],
    attempt_attachments: Mapping[str, tuple[Attachment | ExportAttachment, ...]], exported_at: datetime,
    scope: TranscriptScope = TranscriptScope.ACTIVE_PATH,
) -> TranscriptExport:
    active = _active_path(chat, messages)
    active_ids = frozenset(item.id for item in active)
    lines = [
        "# B.O.T.S. Transcript 0.1", "",
        f"- Chat: {_markdown_text(chat.title)}", f"- Created: {utc_timestamp(chat.created_at)}",
        f"- Updated: {utc_timestamp(chat.updated_at)}",
        f"- Archive state: {'archived' if chat.archived_at else 'active'}",
        f"- Exported UTC: {utc_timestamp(exported_at)}", f"- Scope: {scope.value}",
        "", "This is a scrubbed, readable transcript. It is not an import archive.", "",
    ]
    attempts_by_assistant = {item.assistant_message_id: item for item in attempts}
    if any(item.state is AttemptState.RUNNING for item in attempts) or any(
        item.state in {MessageState.SENDING, MessageState.STREAMING} for item in messages
    ):
        lines.extend(["This is a point-in-time view; later generation output may be absent.", ""])
    for message in active:
        _append_transcript_message(lines, message, attempts_by_assistant.get(message.id),
                                   message_attachments.get(message.id, ()),
                                   attempt_attachments.get(attempts_by_assistant[message.id].id, ()) if message.id in attempts_by_assistant else ())
    if scope is TranscriptScope.FULL_LINEAGE:
        lines.extend(["", "# Historical branches and revisions", ""])
        for message in sorted(messages, key=lambda item: (item.sequence, item.id)):
            if message.id in active_ids:
                continue
            lines.append(f"## Historical {message.role.value} — {message.id}")
            _append_transcript_message(
                lines,
                message,
                attempts_by_assistant.get(message.id),
                message_attachments.get(message.id, ()),
                attempt_attachments.get(attempts_by_assistant[message.id].id, ())
                if message.id in attempts_by_assistant
                else (),
            )
    rendered = "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n")
    return TranscriptExport((rendered.rstrip("\n") + "\n").encode("utf-8"), scope)


def _append_transcript_message(lines: list[str], message: Message, attempt: GenerationAttempt | None,
                               attachments: tuple[Attachment | ExportAttachment, ...],
                               attempt_files: tuple[Attachment | ExportAttachment, ...]) -> None:
    lines.extend([
        f"## {message.role.value.title()} — {message.id}",
        f"- Recorded: {utc_timestamp(message.created_at)}", f"- State: {message.state.value}",
        f"- Branch context: parent `{message.parent_id or 'root'}`, lineage `{message.lineage_id}`, revision {message.revision}, supersedes `{message.supersedes_id or 'none'}`", "",
        _fence(message.content), "",
    ])
    if attempt is not None:
        end = utc_timestamp(attempt.ended_at) if attempt.ended_at else "running"
        snapshot = _archive_snapshot(attempt)
        attribution = snapshot.get("attribution")
        if not isinstance(attribution, Mapping):
            attribution = {}
        lines.extend([
            "Generation metadata:",
            f"- Provider/model: {attribution.get('provider_id', '[unavailable]')} / {attribution.get('model', '[unavailable]')}",
            f"- Attempt state: {attempt.state.value}; started: {utc_timestamp(attempt.started_at)}; ended: {end}",
            f"- Finish reason: {safe_finish_reason(attempt.finish_reason, attempt.state) or 'not recorded'}",
            f"- Outcome: {'remote outcome unknown' if attempt.remote_outcome_unknown else message.state.value}",
            f"- Request snapshot: {snapshot['status']}",
        ])
        failure = _failure_projection(attempt)
        if failure is not None:
            lines.append(f"- Error: {failure['category']} — {failure['message']}")
    for label, items in (("Message attachments", attachments), ("Attempt attachments", attempt_files)):
        if items:
            lines.append(f"{label}:")
            lines.extend(_attachment_metadata(item) for item in items)
            for item in items:
                if isinstance(item, ExportAttachment):
                    lines.append(
                        f"  - captured bytes: {item.byte_size}; integrity/availability: {item.integrity_status}"
                    )


def _legacy_snapshot_is_authoritative(
    snapshot: Mapping[str, object], attempt: GenerationAttempt, user_content: str | None,
) -> bool:
    allowed = {"attempt_id", "chat_id", "user_message_id", "backend_id", "model", "prompt"}
    if not set(snapshot) <= allowed:
        return False
    if not {"attempt_id", "chat_id", "user_message_id", "backend_id", "model"} <= set(snapshot):
        return False
    expected = {
        "attempt_id": attempt.id, "chat_id": attempt.chat_id,
        "user_message_id": attempt.user_message_id, "backend_id": attempt.backend_id,
        "model": attempt.model,
    }
    if any(snapshot.get(key) != value for key, value in expected.items() if key in snapshot):
        return False
    prompt = snapshot.get("prompt")
    return "prompt" not in snapshot or (
        type(prompt) is str and (user_content is None or prompt == user_content)
    )


def _safe_capabilities(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    facts = snapshot.get("capabilities")
    if not isinstance(facts, list):
        return []
    result = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            return []
        key, key_status = _safe_closed_metadata(fact.get("key"), _SAFE_CAPABILITY_KEYS)
        # Version validators have already restricted all other values to the
        # landed capability language.  Reasons and field labels are excluded.
        result.append({
            "key": key, "key_status": key_status, "state": fact.get("state"),
            "source": fact.get("source"), "source_revision": fact.get("source_revision"),
            "value": fact.get("value"),
        })
    return sorted(result, key=lambda item: str(item["key"]))


def _safe_context(snapshot: Mapping[str, object]) -> Mapping[str, object] | None:
    plan = snapshot.get("context_plan")
    if not isinstance(plan, Mapping):
        return None
    sources = plan.get("sources")
    budget = plan.get("budget")
    if not isinstance(sources, list) or not isinstance(budget, Mapping):
        return None
    safe_sources = []
    raw_sources: list[Mapping[str, object]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            return None
        raw_sources.append(source)
        source_id, source_id_status = _safe_installation_id(source.get("source_id"))
        kind = source.get("kind")
        role = source.get("role")
        state = source.get("state")
        reason = source.get("reason")
        if kind not in {"history", "bots_instruction", "current_user", "attachment"}:
            return None
        if role not in {"user", "assistant", "system"}:
            return None
        state_value, state_status = _safe_closed_metadata(state, _SAFE_CONTEXT_STATE)
        # The source content is validated request-time domain text.  It is
        # retained as a typed field so the plan can be inspected/reconstructed;
        # prompt duplication and raw canonical JSON remain absent.
        if type(source.get("content")) is not str:
            return None
        controlled_reason = reason if reason in _SAFE_CONTEXT_REASON else "unavailable"
        safe_sources.append({
            "source_id": source_id, "source_id_status": source_id_status,
            "kind": kind, "role": role, "content": source["content"],
            "state": state_value, "state_status": state_status,
            "eligible": source.get("eligible"), "selected": source.get("selected"),
            "selection_reason": controlled_reason,
            "representation_id": source.get("representation_id"),
            "representation_digest": source.get("representation_digest"),
        })
    capability = next((item for item in _safe_capabilities(snapshot)
                       if item["key"] == "limits.context_tokens"), None)
    canonical_sources = [
        {
            "source_id": source["source_id"], "kind": source["kind"], "role": source["role"],
            "content": source["content"], "state": source["state"],
            "eligible": source["eligible"], "selected": source["selected"],
            "reason": source["selection_reason"],
            "representation_id": source["representation_id"],
            "representation_digest": source["representation_digest"],
        }
        for source in safe_sources
    ]
    canonical_projection = {
        "version": plan.get("version"), "sources": canonical_sources,
        "included_sources": plan.get("included_sources"), "excluded_sources": plan.get("excluded_sources"),
        "envelope": {"version": 3, "untrusted_user_context": True},
        "wire_sha256": plan.get("wire_representation_sha256"),
    }
    canonical_projection_text = json.dumps(
        canonical_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    original_matches = (
        all(source.get("reason") in _SAFE_CONTEXT_REASON for source in raw_sources)
        and all(dict(source) == canonical for source, canonical in zip(raw_sources, canonical_sources, strict=True))
        and sha256_hex(canonical_projection_text.encode("utf-8")) == plan.get("canonical_digest")
    )
    return {
        "version": plan.get("version"), "canonical_digest": plan.get("canonical_digest"),
        "wire_representation_sha256": plan.get("wire_representation_sha256"),
        "included_sources": plan.get("included_sources"), "excluded_sources": plan.get("excluded_sources"),
        "sources": safe_sources,
        "canonical_projection": canonical_projection,
        "canonical_projection_digest": sha256_hex(canonical_projection_text.encode("utf-8")),
        "canonical_projection_status": "matches-request-time" if original_matches else "sanitized",
        "budget": {key: budget.get(key) for key in (
            "limit", "semantics", "adapter_id", "adapter_version", "output_reserve",
            "envelope_overhead", "input_units", "total_units", "headroom",
        )},
        "input_counts": plan.get("input_counts"), "parent_id": plan.get("parent_id"),
        "lineage_id": plan.get("lineage_id"), "context_capability": capability,
    }


def _archive_snapshot(attempt: GenerationAttempt, user_content: str | None = None) -> Mapping[str, object]:
    """Build the only request-time provenance language permitted to export."""
    try:
        parsed = strict_json_loads(attempt.request_snapshot)
    except InterchangeError:
        return {"status": "corrupt"}
    if not isinstance(parsed, Mapping):
        return {"status": "corrupt"}
    version = parsed.get("snapshot_version")
    if version is not None and (type(version) is not int or type(version) is bool):
        return {"status": "unsupported"}
    if version not in {None, 2, 3}:
        return {"status": "unsupported"}
    try:
        if version in {2, 3}:
            from bots5.infrastructure.persistence.phase3_validation import validate_request_snapshot
            validated = validate_request_snapshot(
                attempt_id=attempt.id, chat_id=attempt.chat_id,
                user_message_id=attempt.user_message_id, backend_id=attempt.backend_id,
                model=attempt.model, provider_id=attempt.provider_id,
                request_snapshot=attempt.request_snapshot, user_message_content=user_content,
            )
            provider, provider_status = _safe_closed_metadata(
                validated.get("provider_id") or attempt.provider_id or attempt.backend_id,
                _SAFE_PROVIDER_IDS,
            )
            model, model_status = _safe_provider_model_id(validated.get("model") or attempt.model)
            result: dict[str, object] = {
                "status": "available", "snapshot_version": version,
                "attribution": {
                    "backend_id": _safe_closed_metadata(
                        validated.get("backend_id") or attempt.backend_id, _SAFE_BACKEND_TYPES,
                    )[0],
                    "provider_id": provider, "provider_id_status": provider_status,
                    "model": model, "model_status": model_status,
                    "provider_profile": validated.get("provider_profile"),
                    "connection_revision": validated.get("connection_revision"),
                    "catalogue_revision": validated.get("catalogue_revision"),
                },
                "settings": validated.get("effective_settings"),
                "settings_provenance": validated.get("settings_provenance"),
                "capabilities": _safe_capabilities(validated),
                "manual_overrides": validated.get("manual_overrides"),
                "omitted_settings": validated.get("omitted_settings"),
            }
            if version == 3:
                result["settings_revisions"] = validated.get("settings_revisions")
                result["context"] = _safe_context(validated)
            return result
        from bots5.infrastructure.persistence.phase3_validation import is_phase3_record, validate_request_snapshot
        if is_phase3_record(backend_id=attempt.backend_id, provider_id=attempt.provider_id, snapshot=parsed):
            validate_request_snapshot(
                attempt_id=attempt.id, chat_id=attempt.chat_id,
                user_message_id=attempt.user_message_id, backend_id=attempt.backend_id,
                model=attempt.model, provider_id=attempt.provider_id,
                request_snapshot=attempt.request_snapshot, user_message_content=user_content, phase3=True,
            )
        elif not _legacy_snapshot_is_authoritative(parsed, attempt, user_content):
            return {"status": "corrupt"}
    except ValueError:
        return {"status": "corrupt"}
    provider, provider_status = _safe_closed_metadata(
        attempt.provider_id or attempt.backend_id, _SAFE_PROVIDER_IDS,
    )
    model, model_status = _safe_provider_model_id(attempt.model)
    return {
        "status": "legacy-limited", "snapshot_version": "legacy",
        "attribution": {"backend_id": _safe_closed_metadata(attempt.backend_id, _SAFE_BACKEND_TYPES)[0],
                          "provider_id": provider, "provider_id_status": provider_status,
                          "model": model, "model_status": model_status},
    }


def _failure_projection(attempt: GenerationAttempt) -> Mapping[str, str] | None:
    if attempt.error_type is None and attempt.error_message is None:
        return None
    category = {
        AttemptState.FAILED: "generation-failed",
        AttemptState.ABORTED: "generation-aborted",
        AttemptState.INCOMPLETE: "generation-incomplete",
    }.get(attempt.state, "generation-error")
    return {"category": category, "message": SAFE_FAILURE_MESSAGE}


def _archive_decimal(value: Decimal) -> str:
    """Encode a persisted non-negative Decimal in Archive v1 fixed notation."""
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ExportError("attempt known cost is invalid")
    result = "0" if value.is_zero() else format(value, "f")
    if len(result) > 128:
        raise ExportError("attempt known cost cannot be represented by Archive v1")
    return result


def _message_row(message: Message) -> dict[str, object]:
    return {
        "source_id": message.id, "chat_id": message.chat_id, "role": message.role.value,
        "state": message.state.value, "content": message.content, "sequence": message.sequence,
        "created_at": utc_timestamp(message.created_at), "parent_id": message.parent_id,
        "lineage_id": message.lineage_id, "revision": message.revision,
        "supersedes_id": message.supersedes_id,
    }


def _attempt_row(
    attempt: GenerationAttempt, user_content: str | None = None,
    archived_provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    failure = _failure_projection(attempt)
    provenance = (
        _archive_snapshot(attempt, user_content)
        if archived_provenance is None else _json_value(archived_provenance)
    )
    attribution = provenance.get("attribution")
    if not isinstance(attribution, Mapping):
        attribution = {}
    return {
        "source_id": attempt.id, "chat_id": attempt.chat_id,
        "user_message_id": attempt.user_message_id, "assistant_message_id": attempt.assistant_message_id,
        "backend_id": attribution.get("backend_id", "[unavailable]"),
        "provider_id": attribution.get("provider_id"), "model": attribution.get("model"),
        "state": attempt.state.value, "started_at": utc_timestamp(attempt.started_at),
        "ended_at": None if attempt.ended_at is None else utc_timestamp(attempt.ended_at),
        "finish_reason": safe_finish_reason(attempt.finish_reason, attempt.state), "failure": failure,
        "returned_model": _safe_returned_model(attempt.returned_model, attempt.model),
        "prompt_tokens": attempt.prompt_tokens, "completion_tokens": attempt.completion_tokens,
        "reasoning_tokens": attempt.reasoning_tokens, "total_tokens": attempt.total_tokens,
        "known_cost_usd": None if attempt.known_cost_usd is None else _archive_decimal(attempt.known_cost_usd),
        "remote_outcome_unknown": attempt.remote_outcome_unknown,
        "request_time_provenance": provenance,
    }


def _safe_descriptor(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExportError("chat continuation descriptor is malformed")
    result: dict[str, object] = {}
    validators = {
        "source_model_entry_id": _safe_installation_id,
        "source_connection_id": _safe_installation_id,
        "backend_type": lambda item: _safe_closed_metadata(item, _SAFE_BACKEND_TYPES),
        "provider_profile": lambda item: _safe_closed_metadata(item, _SAFE_PROVIDER_PROFILES),
        "provider_model_id": _safe_provider_model_id,
        "display_name": _safe_display_metadata,
        "origin": lambda item: _safe_closed_metadata(item, _SAFE_CATALOGUE_ORIGINS),
        "availability": lambda item: _safe_closed_metadata(item, _SAFE_CATALOGUE_AVAILABILITY),
    }
    for key, validator in validators.items():
        text, status = validator(value.get(key))
        result[key] = text
        result[f"{key}_status"] = status
    for key in ("model_revision", "connection_revision", "catalogue_revision"):
        number = value.get(key)
        minimum = 1 if key in {"model_revision", "connection_revision"} else 0
        if type(number) is not int or number < minimum:
            raise ExportError("chat continuation descriptor revision is malformed")
        result[key] = number
    return result


def _safe_chat_configuration(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or value.get("semantic") != "inert-continuation-hints":
        raise ExportError("chat continuation configuration is malformed")
    selection = value.get("selection")
    if selection is not None and not isinstance(selection, Mapping):
        raise ExportError("chat continuation selection is malformed")
    safe_selection = None
    if selection is not None:
        revision = selection.get("revision")
        required = selection.get("selection_required")
        if type(revision) is not int or revision < 1 or type(required) is not bool:
            raise ExportError("chat continuation selection is malformed")
        model = selection.get("model")
        if (required and model is not None) or (not required and model is None):
            raise ExportError("chat continuation selection is malformed")
        safe_selection = {
            "model": None if model is None else _safe_descriptor(model),
            "selection_required": required, "revision": revision,
        }
    overrides = value.get("overrides")
    if not isinstance(overrides, tuple):
        raise ExportError("chat continuation overrides are malformed")
    safe_overrides = []
    for override in overrides:
        if not isinstance(override, Mapping):
            raise ExportError("chat continuation override is malformed")
        revision = override.get("revision")
        max_output = override.get("max_output_tokens")
        reasoning = override.get("reasoning_effort")
        temperature = override.get("temperature")
        timeout = override.get("timeout_seconds")
        if (type(revision) is not int or revision < 1 or
                (max_output is not None and (type(max_output) is not int or max_output < 1)) or
                (reasoning is not None and (type(reasoning) is not str or reasoning != "none")) or
                (temperature is not None and (
                    type(temperature) not in {int, float} or type(temperature) is bool
                    or not math.isfinite(temperature) or not 0 <= temperature <= 2
                )) or
                (timeout is not None and (
                    type(timeout) not in {int, float} or type(timeout) is bool
                    or not math.isfinite(timeout) or timeout <= 0
                ))):
            raise ExportError("chat continuation override is malformed")
        safe_overrides.append({
            "model": _safe_descriptor(override.get("model")), "revision": revision,
            "temperature": None if temperature is None else float(temperature), "max_output_tokens": max_output,
            "reasoning_effort": reasoning, "timeout_seconds": None if timeout is None else float(timeout),
        })
    _validate_configuration_descriptor_joins(safe_selection, safe_overrides)
    return {"semantic": "inert-continuation-hints", "selection": safe_selection,
            "overrides": sorted(safe_overrides, key=lambda item: str(item["model"]["source_model_entry_id"]))}


def _validate_configuration_descriptor_joins(
    selection: Mapping[str, object] | None, overrides: list[Mapping[str, object]],
) -> None:
    """Keep one portable descriptor per visible model and connection identity."""
    descriptors: list[Mapping[str, object]] = [item["model"] for item in overrides]
    if selection is not None and selection["model"] is not None:
        descriptors.append(selection["model"])
    by_model: dict[str, Mapping[str, object]] = {}
    by_connection: dict[str, tuple[object, ...]] = {}
    connection_fields = (
        "source_connection_id", "source_connection_id_status", "backend_type", "backend_type_status",
        "provider_profile", "provider_profile_status", "connection_revision", "catalogue_revision",
    )
    for descriptor in descriptors:
        model_id = descriptor["source_model_entry_id"]
        if descriptor["source_model_entry_id_status"] == "available":
            existing = by_model.setdefault(model_id, descriptor)
            if existing != descriptor:
                raise ExportError("chat continuation model descriptor contradicts itself")
        connection_id = descriptor["source_connection_id"]
        if descriptor["source_connection_id_status"] == "available":
            facts = tuple(descriptor[key] for key in connection_fields)
            existing_facts = by_connection.setdefault(connection_id, facts)
            if existing_facts != facts:
                raise ExportError("chat continuation connection descriptor contradicts itself")


def _canonical_persisted_timestamp(value: object) -> str:
    if type(value) is not str or not value.endswith("Z") or "\r" in value or value.startswith("\ufeff"):
        raise ExportError("persisted context timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ExportError("persisted context timestamp is malformed") from exc
    canonical = parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed) or canonical != value:
        raise ExportError("persisted context timestamp is malformed")
    return value


def _safe_persisted_context(
    value: Mapping[str, object], attempt: GenerationAttempt, user_content: str,
) -> Mapping[str, object]:
    """Reconstruct a row only after it proves agreement with a valid v3 plan."""
    if not isinstance(value, Mapping):
        raise ExportError("persisted context row is malformed")
    expected_keys = {
        "attempt_id", "plan_version", "canonical_representation", "canonical_digest",
        "wire_representation_digest", "budget_limit", "budget_provenance", "budget_semantics",
        "adapter_id", "adapter_version", "input_counts", "envelope_overhead", "output_reserve",
        "input_units", "total_units", "headroom", "created_at",
    }
    if set(value) != expected_keys:
        raise ExportError("persisted context row has an unknown field")
    try:
        parsed = strict_json_loads(attempt.request_snapshot)
        if not isinstance(parsed, Mapping) or type(parsed.get("snapshot_version")) is not int or parsed.get("snapshot_version") != 3:
            raise ValueError
        from bots5.infrastructure.persistence.phase3_validation import validate_request_snapshot
        snapshot = validate_request_snapshot(
            attempt_id=attempt.id, chat_id=attempt.chat_id,
            user_message_id=attempt.user_message_id, backend_id=attempt.backend_id,
            model=attempt.model, provider_id=attempt.provider_id,
            request_snapshot=attempt.request_snapshot, user_message_content=user_content,
        )
        plan = snapshot["context_plan"]
    except (InterchangeError, ValueError, KeyError) as exc:
        raise ExportError("persisted context row has no valid v3 request plan") from exc
    if not isinstance(plan, Mapping):
        raise ExportError("persisted context row has no valid v3 request plan")
    raw_counts = value["input_counts"]
    try:
        counts = strict_json_loads(raw_counts)
    except InterchangeError as exc:
        raise ExportError("persisted context input counts are malformed") from exc
    if not isinstance(counts, Mapping) or canonical_json_bytes(counts)[:-1] != (
        raw_counts.encode("utf-8") if type(raw_counts) is str else b""
    ):
        raise ExportError("persisted context input counts are not canonical")
    budget = plan.get("budget")
    if not isinstance(budget, Mapping):
        raise ExportError("persisted context row has no valid v3 request plan")
    comparisons = {
        "attempt_id": attempt.id,
        "plan_version": plan.get("version"),
        "canonical_representation": plan.get("canonical_representation"),
        "canonical_digest": plan.get("canonical_digest"),
        "wire_representation_digest": plan.get("wire_representation_sha256"),
        "budget_limit": budget.get("limit"),
        "budget_provenance": budget.get("provenance"),
        "budget_semantics": budget.get("semantics"),
        "adapter_id": budget.get("adapter_id"),
        "adapter_version": budget.get("adapter_version"),
        "input_counts": plan.get("input_counts"),
        "envelope_overhead": budget.get("envelope_overhead"),
        "output_reserve": budget.get("output_reserve"),
        "input_units": budget.get("input_units"),
        "total_units": budget.get("total_units"),
        "headroom": budget.get("headroom"),
    }
    for key, expected in comparisons.items():
        observed = counts if key == "input_counts" else value[key]
        if observed != expected or type(observed) is not type(expected):
            raise ExportError("persisted context row contradicts the request plan")
    created_at = _canonical_persisted_timestamp(value["created_at"])
    # Copy only validated plan facts.  The persisted canonical JSON and
    # free-form provenance are comparison evidence, never export fields.
    return {
        "attempt_id": attempt.id, "plan_version": plan["version"],
        "canonical_digest": plan["canonical_digest"],
        "wire_representation_digest": plan["wire_representation_sha256"],
        "budget_limit": budget["limit"], "budget_semantics": budget["semantics"],
        "adapter_id": budget["adapter_id"], "adapter_version": budget["adapter_version"],
        "input_counts": dict(plan["input_counts"]), "envelope_overhead": budget["envelope_overhead"],
        "output_reserve": budget["output_reserve"], "input_units": budget["input_units"],
        "total_units": budget["total_units"], "headroom": budget["headroom"],
        "created_at": created_at,
    }


def _safe_imported_context(
    value: Mapping[str, object], attempt: GenerationAttempt,
    archived_provenance: Mapping[str, object],
) -> Mapping[str, object]:
    """Project sealed imported v3 context without pretending it is native."""
    if not isinstance(value, Mapping) or not isinstance(archived_provenance, Mapping):
        raise ExportError("imported context evidence is malformed")
    try:
        from bots5.infrastructure.archive_package import _validate_safe_context
        if archived_provenance.get("status") != "available" or archived_provenance.get("snapshot_version") != 3:
            raise ValueError
        safe = _validate_safe_context(_json_value(archived_provenance["context"]))
        if value.get("attempt_id") != attempt.id:
            raise ValueError
        budget = safe["budget"]
        comparisons = {
            "plan_version": safe["version"], "canonical_digest": safe["canonical_digest"],
            "wire_representation_digest": safe["wire_representation_sha256"],
            "budget_limit": budget["limit"], "budget_semantics": budget["semantics"],
            "adapter_id": budget["adapter_id"], "adapter_version": budget["adapter_version"],
            "envelope_overhead": budget["envelope_overhead"], "output_reserve": budget["output_reserve"],
            "input_units": budget["input_units"], "total_units": budget["total_units"],
            "headroom": budget["headroom"],
        }
        if any(value.get(key) != expected or type(value.get(key)) is not type(expected) for key, expected in comparisons.items()):
            raise ValueError
        created_at = _canonical_persisted_timestamp(value["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportError("imported context row contradicts sealed request-time evidence") from exc
    return {
        "attempt_id": attempt.id, "plan_version": safe["version"],
        "canonical_digest": safe["canonical_digest"],
        "wire_representation_digest": safe["wire_representation_sha256"],
        "budget_limit": budget["limit"], "budget_semantics": budget["semantics"],
        "adapter_id": budget["adapter_id"], "adapter_version": budget["adapter_version"],
        "input_counts": dict(safe["input_counts"]), "envelope_overhead": budget["envelope_overhead"],
        "output_reserve": budget["output_reserve"], "input_units": budget["input_units"],
        "total_units": budget["total_units"], "headroom": budget["headroom"],
        "created_at": created_at,
    }


def build_archive_projection(
    *, archive_id: str, created_at: datetime, chat: Chat, messages: tuple[Message, ...],
    attempts: tuple[GenerationAttempt, ...], message_attachments: Mapping[str, tuple[Attachment | ExportAttachment, ...]],
    attempt_attachments: Mapping[str, tuple[Attachment | ExportAttachment, ...]], payloads: Mapping[str, bytes],
    attachment_policy: AttachmentPolicy, chat_configuration: Mapping[str, object],
    application_version: str, migration_revision: str,
    context_plans: Mapping[str, Mapping[str, object]] | None = None,
    archived_attempt_provenance: Mapping[str, Mapping[str, object]] | None = None,
    allow_missing_external: bool = False,
) -> ArchiveProjection:
    if any(item.state is AttemptState.RUNNING for item in attempts) or any(
        item.state in {MessageState.SENDING, MessageState.STREAMING} for item in messages
    ):
        raise ExportError("Archive v1 refuses chats with a running generation")
    active = _active_path(chat, messages)
    by_id = {item.id: item for item in messages}
    if len(by_id) != len(messages) or any(item.chat_id != chat.id for item in messages):
        raise ExportError("message graph has incoherent chat ownership")
    for item in messages:
        if item.parent_id is not None and item.parent_id not in by_id:
            raise ExportError("message parent is absent from archive chat")
        if item.supersedes_id is not None and item.supersedes_id not in by_id:
            raise ExportError("message supersession is absent from archive chat")
    if chat.head_message_id is not None and chat.head_message_id not in by_id:
        raise ExportError("chat head is absent from archive chat")
    for attempt in attempts:
        if attempt.chat_id != chat.id or attempt.user_message_id not in by_id or attempt.assistant_message_id not in by_id:
            raise ExportError("attempt references are not coherent with archive chat")
    attachments: dict[str, Attachment | ExportAttachment] = {}
    message_links: list[dict[str, object]] = []
    attempt_links: list[dict[str, object]] = []
    for message_id, values in message_attachments.items():
        for ordinal, value in enumerate(values):
            attachment = _export_attachment(value)
            attachments[attachment.id] = value
            message_links.append({"message_id": message_id, "attachment_id": attachment.id, "ordinal": ordinal})
    for attempt_id, values in attempt_attachments.items():
        for ordinal, value in enumerate(values):
            attachment = _export_attachment(value)
            attachments[attachment.id] = value
            attempt_links.append({"attempt_id": attempt_id, "attachment_id": attachment.id, "ordinal": ordinal})
    attachment_rows = [
        _attachment_row(value)
        for value in sorted(attachments.values(), key=lambda item: _export_attachment(item).id)
    ]
    external: list[dict[str, object]] = []
    digests = sorted({_export_attachment(value).blob_digest for value in attachments.values()})
    payload_entries: list[ArchiveLogicalEntry] = []
    if attachment_policy is AttachmentPolicy.EMBEDDED:
        for digest in digests:
            raw = payloads.get(digest)
            if raw is None or sha256_hex(raw) != digest:
                raise ExportError("authoritative attachment payload is unavailable or mismatched")
            payload_entries.append(ArchiveLogicalEntry(f"payloads/sha256/{digest}", "application/octet-stream", True, raw))
    else:
        for digest in digests:
            item = next(value for value in attachments.values() if _export_attachment(value).blob_digest == digest)
            size = item.byte_size if isinstance(item, ExportAttachment) else len(payloads.get(digest, b""))
            if (
                isinstance(item, ExportAttachment)
                and item.integrity_status != "verified"
                and not (allow_missing_external and item.integrity_status == "missing-external")
            ):
                raise ExportError("authoritative attachment payload is unavailable or mismatched")
            external.append({"digest": digest, "size": size, "logical_resource_id": f"sha256:{digest}", "required": True})
    provenance = {
        "source_origin": "native", "import_origin": "not-recorded",
        "attachment_policy": attachment_policy.value,
        "resources": [{"digest": item["digest"], "size": item["size"], "required": True} for item in external],
    }
    supplied_contexts = context_plans or {}
    imported_provenance = archived_attempt_provenance or {}
    if not set(supplied_contexts) <= {item.id for item in attempts}:
        raise ExportError("persisted context row references an unknown attempt")
    if not set(imported_provenance) <= {item.id for item in attempts}:
        raise ExportError("archived attempt provenance references an unknown attempt")
    context_rows = []
    for attempt in attempts:
        try:
            snapshot = strict_json_loads(attempt.request_snapshot)
        except InterchangeError:
            snapshot = None
        version = snapshot.get("snapshot_version") if isinstance(snapshot, Mapping) else None
        row = supplied_contexts.get(attempt.id)
        archived = imported_provenance.get(attempt.id)
        if isinstance(archived, Mapping) and archived.get("status") == "available" and archived.get("snapshot_version") == 3:
            if row is None:
                raise ExportError("sealed imported v3 request plan lacks context evidence")
            context_rows.append(_safe_imported_context(row, attempt, archived))
        elif type(version) is int and version == 3:
            if row is None:
                raise ExportError("valid v3 request plan lacks persisted context evidence")
            context_rows.append(_safe_persisted_context(row, attempt, by_id[attempt.user_message_id].content))
        elif row is not None:
            raise ExportError("persisted context row does not belong to a v3 request plan")
    entries = [
        ArchiveLogicalEntry("domain/chat.json", "application/json", True, canonical_json_bytes({
            "source_id": chat.id, "title": chat.title, "created_at": utc_timestamp(chat.created_at),
            "updated_at": utc_timestamp(chat.updated_at), "archived_at": None if chat.archived_at is None else utc_timestamp(chat.archived_at),
            "head_message_id": chat.head_message_id, "revision": chat.revision,
        })),
        ArchiveLogicalEntry("domain/messages.jsonl", "application/x-ndjson", True, canonical_jsonl_bytes(_message_row(item) for item in sorted(messages, key=lambda item: (item.sequence, item.id)))),
        ArchiveLogicalEntry("domain/attempts.jsonl", "application/x-ndjson", True, canonical_jsonl_bytes(
            _attempt_row(item, by_id[item.user_message_id].content, imported_provenance.get(item.id))
            for item in sorted(attempts, key=lambda item: (utc_timestamp(item.started_at), item.id))
        )),
        ArchiveLogicalEntry("domain/context-plans.jsonl", "application/x-ndjson", True, canonical_jsonl_bytes(
            _json_value(item) for item in sorted(context_rows, key=lambda item: str(item["attempt_id"]))
        )),
        ArchiveLogicalEntry("domain/attachments.jsonl", "application/x-ndjson", True, canonical_jsonl_bytes(attachment_rows)),
        ArchiveLogicalEntry("domain/message-attachments.jsonl", "application/x-ndjson", True, canonical_jsonl_bytes(sorted(message_links, key=lambda item: (str(item["message_id"]), int(item["ordinal"]), str(item["attachment_id"]))))),
        ArchiveLogicalEntry("domain/attempt-attachments.jsonl", "application/x-ndjson", True, canonical_jsonl_bytes(sorted(attempt_links, key=lambda item: (str(item["attempt_id"]), int(item["ordinal"]), str(item["attachment_id"]))))),
        ArchiveLogicalEntry("domain/chat-configuration.json", "application/json", True, canonical_json_bytes(_json_value(_safe_chat_configuration(chat_configuration)))),
        ArchiveLogicalEntry("domain/provenance.json", "application/json", True, canonical_json_bytes(provenance)),
    ]
    entries.extend(payload_entries)
    return ArchiveProjection(
        archive_id=archive_id, created_at=created_at, chat_id=chat.id, chat_title=chat.title,
        attachment_policy=attachment_policy, entries=tuple(entries),
        manifest_base={"format": "org.necromilias.bots5.chat-archive", "archive_version": 1,
            "archive_id": archive_id, "created_at": utc_timestamp(created_at),
            "source_application_version": application_version, "source_db_migration_revision": migration_revision,
            "source_chat": {"source_id": chat.id, "title": chat.title},
            "attachment_policy": attachment_policy.value, "self_contained": attachment_policy is AttachmentPolicy.EMBEDDED,
            "features": ["chat-lineage", "generation-outcomes", "request-time-provenance", "context-plans", "attachments"],
            "external_resources": external, "secret_exclusion": "credentials, endpoints, request identifiers, host paths, and raw secret-shaped fields are excluded"},
    )


def build_archive_v2_projection(
    *, archive_id: str, created_at: datetime, chat: Chat, messages: tuple[Message, ...],
    attempts: tuple[GenerationAttempt, ...], message_attachments: Mapping[str, tuple[Attachment | ExportAttachment, ...]],
    attempt_attachments: Mapping[str, tuple[Attachment | ExportAttachment, ...]], payloads: Mapping[str, bytes],
    attachment_policy: AttachmentPolicy, chat_configuration: Mapping[str, object],
    application_version: str, migration_revision: str,
    context_plans: Mapping[str, Mapping[str, object]] | None = None,
    object_provenance: tuple[Mapping[str, object], ...] | None = None,
    continuation_history: Mapping[str, object] | None = None,
    history_bindings: tuple[Mapping[str, object], ...] = (),
    archived_attempt_provenance: Mapping[str, Mapping[str, object]] | None = None,
) -> ArchiveProjection:
    """Build deterministic v2 bytes with complete identity provenance.

    V2 preserves the validated v1 domain graph verbatim and appends a closed
    provenance inventory.  Imported callers pass their persisted per-object
    history; native callers receive a complete native inventory.
    """
    v1 = build_archive_projection(
        archive_id=archive_id, created_at=created_at, chat=chat, messages=messages,
        attempts=attempts, message_attachments=message_attachments,
        attempt_attachments=attempt_attachments, payloads=payloads,
        attachment_policy=attachment_policy, chat_configuration=chat_configuration,
        application_version=application_version, migration_revision=migration_revision,
        context_plans=context_plans,
        archived_attempt_provenance=archived_attempt_provenance,
        # Archive v1 remains frozen: only the v2 writer can carry an honest
        # payload-absence conclusion for an externally referenced object.
        allow_missing_external=True,
    )
    entries = {item.path: item.content for item in v1.entries}
    # V2 makes the payload-presence conclusion explicit.  V1's attachment
    # row is deliberately frozen, so add this field only on the evolved wire.
    # Every attachment currently admitted to the v1 projection has a verified
    # backing; unavailable imported references are projected separately by
    # the store and must never be recast as verified here.
    try:
        v2_attachments = [json.loads(line) for line in entries["domain/attachments.jsonl"].splitlines() if line]
    except (KeyError, ValueError) as exc:
        raise ExportError("v1 attachment projection is malformed") from exc
    if any(not isinstance(item, dict) for item in v2_attachments):
        raise ExportError("v1 attachment projection is malformed")
    for item in v2_attachments:
        if item.get("integrity_status") == "verified":
            item["payload_availability"] = "verified"
        elif item.get("integrity_status") == "missing-external":
            item["integrity_status"] = "not-present"
            item["payload_availability"] = "missing-external"
        else:
            raise ExportError("v2 export requires verified attachment backing")
    entries["domain/attachments.jsonl"] = canonical_jsonl_bytes(v2_attachments)
    attachment_ids = {
        _export_attachment(value).id
        for values in (*message_attachments.values(), *attempt_attachments.values())
        for value in values
    }
    if object_provenance is None:
        identities = [("chat", chat.id)]
        identities.extend(("message", item.id) for item in messages)
        identities.extend(("lineage", item.lineage_id) for item in messages)
        identities.extend(("attachment", item) for item in attachment_ids)
        identities.extend(("attempt", item.id) for item in attempts)
        object_provenance = tuple(
            {"object_kind": kind, "object_id": identity,
             "source": {"kind": "native", "immediate": None, "prior_chain": []},
             "derivation": {"kind": "root", "predecessor": None}}
            for kind, identity in dict.fromkeys(identities)
        )
    source_kinds = {
        str(row.get("source", {}).get("kind"))
        for row in object_provenance if isinstance(row, Mapping)
    }
    if not source_kinds <= {"native", "v1-bootstrap", "imported"} or not source_kinds:
        raise ExportError("v2 object provenance is malformed")
    entries["domain/object-provenance.jsonl"] = canonical_jsonl_bytes(
        _json_value(item) for item in object_provenance
    )
    entries["domain/continuation-history.json"] = canonical_json_bytes(_json_value(
        continuation_history if continuation_history is not None else {
            "active_head_message_id": chat.head_message_id,
            "anchors": [], "choices": [], "branches": [],
        }
    ))
    entries["domain/history-bindings.jsonl"] = canonical_jsonl_bytes(
        _json_value(item) for item in history_bindings
    )
    existing_provenance = strict_json_loads(entries["domain/provenance.json"])
    entries["domain/provenance.json"] = canonical_json_bytes({
        "source_origin": "native" if source_kinds == {"native"} else "imported",
        "import_origin": "not-recorded" if source_kinds == {"native"} else "recorded",
        "attachment_policy": attachment_policy.value,
        "resources": existing_provenance["resources"],
    })
    manifest = dict(v1.manifest_base)
    manifest["archive_version"] = 2
    manifest["features"] = [
        "attachments", "chat-lineage", "context-plans", "continuation-history-v1",
        "generation-outcomes", "history-bindings-v1", "import-provenance-v1",
        "request-time-provenance",
    ]
    return ArchiveProjection(
        archive_id=archive_id, created_at=created_at, chat_id=chat.id, chat_title=chat.title,
        attachment_policy=attachment_policy,
        entries=tuple(
            ArchiveLogicalEntry(
                path, "application/octet-stream" if path.startswith("payloads/") else (
                    "application/x-ndjson" if path.endswith(".jsonl") else "application/json"
                ), True, content,
            )
            for path, content in sorted(entries.items())
        ),
        manifest_base=manifest,
    )
