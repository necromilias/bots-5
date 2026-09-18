from __future__ import annotations

import io
import json
import math
import os
import stat
import zipfile
import asyncio
import threading
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event

from bots5.core.export import AttachmentPolicy, ExportAttachment, ExportError, TranscriptScope, _safe_chat_configuration, build_archive_projection
from bots5.core.generation import GenerationCompleted, GenerationDelta
from bots5.core.interchange import canonical_json_bytes, canonical_jsonl_bytes, logical_content_digest, sha256_hex
from bots5.infrastructure import archive_package
from bots5.infrastructure.archive_package import ArchivePackageError, archive_bytes, validate_archive, write_archive, write_transcript
from bots5.domain.models import AttemptState, Attachment, Chat, GenerationAttempt, Message, MessageRole, MessageState
from bots5.domain.provider import BackendType, GenerationSettings, ProviderProfile
from tests.test_phase6_context_attachments import _configured_application as phase6_application, _finish
from tests.test_phase3_generation import _persist_fixture as phase3_persist_fixture, _store_attempt_fixture as phase3_store_attempt_fixture


NOW = datetime(2026, 9, 15, tzinfo=UTC)


_EMPTY_CONFIGURATION = {
    "semantic": "inert-continuation-hints", "selection": None, "overrides": (),
}


def _projection(*, archive_id="archive", created_at=NOW, attachment_policy=AttachmentPolicy.EMBEDDED):
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="assistant", revision=1)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "done", 2, NOW, parent_id="user")
    attempt = GenerationAttempt("attempt", "chat", "user", "assistant", "fake", "model", AttemptState.COMPLETE, "{}", NOW, ended_at=NOW, finish_reason="stop")
    return build_archive_projection(
        archive_id=archive_id, created_at=created_at, chat=chat, messages=(user, assistant), attempts=(attempt,),
        message_attachments={}, attempt_attachments={}, payloads={},
        attachment_policy=attachment_policy, chat_configuration=_EMPTY_CONFIGURATION,
        application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )


def test_archive_layout_is_valid_and_completed():
    raw = archive_bytes(_projection())
    result = validate_archive(io.BytesIO(raw))
    assert result.archive_id == "archive"
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        assert package.namelist()[-1] == "COMPLETED"
        assert set(package.namelist()) >= {"manifest.json", "domain/chat.json", "domain/attempts.jsonl", "COMPLETED"}


