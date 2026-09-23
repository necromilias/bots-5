from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace

import pytest
import bots5.core.archive_import as archive_import
from bots5.core.interchange import canonical_json_bytes, canonical_jsonl_bytes, parse_jsonl, sha256_hex

from bots5.core.archive_import import (
    ImportErrorCode,
    RESOLUTION_CANCELLED,
    UNSUPPORTED,
    SOURCE_CHANGED,
    capture_source,
    capture_validated_archive,
    close_payload_snapshots,
    resolve_source,
    resolve_external_payloads,
    source_fingerprint,
)
from bots5.core.archive_import import ARCHIVE_INVALID, RESOLVER_UNAVAILABLE, SOURCE_OUTSIDE_RESOLVER
from bots5.infrastructure.archive_package import archive_bytes
from tests.test_phase9_archive import _attachment_projection
from tests.test_phase9_archive import _repack_canonical_archive
from tests.test_phase9_archive_v2 import missing_external_archive


def test_capture_binds_exact_opened_source_bytes_before_archive_validation(tmp_path):
    source = tmp_path / "fixture.botsarchive"
    source.write_bytes(b"sealed source")
    fingerprint = source_fingerprint(source)
    binding, captured = capture_source(source, fingerprint, chunk_size=4096)
    assert captured == b"sealed source"
    assert binding.sha256 == hashlib.sha256(captured).hexdigest()
    assert binding.byte_size == len(captured)


def test_capture_refuses_path_replacement_before_any_effect(tmp_path):
    source = tmp_path / "fixture.botsarchive"
    source.write_bytes(b"old")
    fingerprint = source_fingerprint(source)
    source.unlink()
    source.write_bytes(b"new bytes")
    with pytest.raises(ImportErrorCode, match=SOURCE_CHANGED):
        capture_source(source, fingerprint, chunk_size=4096)


def test_validated_capture_retains_exact_embedded_payload_plan_from_the_same_bytes(tmp_path):
    source = tmp_path / "fixture.botsarchive"
    payload = b"sealed attachment bytes"
    source.write_bytes(archive_bytes(_attachment_projection(payload=payload)))
    captured = capture_validated_archive(source, source_fingerprint(source))
    digest, snapshot = captured.payloads[0]
    assert digest == hashlib.sha256(payload).hexdigest()
    assert snapshot.handle.read() == payload
    close_payload_snapshots(captured)
    assert snapshot.handle.closed


def test_external_resolver_captures_one_verified_payload_and_deduplicates_by_digest(tmp_path):
    payload = b"external payload"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "source.botsarchive"
    archive.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    root = tmp_path / "resolver"; root.mkdir()
    (root / "one").write_bytes(payload)
    (root / "other").write_bytes(payload)
    captured = capture_validated_archive(archive, source_fingerprint(archive))
    resolved = resolve_external_payloads(captured, (root,))
    actual_digest, snapshot = resolved.payloads[0]
    assert actual_digest == digest and snapshot.handle.read() == payload
    close_payload_snapshots(resolved)
    assert snapshot.handle.closed


def test_external_resolver_distinguishes_absence_and_cancellation(tmp_path):
    payload = b"external payload"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "source.botsarchive"
    archive.write_bytes(missing_external_archive(digest=digest, size=len(payload)))
    root = tmp_path / "resolver"; root.mkdir()
    captured = capture_validated_archive(archive, source_fingerprint(archive))
    # Missing-external is a truthful result rather than a failed preflight.
    assert resolve_external_payloads(captured, (root,)).payloads == ()
    with pytest.raises(ImportErrorCode, match=RESOLUTION_CANCELLED):
        resolve_external_payloads(captured, (root,), cancelled=lambda: True)


