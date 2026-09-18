from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bots5.core.export import TranscriptScope, _archive_snapshot, _attachment_metadata, _attachment_row, _attempt_row, _inline_code, _safe_attachment_filename, _safe_descriptor, build_transcript, metadata_status_matches, safe_finish_reason
from bots5.core.interchange import InterchangeError, canonical_json_bytes, canonical_jsonl_bytes, logical_content_digest, parse_jsonl, strict_json_loads, utc_timestamp
from bots5.domain.models import AttemptState, Attachment, Chat, GenerationAttempt, Message, MessageRole, MessageState


NOW = datetime(2026, 9, 15, 1, 2, 3, 4000, tzinfo=UTC)


def test_canonical_json_jsonl_and_digest_are_closed_and_deterministic():
    assert canonical_json_bytes({"z": "é", "a": 1}) == b'{"a":1,"z":"\xc3\xa9"}\n'
    assert canonical_jsonl_bytes(({"b": 2}, {"a": 1})) == b'{"b":2}\n{"a":1}\n'
    assert logical_content_digest((("b", 1, "b" * 64), ("a", 0, "a" * 64))) == logical_content_digest((("a", 0, "a" * 64), ("b", 1, "b" * 64)))
    assert utc_timestamp(NOW) == "2026-09-15T01:02:03.004000Z"


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_strict_json_rejects_duplicate_and_nonfinite_values(raw):
    with pytest.raises(InterchangeError):
        strict_json_loads(raw)


@pytest.mark.parametrize("raw", [
    b'\xef\xbb\xbf{"a":1}\n', b'{"a":1}\r\n', b'{"a":1}\n\n', b'{"a": 1}\n',
])
def test_canonical_jsonl_rejects_bom_cr_blank_lines_and_noncanonical_input(raw):
    with pytest.raises(InterchangeError):
        parse_jsonl(raw)


def test_request_snapshot_statuses_are_closed_and_do_not_return_raw_bytes():
    base = dict(
        id="attempt", chat_id="chat", user_message_id="user", assistant_message_id="assistant",
        backend_id="fake", model="model", state=AttemptState.COMPLETE, started_at=NOW,
    )
    legacy = GenerationAttempt(
        request_snapshot='{"attempt_id":"attempt","backend_id":"fake","chat_id":"chat","model":"model","prompt":"prompt","user_message_id":"user"}',
        **base,
    )
    assert _archive_snapshot(legacy, "prompt")["status"] == "legacy-limited"
    corrupt = GenerationAttempt(request_snapshot="{}", **base)
    assert _archive_snapshot(corrupt, "prompt") == {"status": "corrupt"}
    unsupported = GenerationAttempt(request_snapshot='{"snapshot_version":4,"endpoint":"https://sentinel.invalid"}', **base)
    assert _archive_snapshot(unsupported, "prompt") == {"status": "unsupported"}


def test_transcript_uses_head_path_and_code_fences_raw_html():
    chat = Chat("chat", "<title>", NOW, NOW, head_message_id="a2")
    root = Message("u1", "chat", MessageRole.USER, MessageState.SENT, "<b>unsafe</b>", 1, NOW)
    sibling = Message("a1", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "historical", 2, NOW, parent_id="u1")
    active = Message("a2", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "active", 3, NOW, parent_id="u1")
    active_export = build_transcript(chat=chat, messages=(root, sibling, active), attempts=(), message_attachments={}, attempt_attachments={}, exported_at=NOW)
    assert b"historical" not in active_export.content
    assert b"```text\n<b>unsafe</b>\n```" in active_export.content
    full = build_transcript(chat=chat, messages=(root, sibling, active), attempts=(), message_attachments={}, attempt_attachments={}, exported_at=NOW, scope=TranscriptScope.FULL_LINEAGE)
    assert b"Historical branches and revisions" in full.content and b"historical" in full.content