def test_archive_round_trips_independent_reasoning_token_telemetry():
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="assistant", revision=1)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "done", 2, NOW, parent_id="user")
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "model", AttemptState.COMPLETE, "{}", NOW,
        ended_at=NOW, finish_reason="stop", prompt_tokens=3, completion_tokens=2,
        reasoning_tokens=3, total_tokens=5,
    )
    projection = build_archive_projection(
        archive_id="reasoning-telemetry", created_at=NOW, chat=chat, messages=(user, assistant), attempts=(attempt,),
        message_attachments={}, attempt_attachments={}, payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
        chat_configuration=_EMPTY_CONFIGURATION, application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    raw = archive_bytes(projection)
    assert validate_archive(io.BytesIO(raw)).archive_id == "reasoning-telemetry"
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        row = json.loads(package.read("domain/attempts.jsonl"))
    assert tuple(row[key] for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")) == (3, 2, 3, 5)


def _manifest(raw: bytes):
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        return json.loads(package.read("manifest.json"))


def _repack_canonical_archive(raw: bytes, mutate) -> bytes:
    """Make a bounded semantic mutant with fresh declared hashes.

    This deliberately does not reuse the production writer or validator after
    mutation, so every malformed archive reaches the reader-side rule being
    tested instead of failing at a stale inventory hash.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        names = package.namelist()
        entries = {name: package.read(name) for name in names}
    manifest = json.loads(entries["manifest.json"])
    mutate(entries, manifest)
    extras = sorted(set(entries) - set(names) - {"manifest.json"})
    if extras:
        names = [name for name in names if name != "COMPLETED"] + extras + ["COMPLETED"]
    inventory_attributes = {
        row["path"]: (row["media_type"], row["required"])
        for row in manifest["entry_inventory"]
    }
    inventory = []
    for name in names:
        if name == "manifest.json":
            continue
        content = entries[name]
        inventory.append({
            "path": name, "media_type": inventory_attributes[name][0], "required": inventory_attributes[name][1],
            "uncompressed_size": len(content), "sha256": sha256_hex(content),
        })
    inventory.sort(key=lambda row: row["path"])
    manifest["entry_inventory"] = inventory
    manifest["logical_content_digest"] = logical_content_digest(
        (row["path"], row["uncompressed_size"], row["sha256"]) for row in inventory
    )
    entries["manifest.json"] = canonical_json_bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as package:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100600) << 16
            info.create_system = 3
            package.writestr(info, entries[name])
    return output.getvalue()


def _attachment_projection(*, policy=AttachmentPolicy.EMBEDDED, payload=b"x", filename="note.txt"):
    digest = sha256_hex(payload)
    attachment = ExportAttachment(
        Attachment("attachment", digest, filename, "file", "/not/exported", None, None, "not_text", NOW),
        byte_size=len(payload), integrity_status="verified", payload=payload,
    )
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="assistant", revision=1)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "done", 2, NOW, parent_id="user")
    attempt = GenerationAttempt("attempt", "chat", "user", "assistant", "fake", "model", AttemptState.COMPLETE, "{}", NOW, ended_at=NOW, finish_reason="stop")
    return build_archive_projection(
        archive_id="attachment-archive", created_at=NOW, chat=chat, messages=(user, assistant), attempts=(attempt,),
        message_attachments={"user": (attachment,)}, attempt_attachments={}, payloads={digest: payload},
        attachment_policy=policy, chat_configuration=_EMPTY_CONFIGURATION,
        application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )


def test_archive_preserves_safe_dotted_unicode_attachment_basename():
    raw = archive_bytes(_attachment_projection(filename="r\u00e9sum\u00e9-input.txt"))
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        row = json.loads(package.read("domain/attachments.jsonl"))
    assert row["filename"] == "r\u00e9sum\u00e9-input.txt"
    assert row["filename_status"] == "available"


def test_reader_binds_attachment_metadata_status_to_the_writer_projection():
    raw = archive_bytes(_attachment_projection())

    def mismatched_filename(entries, manifest):
        rows = [json.loads(line) for line in entries["domain/attachments.jsonl"].splitlines()]
        rows[0]["filename"] = "not-a-basename/name"
        rows[0]["filename_status"] = "available"
        entries["domain/attachments.jsonl"] = canonical_jsonl_bytes(rows)

    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(_repack_canonical_archive(raw, mismatched_filename)))


def test_reader_accepts_writer_redacted_metadata_and_corrupt_provenance_returned_model():
    redacted_filename = archive_bytes(_attachment_projection(filename="not-a-basename/name"))
    assert validate_archive(io.BytesIO(redacted_filename)).archive_id == "attachment-archive"
    with zipfile.ZipFile(io.BytesIO(redacted_filename)) as package:
        attachment = json.loads(package.read("domain/attachments.jsonl"))
    assert (attachment["filename"], attachment["filename_status"]) == ("[redacted]", "redacted")

    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="assistant", revision=1)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "done", 2, NOW, parent_id="user")
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "unmapped_backend", "safe-model", AttemptState.COMPLETE,
        "{\"attempt_id\":\"attempt\",\"backend_id\":\"unmapped_backend\",\"chat_id\":\"chat\",\"model\":\"safe-model\",\"prompt\":\"hello\",\"user_message_id\":\"user\"}",
        NOW, ended_at=NOW, finish_reason="stop", returned_model="safe-model",
    )
    projection = build_archive_projection(
        archive_id="legacy-redaction", created_at=NOW, chat=chat, messages=(user, assistant), attempts=(attempt,),
        message_attachments={}, attempt_attachments={}, payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
        chat_configuration=_EMPTY_CONFIGURATION, application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    assert validate_archive(io.BytesIO(archive_bytes(projection))).archive_id == "legacy-redaction"

    corrupt = replace(attempt, backend_id="fake", request_snapshot="{}")
    corrupt_projection = build_archive_projection(
        archive_id="corrupt-returned-model", created_at=NOW, chat=chat, messages=(user, assistant), attempts=(corrupt,),
        message_attachments={}, attempt_attachments={}, payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
        chat_configuration=_EMPTY_CONFIGURATION, application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    assert validate_archive(io.BytesIO(archive_bytes(corrupt_projection))).archive_id == "corrupt-returned-model"


def test_finish_reason_projection_preserves_length_and_rejects_raw_unknown_outcomes():
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="assistant", revision=1)
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.TRUNCATED, "partial", 2, NOW, parent_id="user")
    attempt = GenerationAttempt(
        "attempt", "chat", "user", "assistant", "fake", "model", AttemptState.INCOMPLETE,
        "{}", NOW, ended_at=NOW, finish_reason="unrecognized-provider-outcome",
        error_type="provider", error_message="provider outcome",
    )
    projection = build_archive_projection(
        archive_id="finish-reason", created_at=NOW, chat=chat, messages=(user, assistant), attempts=(attempt,),
        message_attachments={}, attempt_attachments={}, payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
        chat_configuration=_EMPTY_CONFIGURATION, application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    raw = archive_bytes(projection)
    assert validate_archive(io.BytesIO(raw)).archive_id == "finish-reason"
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        row = json.loads(package.read("domain/attempts.jsonl"))
    assert row["finish_reason"] == "non-stop"

    def raw_unknown_finish(entries, manifest):
        rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
        rows[0]["finish_reason"] = "unrecognized-provider-outcome"
        entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(_repack_canonical_archive(raw, raw_unknown_finish)))


def test_logical_digest_uses_domain_policy_not_archive_run_identity():
    first = _manifest(archive_bytes(_projection(archive_id="run-a", created_at=NOW)))
    later = _manifest(archive_bytes(_projection(
        archive_id="run-b", created_at=datetime(2026, 9, 16, tzinfo=UTC),
    )))
    external = _manifest(archive_bytes(_projection(
        archive_id="run-c", created_at=datetime(2026, 9, 17, tzinfo=UTC),
        attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
    )))
    assert first["logical_content_digest"] == later["logical_content_digest"]
    assert first["logical_content_digest"] != external["logical_content_digest"]


def test_validator_rejects_traversal_and_missing_entries():
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as package:
        package.writestr("../manifest.json", b"{}")
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(raw.getvalue()))


def test_real_phase6_core_projection_embeds_one_payload_once(tmp_path):
    async def scenario():
        application, store, authority = phase6_application(tmp_path / "root")
        try:
            source = tmp_path / "fixture.txt"
            source.write_text("attachment truth", encoding="utf-8")
            attachment = store.ingest_attachment(source)
            chat = await application.create_chat("archive fixture")
            await application.stage_attachment(chat.id, attachment.id)
            attempt = await application.send_message(chat.id, "hello")
            await _finish(application, attempt.id)
            projection = await application.prepare_archive_export(chat.id)
            raw = archive_bytes(projection)
            assert validate_archive(io.BytesIO(raw)).archive_id == projection.archive_id
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                assert package.namelist().count(f"payloads/sha256/{attachment.blob_digest}") == 1
        finally:
            await application.close()
            if not store.closed:
                authority.close()
    asyncio.run(scenario())


def test_real_core_terminal_lifecycle_and_finite_decimal_archive_round_trip(tmp_path):
    class TerminalBackend:
        async def stream(self, request):
            yield GenerationDelta(request.attempt_id, "partial")
            if request.prompt == "length":
                yield GenerationCompleted(
                    request.attempt_id, finish_reason="length", known_cost_usd=Decimal("1E-7"),
                )

    async def scenario():
        application, store, authority = phase6_application(tmp_path / "root", TerminalBackend())
        try:
            length_chat = await application.create_chat("length archive")
            length = await application.send_message(length_chat.id, "length")
            await _finish(application, length.id)
            length_attempt = store.get_generation_attempt(length.id)
            assert length_attempt is not None
            length_assistant = store.get_message(length_attempt.assistant_message_id)
            assert length_attempt.state is AttemptState.INCOMPLETE
            assert length_attempt.finish_reason == "length"
            assert length_assistant is not None and length_assistant.state is MessageState.TRUNCATED

            incomplete_chat = await application.create_chat("stream archive")
            incomplete = await application.send_message(incomplete_chat.id, "stream")
            await _finish(application, incomplete.id)
            incomplete_attempt = store.get_generation_attempt(incomplete.id)
            assert incomplete_attempt is not None
            incomplete_assistant = store.get_message(incomplete_attempt.assistant_message_id)
            assert incomplete_attempt.state is AttemptState.INCOMPLETE
            assert incomplete_attempt.finish_reason is None
            assert incomplete_assistant is not None and incomplete_assistant.state is MessageState.INCOMPLETE

            raw = archive_bytes(await application.prepare_archive_export(length_chat.id))
            assert validate_archive(io.BytesIO(raw)).archive_id
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                row = json.loads(package.read("domain/attempts.jsonl"))
            assert row["state"] == "incomplete"
            assert row["finish_reason"] == "length"
            assert row["failure"] == {
                "category": "generation-incomplete", "message": "B.O.T.S. recorded a generation failure.",
            }
            assert row["known_cost_usd"] == "0.0000001"

            def contradictory_incomplete_finish(entries, manifest):
                rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
                rows[0]["finish_reason"] = "stop"
                entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

            def contradictory_complete_finish(entries, manifest):
                attempts = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
                messages = [json.loads(line) for line in entries["domain/messages.jsonl"].splitlines()]
                attempts[0]["state"] = "complete"
                attempts[0]["failure"] = None
                for message in messages:
                    if message["source_id"] == attempts[0]["assistant_message_id"]:
                        message["state"] = "complete"
                entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(attempts)
                entries["domain/messages.jsonl"] = canonical_jsonl_bytes(messages)

            for mutation in (contradictory_incomplete_finish, contradictory_complete_finish):
                with pytest.raises(ArchivePackageError):
                    validate_archive(io.BytesIO(_repack_canonical_archive(raw, mutation)))
        finally:
            await application.close()
            if not store.closed:
                authority.close()

    asyncio.run(scenario())


def test_real_store_to_export_to_reader_preserves_no_error_incomplete_attempt(tmp_path):
    store, chat, user, assistant, attempt = phase3_store_attempt_fixture(tmp_path)
    try:
        phase3_persist_fixture(store, chat, user, assistant, attempt)
        terminal_attempt = replace(
            attempt, state=AttemptState.INCOMPLETE, ended_at=NOW, finish_reason="length",
        )
        terminal_assistant = replace(assistant, state=MessageState.TRUNCATED, content="partial")
        store.finalize_generation(terminal_assistant, terminal_attempt)
        stored = store.get_generation_attempt(attempt.id)
        assert stored is not None and stored.error_type is None and stored.error_message is None
        source = store.read_chat_export_source(chat.id, attachment_policy=AttachmentPolicy.EMBEDDED)
        projection = build_archive_projection(
            archive_id="stored-no-error-incomplete", created_at=NOW, chat=source.chat,
            messages=source.messages, attempts=source.attempts,
            message_attachments=source.message_attachments, attempt_attachments=source.attempt_attachments,
            payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
            chat_configuration=source.chat_configuration,
            application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
            context_plans=source.context_plans,
        )
        raw = archive_bytes(projection)
        assert validate_archive(io.BytesIO(raw)).archive_id == "stored-no-error-incomplete"
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            row = json.loads(package.read("domain/attempts.jsonl"))
        assert row["state"] == "incomplete" and row["failure"] is None
    finally:
        store.close()


def test_reader_applies_landed_version_aware_remote_outcome_rules():
    raw = archive_bytes(_projection())

    def phase3_complete(entries, manifest, *, remote_unknown):
        rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
        row = rows[0]
        row.update({
            "backend_id": "openai_compatible_http", "provider_id": "local_openai", "model": "model",
            "remote_outcome_unknown": remote_unknown,
            "request_time_provenance": {
                "status": "legacy-limited", "snapshot_version": "legacy",
                "attribution": {
                    "backend_id": "openai_compatible_http", "provider_id": "local_openai",
                    "provider_id_status": "available", "model": "model", "model_status": "available",
                },
            },
        })
        entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

    valid_phase3 = _repack_canonical_archive(
        raw, lambda entries, manifest: phase3_complete(entries, manifest, remote_unknown=False),
    )
    assert validate_archive(io.BytesIO(valid_phase3)).archive_id == "archive"
    for remote_unknown in (None, True):
        invalid = _repack_canonical_archive(
            raw,
            lambda entries, manifest, remote_unknown=remote_unknown: phase3_complete(
                entries, manifest, remote_unknown=remote_unknown,
            ),
        )
        with pytest.raises(ArchivePackageError):
            validate_archive(io.BytesIO(invalid))

    # A legacy fake record is outside Phase 3 and retains its valid nullable
    # outcome language; the reader must not invent a universal rule.
    assert validate_archive(io.BytesIO(raw)).archive_id == "archive"


def test_v3_safe_projection_preserves_closed_context_and_excludes_harmless_sensitive_sentinels(tmp_path):
    async def scenario():
        application, store, authority = phase6_application(tmp_path / "root")
        try:
            chat = await application.create_chat("safe projection")
            attempt = await application.send_message(chat.id, "context domain text")
            await _finish(application, attempt.id)
            source = store.read_chat_export_source(chat.id, attachment_policy=AttachmentPolicy.EMBEDDED)
            original = source.attempts[0]
            raw_snapshot = json.loads(original.request_snapshot)
            assert raw_snapshot["snapshot_version"] == 3
            modified = replace(
                original,
                request_snapshot=json.dumps(raw_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                request_id="OPAQUE_REQUEST_ID_SENTINEL",
            )
            projection = build_archive_projection(
                archive_id="safe-run", created_at=NOW, chat=source.chat,
                messages=source.messages, attempts=(modified,),
                message_attachments=source.message_attachments,
                attempt_attachments=source.attempt_attachments,
                payloads={
                    item.attachment.blob_digest: item.payload
                    for values in (*source.message_attachments.values(), *source.attempt_attachments.values())
                    for item in values if item.payload is not None
                },
                attachment_policy=AttachmentPolicy.EMBEDDED,
                chat_configuration=source.chat_configuration,
                application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
                context_plans=source.context_plans,
            )
            raw = archive_bytes(projection)

            def malformed_capability(entries, manifest):
                rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
                rows[0]["request_time_provenance"]["capabilities"][0]["state"] = []
                entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

            def malformed_override_value(entries, manifest):
                rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
                provenance = rows[0]["request_time_provenance"]
                fact = provenance["capabilities"][0]
                provenance["manual_overrides"][fact["key"]] = {
                    "state": fact["state"], "value": [], "revision": 1,
                }
                entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

            def unsafe_attribution_model(entries, manifest):
                rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
                attribution = rows[0]["request_time_provenance"]["attribution"]
                attribution["model"] = "metadata.invalid/opaque"
                attribution["model_status"] = "available"
                rows[0]["model"] = attribution["model"]
                entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

            for mutation in (malformed_capability, malformed_override_value, unsafe_attribution_model):
                with pytest.raises(ArchivePackageError):
                    validate_archive(io.BytesIO(_repack_canonical_archive(raw, mutation)))

            def unknown_v3_complete(entries, manifest):
                rows = [json.loads(line) for line in entries["domain/attempts.jsonl"].splitlines()]
                rows[0]["remote_outcome_unknown"] = True
                entries["domain/attempts.jsonl"] = canonical_jsonl_bytes(rows)

            with pytest.raises(ArchivePackageError):
                validate_archive(io.BytesIO(_repack_canonical_archive(raw, unknown_v3_complete)))
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                attempt_row = json.loads(package.read("domain/attempts.jsonl"))
                persisted_context = json.loads(package.read("domain/context-plans.jsonl"))
                output = b"".join(package.read(name) for name in package.namelist())
            provenance = attempt_row["request_time_provenance"]
            assert provenance["status"] == "available"
            assert provenance["snapshot_version"] == 3
            assert provenance["settings"] == raw_snapshot["effective_settings"]
            assert provenance["capabilities"]
            assert provenance["manual_overrides"] == raw_snapshot["manual_overrides"]
            context = provenance["context"]
            assert context["sources"] and any(item["content"] == "context domain text" for item in context["sources"])
            canonical = json.dumps(context["canonical_projection"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            assert hashlib.sha256(canonical).hexdigest() == context["canonical_projection_digest"]
            assert context["canonical_projection_status"] == "matches-request-time"
            assert context["canonical_digest"] == context["canonical_projection_digest"]
            assert persisted_context == {
                "attempt_id": original.id,
                "plan_version": 3,
                "canonical_digest": raw_snapshot["context_plan"]["canonical_digest"],
                "wire_representation_digest": raw_snapshot["context_plan"]["wire_representation_sha256"],
                "budget_limit": raw_snapshot["context_plan"]["budget"]["limit"],
                "budget_semantics": raw_snapshot["context_plan"]["budget"]["semantics"],
                "adapter_id": raw_snapshot["context_plan"]["budget"]["adapter_id"],
                "adapter_version": raw_snapshot["context_plan"]["budget"]["adapter_version"],
                "input_counts": raw_snapshot["context_plan"]["input_counts"],
                "envelope_overhead": raw_snapshot["context_plan"]["budget"]["envelope_overhead"],
                "output_reserve": raw_snapshot["context_plan"]["budget"]["output_reserve"],
                "input_units": raw_snapshot["context_plan"]["budget"]["input_units"],
                "total_units": raw_snapshot["context_plan"]["budget"]["total_units"],
                "headroom": raw_snapshot["context_plan"]["budget"]["headroom"],
                "created_at": source.context_plans[original.id]["created_at"],
            }
            assert attempt_row["failure"] is None
            domain = b"".join(package for package in (json.dumps(attempt_row).encode(),))
            for sentinel in (b"OPAQUE_REQUEST_ID_SENTINEL", b"endpoint", b"credential_reference"):
                assert sentinel not in domain
            v2_snapshot = dict(raw_snapshot)
            v2_snapshot["snapshot_version"] = 2
            v2_snapshot.pop("context_plan")
            v2_snapshot.pop("settings_revisions")
            v2_attempt = replace(
                original,
                request_snapshot=json.dumps(v2_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            from bots5.core.export import _archive_snapshot
            v2 = _archive_snapshot(v2_attempt, "context domain text")
            assert v2["status"] == "available" and v2["snapshot_version"] == 2
            assert v2["settings"] == raw_snapshot["effective_settings"] and v2["capabilities"]
            sanitized_snapshot = json.loads(json.dumps(raw_snapshot))
            sanitized_snapshot["context_plan"]["sources"][0]["reason"] = "UNSAFE_REASON_SENTINEL"
            canonical_value = json.loads(sanitized_snapshot["context_plan"]["canonical_representation"])
            canonical_value["sources"][0]["reason"] = "UNSAFE_REASON_SENTINEL"
            canonical_text = json.dumps(canonical_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            sanitized_snapshot["context_plan"]["canonical_representation"] = canonical_text
            sanitized_snapshot["context_plan"]["canonical_digest"] = hashlib.sha256(canonical_text.encode()).hexdigest()
            sanitized_attempt = replace(
                original,
                request_snapshot=json.dumps(sanitized_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            sanitized = _archive_snapshot(sanitized_attempt, "context domain text")
            assert sanitized["status"] == "available"
            assert sanitized["context"]["canonical_projection_status"] == "sanitized"
            assert sanitized["context"]["sources"][0]["selection_reason"] == "unavailable"
            assert "UNSAFE_REASON_SENTINEL" not in json.dumps(sanitized)
            invalid_contexts = {key: dict(value) for key, value in source.context_plans.items()}
            invalid_contexts[original.id]["input_counts"] = '{"history_turns_considered":"CONTEXT_ROW_SENTINEL"}'
            with pytest.raises(ExportError, match="persisted context"):
                build_archive_projection(
                    archive_id="invalid-context-row", created_at=NOW, chat=source.chat,
                    messages=source.messages, attempts=(original,),
                    message_attachments=source.message_attachments,
                    attempt_attachments=source.attempt_attachments, payloads={},
                    attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
                    chat_configuration=source.chat_configuration,
                    application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
                    context_plans=invalid_contexts,
                )
        finally:
            await application.close()
            if not store.closed:
                authority.close()
    asyncio.run(scenario())


def test_export_captures_all_chat_model_overrides_with_immutable_portable_descriptors(tmp_path):
    async def scenario():
        application, store, authority = phase6_application(tmp_path / "root")
        try:
            first = await application.create_provider_connection(name="first fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC)
            second = await application.create_provider_connection(name="second fake", backend_type=BackendType.FAKE, profile=ProviderProfile.GENERIC)
            selected = await application.add_manual_model(connection_id=first.id, provider_model_id="vendor-a/selected-model", display_name="Selected Model")
            nonselected = await application.add_manual_model(connection_id=second.id, provider_model_id="vendor-b/other-model", display_name="Other Model")
            chat = await application.create_chat("override fixture")
            store.set_chat_model_selection(chat.id, selected.id)
            selected_revision = store.set_chat_model_generation_settings(
                chat.id, selected.id, GenerationSettings(temperature=0.2, timeout_seconds=3.5),
            )
            other_revision = store.set_chat_model_generation_settings(
                chat.id, nonselected.id,
                GenerationSettings(temperature=1.0, max_output_tokens=77, timeout_seconds=9.25),
            )
            source = store.read_chat_export_source(chat.id, attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE)
            with pytest.raises(TypeError):
                source.chat_configuration["semantic"] = "mutated"
            overrides = source.chat_configuration["overrides"]
            assert isinstance(overrides, tuple)
            assert {item["model"]["provider_model_id"] for item in overrides} == {"vendor-a/selected-model", "vendor-b/other-model"}
            assert {item["revision"] for item in overrides} == {selected_revision, other_revision}
            assert source.chat_configuration["selection"]["model"]["provider_model_id"] == "vendor-a/selected-model"
            source_by_model = {item["model"]["provider_model_id"]: item for item in overrides}
            assert source_by_model["vendor-a/selected-model"]["temperature"] == 0.2
            assert source_by_model["vendor-a/selected-model"]["timeout_seconds"] == 3.5
            assert source_by_model["vendor-b/other-model"]["temperature"] == 1.0
            assert source_by_model["vendor-b/other-model"]["timeout_seconds"] == 9.25
            assert all(type(item["temperature"]) is float for item in overrides)
            assert all(type(item["timeout_seconds"]) is float for item in overrides)
            projection = await application.prepare_archive_export(chat.id)
            configuration = next(item for item in projection.entries if item.path == "domain/chat-configuration.json")
            exported = json.loads(configuration.content)
            assert exported["semantic"] == "inert-continuation-hints"
            assert {item["model"]["provider_model_id"] for item in exported["overrides"]} == {"vendor-a/selected-model", "vendor-b/other-model"}
            assert {item["model"]["provider_model_id_status"] for item in exported["overrides"]} == {"available"}
            assert {item["model"]["display_name"] for item in exported["overrides"]} == {"Selected Model", "Other Model"}
            exported_by_model = {item["model"]["provider_model_id"]: item for item in exported["overrides"]}
            assert exported_by_model["vendor-a/selected-model"]["temperature"] == 0.2
            assert exported_by_model["vendor-a/selected-model"]["timeout_seconds"] == 3.5
            assert exported_by_model["vendor-b/other-model"]["temperature"] == 1.0
            assert exported_by_model["vendor-b/other-model"]["timeout_seconds"] == 9.25
            raw = archive_bytes(projection)
            assert validate_archive(io.BytesIO(raw)).archive_id == projection.archive_id

            def mismatched_descriptor(entries, manifest):
                configuration = json.loads(entries["domain/chat-configuration.json"])
                model = configuration["overrides"][0]["model"]
                model["display_name"] = "CREDENTIAL_REFERENCE_SENTINEL"
                model["display_name_status"] = "available"
                entries["domain/chat-configuration.json"] = canonical_json_bytes(configuration)

            with pytest.raises(ArchivePackageError):
                validate_archive(io.BytesIO(_repack_canonical_archive(raw, mismatched_descriptor)))

            def contradictory_selection(entries, manifest):
                configuration = json.loads(entries["domain/chat-configuration.json"])
                configuration["selection"]["model"] = None
                entries["domain/chat-configuration.json"] = canonical_json_bytes(configuration)

            def zero_descriptor_revision(entries, manifest):
                configuration = json.loads(entries["domain/chat-configuration.json"])
                configuration["selection"]["model"]["model_revision"] = 0
                entries["domain/chat-configuration.json"] = canonical_json_bytes(configuration)

            def same_model_different_descriptor(entries, manifest):
                configuration = json.loads(entries["domain/chat-configuration.json"])
                selected = configuration["selection"]["model"]
                override = next(
                    item for item in configuration["overrides"]
                    if item["model"]["source_model_entry_id"] == selected["source_model_entry_id"]
                )
                override["model"]["display_name"] = "Different Safe Name"
                entries["domain/chat-configuration.json"] = canonical_json_bytes(configuration)

            def same_connection_different_facts(entries, manifest):
                configuration = json.loads(entries["domain/chat-configuration.json"])
                selected = configuration["selection"]["model"]
                override = next(
                    item for item in configuration["overrides"]
                    if item["model"]["source_model_entry_id"] != selected["source_model_entry_id"]
                )
                override["model"]["source_connection_id"] = selected["source_connection_id"]
                override["model"]["connection_revision"] = selected["connection_revision"] + 1
                entries["domain/chat-configuration.json"] = canonical_json_bytes(configuration)

            for mutation in (
                contradictory_selection, zero_descriptor_revision, same_model_different_descriptor,
                same_connection_different_facts,
            ):
                with pytest.raises(ArchivePackageError):
                    validate_archive(io.BytesIO(_repack_canonical_archive(raw, mutation)))
            for key, malformed in (("temperature", True), ("timeout_seconds", math.inf), ("reasoning_effort", [])):
                bad_configuration = {
                    **source.chat_configuration,
                    "overrides": tuple(
                        {**item, key: malformed} if item["model"]["provider_model_id"] == "vendor-a/selected-model" else item
                        for item in overrides
                    ),
                }
                with pytest.raises(ExportError, match="chat continuation override"):
                    _safe_chat_configuration(bad_configuration)
            with pytest.raises(ExportError, match="chat continuation selection"):
                _safe_chat_configuration({
                    **source.chat_configuration,
                    "selection": {**source.chat_configuration["selection"], "model": None},
                })
            with pytest.raises(ExportError, match="descriptor revision"):
                _safe_chat_configuration({
                    **source.chat_configuration,
                    "selection": {
                        **source.chat_configuration["selection"],
                        "model": {**source.chat_configuration["selection"]["model"], "model_revision": 0},
                    },
                })
            assert "application" not in exported and "endpoint" not in configuration.content.decode()
        finally:
            await application.close()
            if not store.closed:
                authority.close()
    asyncio.run(scenario())


def test_external_reference_omits_payload_and_transcript_output_does_not_overwrite(tmp_path):
    projection = _projection()
    external = build_archive_projection(
        archive_id=projection.archive_id, created_at=projection.created_at,
        chat=Chat("chat", "fixture", NOW, NOW, head_message_id="assistant", revision=1),
        messages=(
            Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW),
            Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "done", 2, NOW, parent_id="user"),
        ),
        attempts=(GenerationAttempt("attempt", "chat", "user", "assistant", "fake", "model", AttemptState.COMPLETE, "{}", NOW, ended_at=NOW, finish_reason="stop"),),
        message_attachments={}, attempt_attachments={}, payloads={},
        attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE,
        chat_configuration=_EMPTY_CONFIGURATION,
        application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    with zipfile.ZipFile(io.BytesIO(archive_bytes(external))) as package:
        assert not any(item.startswith("payloads/") for item in package.namelist())
    from bots5.core.export import TranscriptExport, TranscriptScope
    target = tmp_path / "fixture.md"
    write_transcript(TranscriptExport(b"# Fixture\n", TranscriptScope.ACTIVE_PATH), target)
    with pytest.raises(FileExistsError):
        write_transcript(TranscriptExport(b"# Fixture\n", TranscriptScope.ACTIVE_PATH), target)


def _cycle_parent(entries, manifest):
    rows = [json.loads(line) for line in entries["domain/messages.jsonl"].splitlines()]
    rows[1]["parent_id"] = rows[1]["source_id"]
    entries["domain/messages.jsonl"] = canonical_jsonl_bytes(rows)


def _user_head(entries, manifest):
    entries["domain/chat.json"] = canonical_json_bytes({**json.loads(entries["domain/chat.json"]), "head_message_id": "user"})


def _boolean_version(entries, manifest):
    manifest["archive_version"] = True


@pytest.mark.parametrize("mutation", [_cycle_parent, _user_head, _boolean_version])
def test_reader_rejects_rehashed_closed_schema_and_graph_mutants(mutation):
    raw = _repack_canonical_archive(archive_bytes(_projection()), mutation)
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(raw))


def test_reader_rejects_rehashed_payload_and_external_resource_contradictions():
    embedded = archive_bytes(_attachment_projection())
    bad_payload = _repack_canonical_archive(
        embedded,
        lambda entries, manifest: entries.__setitem__("payloads/sha256/" + sha256_hex(b"x"), b"y"),
    )
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(bad_payload))
    external = archive_bytes(_attachment_projection(policy=AttachmentPolicy.EXTERNAL_REFERENCE))
    bad_external = _repack_canonical_archive(
        external,
        lambda entries, manifest: manifest["external_resources"][0].__setitem__("size", 2),
    )
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(bad_external))


def test_writer_uses_a_reader_supported_representation_for_repetitive_payloads():
    raw = archive_bytes(_attachment_projection(payload=b"x" * 8192))
    assert validate_archive(io.BytesIO(raw)).entry_count > 0
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        info = package.getinfo("payloads/sha256/" + sha256_hex(b"x" * 8192))
        assert info.compress_type == zipfile.ZIP_STORED


def test_reader_checks_raw_central_names_before_zipfile_normalizes_them():
    raw = archive_bytes(_projection())
    # Same-length bounded corruption in local/central metadata (and the
    # inventory spelling) is enough to prove the raw-name guard, without
    # constructing an extraction payload or a large archive.
    bad = raw.replace(b"domain/chat.json", b"domain/\x00hat.json")
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(bad))


def test_reader_checks_local_header_names_and_canonical_singletons():
    raw = archive_bytes(_projection())
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        offset = package.getinfo("domain/chat.json").header_offset + 30
    local_name_mismatch = bytearray(raw)
    local_name_mismatch[offset:offset + len(b"domain/chat.json")] = b"domain/CHAT.json"
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(local_name_mismatch))
    noncanonical = _repack_canonical_archive(
        raw,
        lambda entries, manifest: entries.__setitem__(
            "domain/chat.json", entries["domain/chat.json"].replace(b'"revision":1', b'"revision": 1')
        ),
    )
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(noncanonical))


def test_reader_bounds_central_directory_before_member_allocation():
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as package:
        for number in range(archive_package.MAX_ENTRIES + 1):
            package.writestr(f"members/{number}", b"")
    with pytest.raises(ArchivePackageError, match="central directory"):
        validate_archive(io.BytesIO(raw.getvalue()))


def test_reader_uses_one_private_path_snapshot_for_preflight_and_parser(tmp_path, monkeypatch):
    source = tmp_path / "fixture.botsarchive"
    raw = archive_bytes(_projection())
    source.write_bytes(raw)
    preflight_streams = []
    parser_streams = []
    original_preflight = archive_package._preflight_central_directory
    original_zipfile = archive_package.zipfile.ZipFile

    def tracked_preflight(stream):
        preflight_streams.append(stream)
        if len(preflight_streams) == 2:
            assert preflight_streams[0].closed
        return original_preflight(stream)

    def tracked_zipfile(stream, *args, **kwargs):
        parser_streams.append(stream)
        assert preflight_streams[0].closed
        position = stream.tell()
        stream.seek(0)
        assert hashlib.sha256(stream.read()).digest() == hashlib.sha256(raw).digest()
        stream.seek(position)
        return original_zipfile(stream, *args, **kwargs)

    monkeypatch.setattr(archive_package, "_preflight_central_directory", tracked_preflight)
    monkeypatch.setattr(archive_package.zipfile, "ZipFile", tracked_zipfile)
    assert validate_archive(source).archive_id == "archive"
    assert len(preflight_streams) == 2
    assert len(parser_streams) == 1
    assert preflight_streams[0] is not parser_streams[0]
    assert preflight_streams[1] is parser_streams[0]


def test_reader_capture_is_chunk_bounded_and_restores_binary_input_position():
    class RecordingBytesIO(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    source = RecordingBytesIO(archive_bytes(_projection()))
    source.seek(17)
    assert validate_archive(source).archive_id == "archive"
    assert source.tell() == 17
    assert source.read_sizes
    assert all(type(size) is int and 0 <= size <= archive_package._CAPTURE_CHUNK_BYTES for size in source.read_sizes)


def test_reader_rejects_short_capture_before_zipfile_and_closes_snapshot(monkeypatch):
    class ShortCaptureStream(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.short_capture = False

        def read(self, size=-1):
            if self.short_capture and size > 1:
                return b""
            return super().read(size)

    source = ShortCaptureStream(archive_bytes(_projection()))
    original_preflight = archive_package._preflight_central_directory
    original_temporary_file = archive_package.tempfile.TemporaryFile
    snapshots = []
    zipfile_calls = []

    def admit_then_shorten(stream):
        result = original_preflight(stream)
        if stream is source:
            source.short_capture = True
        return result

    def tracked_temporary_file(*args, **kwargs):
        snapshot = original_temporary_file(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    def unexpected_zipfile(*args, **kwargs):
        zipfile_calls.append((args, kwargs))
        raise AssertionError("ZipFile must not receive an incomplete capture")

    monkeypatch.setattr(archive_package, "_preflight_central_directory", admit_then_shorten)
    monkeypatch.setattr(archive_package.tempfile, "TemporaryFile", tracked_temporary_file)
    monkeypatch.setattr(archive_package.zipfile, "ZipFile", unexpected_zipfile)
    with pytest.raises(ArchivePackageError, match="capture is incomplete"):
        validate_archive(source)
    assert not zipfile_calls
    assert len(snapshots) == 1 and snapshots[0].closed
    assert source.tell() == 0


def test_reader_rejects_duplicate_attachment_relation_after_inventory_rehash():
    raw = archive_bytes(_attachment_projection())

    def duplicate_relation(entries, manifest):
        rows = [json.loads(line) for line in entries["domain/message-attachments.jsonl"].splitlines()]
        entries["domain/message-attachments.jsonl"] = canonical_jsonl_bytes((*rows, rows[0]))

    bad = _repack_canonical_archive(raw, duplicate_relation)
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(bad))


@pytest.mark.parametrize("mutation", [
    lambda entries, manifest: manifest["entry_inventory"][0].__setitem__("media_type", "text/plain"),
    lambda entries, manifest: manifest["entry_inventory"][0].__setitem__("required", False),
])
def test_reader_requires_exact_inventory_media_and_required_flags(mutation):
    bad = _repack_canonical_archive(archive_bytes(_projection()), mutation)
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(bad))


def test_reader_refuses_an_inventory_covered_unknown_namespace_member():
    def add_unknown(entries, manifest):
        entries["domain/unknown.json"] = b"{}\n"
        manifest["entry_inventory"].append({
            "path": "domain/unknown.json", "media_type": "application/json", "required": True,
            "uncompressed_size": 3, "sha256": sha256_hex(b"{}\n"),
        })

    bad = _repack_canonical_archive(archive_bytes(_projection()), add_unknown)
    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(bad))


def test_reader_applies_landed_supersession_rules_without_parent_position_invention():
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="a2", revision=2)
    u1 = Message("u1", "chat", MessageRole.USER, MessageState.SENT, "first", 1, NOW, lineage_id="user-lineage")
    a1 = Message("a1", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "first answer", 2, NOW, parent_id="u1", lineage_id="assistant-one")
    # sqlite.py validates same role/chat/lineage and an immediate preceding
    # revision. It does not impose the earlier reader's extra parent equality.
    u2 = Message("u2", "chat", MessageRole.USER, MessageState.SENT, "edited", 3, NOW, parent_id="a1", lineage_id="user-lineage", revision=2, supersedes_id="u1")
    a2 = Message("a2", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "edited answer", 4, NOW, parent_id="u2", lineage_id="assistant-two")
    attempts = (
        GenerationAttempt("attempt-1", "chat", "u1", "a1", "fake", "model", AttemptState.COMPLETE, "{}", NOW, ended_at=NOW, finish_reason="stop"),
        GenerationAttempt("attempt-2", "chat", "u2", "a2", "fake", "model", AttemptState.COMPLETE, "{}", NOW, ended_at=NOW, finish_reason="stop"),
    )
    projection = build_archive_projection(
        archive_id="revision-graph", created_at=NOW, chat=chat, messages=(u1, a1, u2, a2), attempts=attempts,
        message_attachments={}, attempt_attachments={}, payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
        chat_configuration=_EMPTY_CONFIGURATION, application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    assert validate_archive(io.BytesIO(archive_bytes(projection))).archive_id == "revision-graph"

    def orphan_revision(entries, manifest):
        rows = [json.loads(line) for line in entries["domain/messages.jsonl"].splitlines()]
        rows[2]["supersedes_id"] = None
        entries["domain/messages.jsonl"] = canonical_jsonl_bytes(rows)

    with pytest.raises(ArchivePackageError):
        validate_archive(io.BytesIO(_repack_canonical_archive(archive_bytes(projection), orphan_revision)))


def test_writer_refuses_a_user_head_even_if_the_builder_received_it():
    chat = Chat("chat", "fixture", NOW, NOW, head_message_id="user")
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "hello", 1, NOW)
    projection = build_archive_projection(
        archive_id="bad-head", created_at=NOW, chat=chat, messages=(user,), attempts=(),
        message_attachments={}, attempt_attachments={}, payloads={}, attachment_policy=AttachmentPolicy.EMBEDDED,
        chat_configuration=_EMPTY_CONFIGURATION, application_version="0.1.0", migration_revision="0011_phase8_inspector_state",
    )
    with pytest.raises(ArchivePackageError):
        archive_bytes(projection)


def test_descriptor_relative_publisher_refuses_symlink_ancestors_and_keeps_parent_identity(tmp_path, monkeypatch):
    projection = _projection()
    safe = tmp_path / "safe" / "nested"
    safe.mkdir(parents=True)
    final = safe / "archive.botsarchive"
    write_archive(projection, final)
    assert final.exists() and stat.S_IMODE(final.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_archive(projection, final)
    final_link = safe / "existing-link.botsarchive"
    final_link.symlink_to(final)
    with pytest.raises(FileExistsError):
        write_archive(projection, final_link)
    redirected = tmp_path / "redirect"
    redirected.symlink_to(safe, target_is_directory=True)
    with pytest.raises(ArchivePackageError):
        write_archive(projection, redirected / "unsafe.botsarchive")

    held_root = tmp_path / "held"
    held_parent = held_root / "nested"
    held_parent.mkdir(parents=True)
    destination = held_parent / "continuity.botsarchive"
    original_link = archive_package.os.link

    def replace_ancestor_then_link(*args, **kwargs):
        moved = tmp_path / "moved"
        held_root.rename(moved)
        (tmp_path / "held" / "nested").mkdir(parents=True)
        return original_link(*args, **kwargs)

    monkeypatch.setattr(archive_package.os, "link", replace_ancestor_then_link)
    write_archive(projection, destination)
    assert (tmp_path / "moved" / "nested" / "continuity.botsarchive").exists()
    assert not (tmp_path / "held" / "nested" / "continuity.botsarchive").exists()


def test_export_source_cut_is_all_before_while_delete_journal_writer_commits_after_release(tmp_path):
    application, store, authority = phase6_application(tmp_path / "root")
    anchored = threading.Event()
    release = threading.Event()
    writer_update = threading.Event()
    result = {}
    errors = []
    select_connections = set()
    reader_id = {}
    reader = None
    updater = None

    def after_cursor_execute(connection, _cursor, statement, _parameters, _context, _many):
        upper = statement.lstrip().upper()
        if threading.get_ident() == reader_id.get("value") and upper.startswith("SELECT"):
            select_connections.add(id(connection))
            if "FROM CHATS" in upper:
                assert connection.connection.driver_connection.in_transaction
                anchored.set()
                assert release.wait(5)
        if upper.startswith("UPDATE MESSAGES"):
            writer_update.set()

    def read_worker():
        try:
            reader_id["value"] = threading.get_ident()
            result["source"] = store.read_chat_export_source(
                result["chat_id"], attachment_policy=AttachmentPolicy.EXTERNAL_REFERENCE
            )
        except BaseException as exc:
            errors.append(exc)

    def writer():
        try:
            message = store.get_message(result["assistant_id"])
            assert message is not None
            store.update_streaming_message(
                replace(message, state=MessageState.STREAMING, content="after")
            )
        except BaseException as exc:
            errors.append(exc)

    event.listen(store._engine, "after_cursor_execute", after_cursor_execute)
    try:
        store.create_chat(Chat("chat", "before", NOW, NOW))
        chat = Chat("chat", "before", NOW, NOW, head_message_id="assistant", revision=1)
        user = Message("user", chat.id, MessageRole.USER, MessageState.SENT, "before", 1, NOW)
        assistant = Message("assistant", chat.id, MessageRole.ASSISTANT, MessageState.STREAMING, "before", 2, NOW, parent_id=user.id)
        attempt = GenerationAttempt("attempt", chat.id, user.id, assistant.id, "fake", "fake", AttemptState.RUNNING, "{}", NOW)
        store.persist_generation_start(chat, user, assistant, attempt)
        result["chat_id"] = chat.id
        result["assistant_id"] = assistant.id
        reader = threading.Thread(target=read_worker)
        reader.start()
        assert anchored.wait(5)
        updater = threading.Thread(target=writer)
        updater.start()
        assert writer_update.wait(5)
        assert "source" not in result
        release.set()
        reader.join(5)
        updater.join(5)
        assert not reader.is_alive() and not updater.is_alive()
        assert not errors
        assert result["source"].chat is not None
        assert len(select_connections) == 1
        assert result["source"].messages[-1].content != "after"
        assert store.get_message(result["assistant_id"]).content == "after"
    finally:
        release.set()
        if reader is not None:
            reader.join(5)
        if updater is not None:
            updater.join(5)
        event.remove(store._engine, "after_cursor_execute", after_cursor_execute)
        asyncio.run(application.close())
        if not store.closed:
            authority.close()


def test_running_cut_renders_transcript_and_refuses_archive_until_terminal(tmp_path):
    async def scenario():
        application, store, authority = phase6_application(tmp_path / "root")
        try:
            chat = await application.create_chat("running")
            attempt = await application.send_message(chat.id, "prompt")
            transcript = await application.export_transcript(chat.id, scope=TranscriptScope.FULL_LINEAGE)
            assert b"point-in-time view" in transcript.content
            with pytest.raises(Exception, match="running generation"):
                await application.prepare_archive_export(chat.id)
            await _finish(application, attempt.id)
            assert (await application.prepare_archive_export(chat.id)).chat_id == chat.id
        finally:
            await application.close()
            if not store.closed:
                authority.close()
    asyncio.run(scenario())
