from datetime import UTC, datetime
from pathlib import Path

from bots5.core.inspection import build_inspection_projection
from bots5.domain.models import AttemptState, Chat, GenerationAttempt, Message, MessageRole, MessageState


NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _projection(snapshot: str):
    chat = Chat("chat", "Inspection", NOW, NOW, head_message_id="assistant")
    user = Message("user", "chat", MessageRole.USER, MessageState.SENT, "question", 1, NOW)
    assistant = Message("assistant", "chat", MessageRole.ASSISTANT, MessageState.COMPLETE, "answer", 2, NOW, parent_id="user")
    attempt = GenerationAttempt("attempt", "chat", "user", "assistant", "fake", "fake-v0.1", AttemptState.COMPLETE, snapshot, NOW)
    return build_inspection_projection(
        chat=chat, message=assistant, historical_leaf_message_id="assistant", revision_count=1,
        attempts=(attempt,), user_content_by_attempt={"attempt": "question"},
        message_attachments=(), attempt_attachments={"attempt": ()},
    )


def test_legacy_projection_and_chat_history_are_display_safe():
    projection = _projection(
        '{"attempt_id":"attempt","backend_id":"fake","chat_id":"chat",'
        '"model":"fake-v0.1","prompt":"question","user_message_id":"user"}'
    )
    fields = {item.name: item.value for item in projection.fields}
    assert projection.status == "available"
    assert fields["Attempt 1 snapshot"] == "available"
    assert fields["Attempt 1 Snapshot"] == "legacy/v1"
    assert fields["Attempt 1 provider/model"] == "fake / fake-v0.1"
    assert fields["Historical leaf"] == "assistant"


def test_corrupt_or_secret_shaped_snapshot_is_not_exposed():
    projection = _projection('{"snapshot_version":3,"secret":"DO_NOT_EXPOSE"}')
    rendered = "\n".join(item.value for item in projection.fields)
    assert projection.status == "corrupt request snapshot"
    assert "DO_NOT_EXPOSE" not in rendered
    assert "secret" not in rendered.casefold()


def test_versionless_nested_secret_shaped_data_is_legacy_limited_and_not_exposed():
    projection = _projection(
        '{"effective_settings":{"password":"LEGACY_SECRET_SENTINEL"}}'
    )
    rendered = "\n".join(f"{item.name}: {item.value}" for item in projection.fields)

    assert projection.status == "legacy-limited request snapshot"
    assert "Attempt 1 snapshot: legacy-limited request snapshot" in rendered
    assert "LEGACY_SECRET_SENTINEL" not in rendered
    assert "effective_settings" not in rendered
    assert "password" not in rendered.casefold()


def test_unknown_future_snapshot_is_unsupported_and_never_rendered_as_provenance():
    projection = _projection(
        '{"snapshot_version":4,"provider_id":"fabricated-provider",'
        '"model":"fabricated-model","effective_settings":{"temperature":99}}'
    )
    fields = {item.name: item.value for item in projection.fields}
    rendered = "\n".join(f"{item.name}: {item.value}" for item in projection.fields)

    assert projection.status == "unsupported request snapshot"
    assert fields["Attempt 1 snapshot"] == "unsupported request snapshot"
    assert "Attempt 1 Snapshot" not in fields
    assert "fabricated-provider" not in rendered
    assert "fabricated-model" not in rendered
    assert "temperature" not in rendered


def test_inspector_reports_native_import_and_export_provenance_truthfully():
    fields = {item.name: item.value for item in _projection("{}").fields}

    assert fields["Import provenance"] == "native"
    assert fields["Export provenance"] == "not recorded"


def test_roadmap_current_state_includes_landed_phase9_slice_b():
    roadmap = (Path(__file__).resolve().parents[1] / "docs/ROADMAP.md").read_text(
        encoding="utf-8"
    )

    assert "Phases **1 through 8 and Phase 9 Slices A and B are implemented, validated, committed, and landed" in roadmap
    assert "9a84d38b6ad2d3968db58f471d53bf85820656b1" in roadmap
    assert "Phase 9 Slice C" in roadmap
    assert "Phases **1 through 8 and Phase 9 Slice A are implemented, validated, committed, and landed" not in roadmap