def test_transcript_normalizes_line_endings_and_preserves_unicode_domain_text():
    chat = Chat("chat", "title\rwith newline", NOW, NOW, head_message_id="user")
    message = Message("user", "chat", MessageRole.USER, MessageState.SENT, "alpha\r\n\u03b2eta\rgamma", 1, NOW)
    exported = build_transcript(
        chat=chat, messages=(message,), attempts=(), message_attachments={}, attempt_attachments={}, exported_at=NOW,
    )
    assert b"\r" not in exported.content
    assert "alpha\n\u03b2eta\ngamma".encode() in exported.content


def test_transcript_uses_the_same_safe_attachment_filename_projection_as_archive():
    attachment = Attachment(
        "attachment", "a" * 64, "r\u00e9sum\u00e9-input.txt", "filesystem", "not-exported", None, None, None, NOW,
    )
    assert "`r\u00e9sum\u00e9-input.txt`" in _attachment_metadata(attachment)
    assert _attachment_row(attachment)["filename"] == "r\u00e9sum\u00e9-input.txt"
    assert _attachment_row(attachment)["filename_status"] == "available"
    malformed = Attachment(
        "attachment", "a" * 64, "not-a-basename/name", "filesystem", "not-exported", None, None, None, NOW,
    )
    assert "`[redacted]`" in _attachment_metadata(malformed)
    assert _attachment_row(malformed)["filename"] == "[redacted]"
    assert _attachment_row(malformed)["filename_status"] == "redacted"


def test_transcript_metadata_is_readable_text_without_markdown_structure_injection():
    chat = Chat(
        "chat", "ordinary\r\n# injected [link](https://example.invalid) ![image](x) <tag>",
        NOW, NOW, head_message_id="user",
    )
    message = Message("user", "chat", MessageRole.USER, MessageState.SENT, "body", 1, NOW)
    exported = build_transcript(
        chat=chat, messages=(message,), attempts=(), message_attachments={}, attempt_attachments={}, exported_at=NOW,
    )
    assert b"\r" not in exported.content
    assert sum(line.startswith(b"# ") for line in exported.content.splitlines()) == 1
    assert b"\n# injected" not in exported.content
    assert b"[link](https://example.invalid)" not in exported.content
    assert b"![image](x)" not in exported.content
    assert b"<tag>" not in exported.content
    assert b"\\[link\\]\\(https://example\\.invalid\\)" in exported.content

    attachment = Attachment(
        "attachment", "a" * 64, "literal`` [link](x) <tag>.txt", "filesystem", "not-exported",
        None, None, None, NOW,
    )
    metadata = _attachment_metadata(attachment)
    assert metadata.startswith("- ```literal`` [link](x) <tag>.txt```")
    assert metadata.count("```") == 2
    assert "\n" not in metadata
    assert _inline_code("`edge`.txt") == "`` `edge`.txt ``"
    assert _inline_code(" leading.txt ") == "`  leading.txt  `"


def test_metadata_status_rule_accepts_fixed_redaction_without_reprojecting_it():
    assert metadata_status_matches("[redacted]", "redacted", _safe_attachment_filename)
    assert metadata_status_matches("[redacted]", "available", _safe_attachment_filename)
    assert not metadata_status_matches("ordinary.txt", "redacted", _safe_attachment_filename)
    assert not metadata_status_matches("not-a-basename/name", "available", _safe_attachment_filename)