def test_resolver_cancellation_after_first_snapshot_closes_it(tmp_path, monkeypatch):
    first = b"first external payload"; second = b"second external payload"
    first_digest = hashlib.sha256(first).hexdigest(); second_digest = hashlib.sha256(second).hexdigest()
    archive = tmp_path / "source.botsarchive"
    archive.write_bytes(missing_external_archive(digest=first_digest, size=len(first)))
    root = tmp_path / "resolver"; root.mkdir()
    (root / "first").write_bytes(first); (root / "second").write_bytes(second)
    captured = capture_validated_archive(archive, source_fingerprint(archive))
    original_attachment = dict(captured.plan.attachments[0])
    second_attachment = dict(original_attachment, source_id="attachment-second", blob_digest=second_digest, byte_size=len(second))
    captured = replace(captured, plan=replace(captured.plan, attachments=(original_attachment, second_attachment)))
    acquired = []
    original_capture = archive_import._capture_snapshot
    def track(*args, **kwargs):
        binding, handle = original_capture(*args, **kwargs); acquired.append(handle); return binding, handle
    monkeypatch.setattr(archive_import, "_capture_snapshot", track)
    with pytest.raises(ImportErrorCode, match=RESOLUTION_CANCELLED):
        resolve_external_payloads(captured, (root,), cancelled=lambda: bool(acquired))
    assert len(acquired) == 1 and acquired[0].closed


def test_export_time_verified_external_bytes_may_be_absent_at_import(tmp_path):
    payload = b"exporter had this external payload"
    archive = tmp_path / "source.botsarchive"
    archive.write_bytes(missing_external_archive(
        digest=hashlib.sha256(payload).hexdigest(), size=len(payload), availability="verified",
    ))
    root = tmp_path / "resolver"; root.mkdir()
    captured = capture_validated_archive(archive, source_fingerprint(archive))
    # The source's verified status is historical evidence.  A receiver with
    # no matching rooted candidate must import the relation as unavailable.
    assert resolve_external_payloads(captured, (root,)).payloads == ()


def test_capture_classifies_unknown_archive_semantics_without_import_effects(tmp_path):
    """Unknown required version/feature semantics are not malformed v1/v2."""
    source = tmp_path / "unknown-version.botsarchive"
    raw = archive_bytes(_attachment_projection(payload=b"typed intake"))
    def future_version(_entries, manifest):
        manifest["archive_version"] = 99
    source.write_bytes(_repack_canonical_archive(raw, future_version))
    with pytest.raises(ImportErrorCode, match=UNSUPPORTED):
        capture_validated_archive(source, source_fingerprint(source))

    source = tmp_path / "unknown-member.botsarchive"
    def future_member(entries, manifest):
        path = "domain/future-required.json"
        entries[path] = canonical_json_bytes({"semantic": "future-required"})
        manifest["entry_inventory"].append({
            "path": path, "media_type": "application/json", "required": True,
            "uncompressed_size": 0, "sha256": "0" * 64,
        })
    source.write_bytes(_repack_canonical_archive(raw, future_member))
    with pytest.raises(ImportErrorCode, match=UNSUPPORTED):
        capture_validated_archive(source, source_fingerprint(source))

    raw = missing_external_archive()
    source = tmp_path / "unknown-v2-feature.botsarchive"
    def future_feature(_entries, manifest):
        manifest["features"] = sorted([*manifest["features"], "future-required-feature"])
    source.write_bytes(_repack_canonical_archive(raw, future_feature))
    with pytest.raises(ImportErrorCode, match=UNSUPPORTED):
        capture_validated_archive(source, source_fingerprint(source))

    source = tmp_path / "unknown-v2-member.botsarchive"
    source.write_bytes(_repack_canonical_archive(raw, future_member))
    with pytest.raises(ImportErrorCode, match=UNSUPPORTED):
        capture_validated_archive(source, source_fingerprint(source))


def test_known_malformed_required_data_is_invalid_not_unsupported(tmp_path):
    """Known malformed required rows are corruption, not future semantics."""
    source = tmp_path / "malformed-chat.botsarchive"
    raw = archive_bytes(_attachment_projection(payload=b"typed intake"))

    def malformed_chat(entries, _manifest):
        chat = json.loads(entries["domain/chat.json"])
        chat["source_id"] = ""
        entries["domain/chat.json"] = canonical_json_bytes(chat)

    source.write_bytes(_repack_canonical_archive(raw, malformed_chat))
    with pytest.raises(ImportErrorCode, match=ARCHIVE_INVALID):
        capture_validated_archive(source, source_fingerprint(source))


