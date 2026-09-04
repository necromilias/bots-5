from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bots5.domain.models import Chat, Message, MessageRole, MessageState


NOW = datetime(2026, 9, 3, tzinfo=UTC)


def _message(**overrides) -> Message:
    values = {
        "id": "message",
        "chat_id": "chat",
        "role": MessageRole.USER,
        "state": MessageState.SENT,
        "content": "hello",
        "sequence": 1,
        "created_at": NOW,
    }
    values.update(overrides)
    return Message(**values)


def test_message_defaults_to_a_self_lineage_root():
    message = _message()

    assert message.lineage_id == message.id
    assert message.revision == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"revision": 0},
        {"parent_id": "message"},
        {"supersedes_id": "message"},
    ],
)
def test_message_rejects_invalid_lineage_identity(overrides):
    with pytest.raises(ValueError):
        _message(**overrides)


def test_chat_rejects_a_negative_revision():
    with pytest.raises(ValueError):
        Chat("chat", "Chat", NOW, NOW, revision=-1)