def test_field_specific_metadata_projection_preserves_models_and_redacts_unsafe_provider_text():
    descriptor = {
        "source_model_entry_id": "historical-model-42",
        "source_connection_id": "historical-connection-7",
        "backend_type": "fake",
        "provider_profile": "generic",
        "provider_model_id": "vendor-a/ordinary-model",
        "display_name": "Ordinary Model",
        "origin": "manual",
        "availability": "available",
        "model_revision": 1,
        "connection_revision": 1,
        "catalogue_revision": 0,
    }
    exported = _safe_descriptor(descriptor)
    assert exported["source_model_entry_id"] == "historical-model-42"
    assert exported["provider_model_id"] == "vendor-a/ordinary-model"
    assert exported["provider_model_id_status"] == "available"
    assert exported["display_name"] == "Ordinary Model"
    unsafe = _safe_descriptor({
        **descriptor,
        "provider_model_id": "metadata.invalid/opaque-value",
        "display_name": "CREDENTIAL_REFERENCE_SENTINEL",
    })
    assert unsafe["provider_model_id"] == "[redacted]"
    assert unsafe["provider_model_id_status"] == "redacted"
    assert unsafe["display_name"] == "[redacted]"
    local_endpoint = _safe_descriptor({
        **descriptor,
        "provider_model_id": "localhost:18400/model",
        "display_name": "127.0.0.1:18400",
    })
    assert local_endpoint["provider_model_id"] == "[redacted]"
    assert local_endpoint["display_name"] == "[redacted]"
    sensitive_model = _safe_descriptor({**descriptor, "provider_model_id": "CREDENTIAL_LABEL/model"})
    assert sensitive_model["provider_model_id"] == "[redacted]"
    token_prefix = _safe_descriptor({
        **descriptor,
        "provider_model_id": "sk-test-sentinel",
        "display_name": "pk_test_sentinel",
    })
    assert token_prefix["provider_model_id"] == "[redacted]"
    assert token_prefix["display_name"] == "[redacted]"
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "vendor-a/ordinary-model",
        AttemptState.COMPLETE,
        '{"attempt_id":"attempt","backend_id":"fake","chat_id":"chat","model":"vendor-a/ordinary-model","prompt":"prompt","user_message_id":"user"}',
        NOW, ended_at=NOW, returned_model="OPAQUE_REQUEST_IDENTIFIER_SENTINEL",
    )
    row = _attempt_row(attempt, "prompt")
    assert row["returned_model"] == "[redacted]"
    assert "OPAQUE_REQUEST_IDENTIFIER_SENTINEL" not in str(row)


def test_finish_reason_projection_keeps_known_outcomes_and_closes_unknown_provider_text():
    assert safe_finish_reason("stop", AttemptState.COMPLETE) == "stop"
    assert safe_finish_reason("length", AttemptState.INCOMPLETE) == "length"
    assert safe_finish_reason("unrecognized-provider-outcome", AttemptState.INCOMPLETE) == "non-stop"
    assert safe_finish_reason("provider-error", AttemptState.FAILED) is None
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "model", AttemptState.INCOMPLETE,
        "{}", NOW, ended_at=NOW, finish_reason="unrecognized-provider-outcome",
    )
    row = _attempt_row(attempt)
    assert row["finish_reason"] == "non-stop"
    assert "unrecognized-provider-outcome" not in str(row)
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="assistant")
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "prompt", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.TRUNCATED, "partial", 2, NOW, parent_id="user")
    transcript = build_transcript(
        chat=chat, messages=(user, assistant), attempts=(attempt,),
        message_attachments={}, attempt_attachments={}, exported_at=NOW,
    )
    assert b"Finish reason: non-stop" in transcript.content
    assert b"unrecognized-provider-outcome" not in transcript.content


def test_full_lineage_uses_the_same_attempt_renderer_for_historical_branches():
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="active")
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "prompt", 1, NOW)
    historical = Message("old", "chat", MessageRole.ASSISTANT, MessageState.FAILED, "partial", 2, NOW, parent_id="user")
    active = Message("active", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "done", 3, NOW, parent_id="user")
    attempt = GenerationAttempt(
        "attempt-old", "chat", "user", "old", "fake", "model", AttemptState.FAILED,
        "{}", NOW, ended_at=NOW, error_type="failure", error_message="ordinary failure",
        finish_reason="error",
    )
    exported = build_transcript(
        chat=chat, messages=(user, historical, active), attempts=(attempt,),
        message_attachments={}, attempt_attachments={}, exported_at=NOW,
        scope=TranscriptScope.FULL_LINEAGE,
    )
    assert b"Historical assistant" in exported.content
    assert b"Attempt state: failed" in exported.content
    assert b"generation-failed" in exported.content
    assert b"B.O.T.S. recorded a generation failure." in exported.content
    assert b"ordinary failure" not in exported.content