def test_resolver_rejects_outside_source_nonregular_and_symlink_candidates(tmp_path):
    """One-shot resolver roots stay bounded, regular, and contained."""
    source = tmp_path / "source.botsarchive"
    payload = b"contained external payload"
    source.write_bytes(missing_external_archive(
        digest=hashlib.sha256(payload).hexdigest(), size=len(payload),
    ))
    captured = capture_validated_archive(source, source_fingerprint(source))

    contained = tmp_path / "contained"; contained.mkdir()
    with pytest.raises(ImportErrorCode, match=SOURCE_OUTSIDE_RESOLVER):
        resolve_source(source, (contained,))

    fifo_root = tmp_path / "fifo-root"; fifo_root.mkdir()
    os.mkfifo(fifo_root / "candidate")
    with pytest.raises(ImportErrorCode, match=RESOLVER_UNAVAILABLE):
        resolve_external_payloads(captured, (fifo_root,))

    outside_payload = tmp_path / "outside-payload.bin"; outside_payload.write_bytes(payload)
    link_root = tmp_path / "link-root"; link_root.mkdir()
    (link_root / "candidate").symlink_to(outside_payload)
    with pytest.raises(ImportErrorCode, match=RESOLVER_UNAVAILABLE):
        resolve_external_payloads(captured, (link_root,))

    outside_directory = tmp_path / "outside-directory"; outside_directory.mkdir()
    (outside_directory / "payload.bin").write_bytes(payload)
    directory_root = tmp_path / "directory-root"; directory_root.mkdir()
    (directory_root / "outside").symlink_to(outside_directory, target_is_directory=True)
    assert resolve_external_payloads(captured, (directory_root,)).payloads == ()


def test_embedded_payload_cancellation_survives_capture_without_invalid_recast(tmp_path):
    payload = b"payload cancellation must remain typed"
    source = tmp_path / "embedded.botsarchive"
    source.write_bytes(archive_bytes(_attachment_projection(payload=payload)))
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 2
    with pytest.raises(ImportErrorCode, match=RESOLUTION_CANCELLED):
        capture_validated_archive(source, source_fingerprint(source), cancelled=cancelled)
    assert checks >= 2


def test_second_embedded_snapshot_failure_closes_first_retained_handle(tmp_path, monkeypatch):
    """A later embedded payload failure releases every earlier anonymous handle."""
    first = b"first embedded payload"; second = b"second embedded payload"
    raw = archive_bytes(_attachment_projection(payload=first))
    def add_second(entries, manifest):
        attachments = list(parse_jsonl(entries["domain/attachments.jsonl"]))
        duplicate = dict(attachments[0]); duplicate["source_id"] = "attachment-second"
        duplicate["blob_digest"] = sha256_hex(second); duplicate["byte_size"] = len(second)
        attachments.append(duplicate)
        entries["domain/attachments.jsonl"] = canonical_jsonl_bytes(attachments)
        links = list(parse_jsonl(entries["domain/message-attachments.jsonl"]))
        links.append({"message_id": "user", "attachment_id": "attachment-second", "ordinal": 1})
        entries["domain/message-attachments.jsonl"] = canonical_jsonl_bytes(links)
        path = f"payloads/sha256/{sha256_hex(second)}"
        entries[path] = second
        manifest["entry_inventory"].append({
            "path": path, "media_type": "application/octet-stream", "required": True,
            "uncompressed_size": 0, "sha256": "0" * 64,
        })
    source = tmp_path / "two-embedded.botsarchive"
    source.write_bytes(_repack_canonical_archive(raw, add_second))
    acquired = []
    real_snapshot_member = archive_import._snapshot_member
    def fail_second(*args, **kwargs):
        if acquired:
            raise ImportErrorCode(archive_import.RESOURCE_LIMIT)
        snapshot = real_snapshot_member(*args, **kwargs)
        acquired.append(snapshot.handle)
        return snapshot
    monkeypatch.setattr(archive_import, "_snapshot_member", fail_second)
    with pytest.raises(ImportErrorCode, match=archive_import.RESOURCE_LIMIT):
        capture_validated_archive(source, source_fingerprint(source))
    assert len(acquired) == 1 and acquired[0].closed
