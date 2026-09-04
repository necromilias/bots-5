from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

from bots5.core.application import BotsApplication
from bots5.core.errors import StateError
from bots5.core.events import EventBus, EventSubscription
from bots5.core.generation import (
    GenerationCompleted,
    GenerationDelta,
    GenerationDispatched,
    GenerationRequest,
)
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import (
    AttemptState,
    Chat,
    GenerationAttempt,
    Message,
    MessageRole,
    MessageState,
)
from bots5.errors import ProviderError, ProviderResponseError
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence import SQLiteAppStateStore, upgrade_database
from bots5.providers.base import CompletionRequest, CompletionStreamEvent
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider


REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "src/bots5/infrastructure/persistence/migrations"


def _upgrade_to(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, revision)


def test_default_fake_snapshot_preserves_phase1_shape(tmp_path: Path):
    async def scenario():
        application = _application(
            tmp_path,
            FakeStreamingBackend(),
            model="fake-v0.1",
        )
        try:
            chat = await application.create_chat()
            attempt = await application.send_message(chat.id, "hello")
            snapshot = json.loads(attempt.request_snapshot)
            assert snapshot == {
                "attempt_id": attempt.id,
                "backend_id": "fake",
                "chat_id": chat.id,
                "model": "fake-v0.1",
                "prompt": "hello",
                "user_message_id": attempt.user_message_id,
            }
        finally:
            await application.close()

    asyncio.run(scenario())


def test_phase3_outcome_columns_are_additive_and_nullable(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        columns = {
            column["name"]: column
            for column in inspect(store.engine).get_columns("generation_attempts")
        }
        outcome_columns = {
            "provider_id",
            "returned_model",
            "request_id",
            "finish_reason",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
            "known_cost_usd",
            "remote_outcome_unknown",
        }
        assert outcome_columns <= columns.keys()
        assert all(columns[name]["nullable"] for name in outcome_columns)
        with store.engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0005_generation_outcomes"
            )
    finally:
        store.close()


def _application(
    tmp_path: Path,
    backend,
    *,
    model: str = "qwen-local",
    provider_id: str = "local_openai",
    base_url: str = "http://127.0.0.1:9000/v1",
    api_key_env: str | None = None,
) -> BotsApplication:
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=32),
        backend,
        ids=ids,
        clock=clock,
        backend_id=backend.backend_id if hasattr(backend, "backend_id") else "fake",
        model=model,
        provider_id=provider_id if hasattr(backend, "provider_id") else None,
        base_url=base_url if hasattr(backend, "base_url") else None,
        api_key_env=api_key_env if hasattr(backend, "api_key_env") else None,
    )


async def _terminal(subscription: EventSubscription, attempt_id: str) -> list[str]:
    kinds: list[str] = []
    while True:
        event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        kinds.append(event.kind)
        if event.payload.get("attempt_id") == attempt_id and event.kind in {
            "generation_completed",
            "generation_incomplete",
            "generation_failed",
            "generation_aborted",
        }:
            return kinds


def _store_attempt_fixture(
    tmp_path: Path,
    *,
    snapshot: str | None = None,
    provider_id: str | None = "local_openai",
    backend_id: str = "openai_compatible_http",
):
    now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    chat = Chat("chat", "Chat", now, now)
    user = Message(
        "user",
        chat.id,
        MessageRole.USER,
        MessageState.SENT,
        "current user message",
        1,
        now,
    )
    assistant = Message(
        "assistant",
        chat.id,
        MessageRole.ASSISTANT,
        MessageState.STREAMING,
        "",
        2,
        now,
        parent_id=user.id,
    )
    attempt = GenerationAttempt(
        id="attempt",
        chat_id=chat.id,
        user_message_id=user.id,
        assistant_message_id=assistant.id,
        backend_id=backend_id,
        model="qwen-local",
        state=AttemptState.RUNNING,
        request_snapshot=(
            snapshot
            if snapshot is not None
            else json.dumps(
                {
                    "attempt_id": "attempt",
                    "chat_id": chat.id,
                    "user_message_id": user.id,
                    "backend_id": backend_id,
                    "model": "qwen-local",
                    "prompt": user.content,
                    "provider_id": provider_id,
                    "base_url": "http://127.0.0.1:9000/v1",
                    "api_key_env": None,
                },
                sort_keys=True,
            )
        ),
        started_at=now,
        provider_id=provider_id,
        remote_outcome_unknown=False,
    )
    store.create_chat(chat)
    return store, chat, user, assistant, attempt


def _persist_fixture(store, chat, user, assistant, attempt) -> None:
    store.persist_generation_start(
        replace(chat, head_message_id=assistant.id, revision=1, updated_at=assistant.created_at),
        user,
        assistant,
        attempt,
        expected_chat_revision=0,
    )


@pytest.mark.parametrize(
    "snapshot_kind",
    [
        "empty",
        "malformed",
        "missing_base_url",
        "wrong_provider",
        "unknown_credential",
        "whitespace_url",
        "duplicate_key",
        "noncanonical_url",
        "malformed_host",
        "malformed_percent_escape",
    ],
)
def test_authoritative_store_rejects_invalid_phase3_request_snapshots(tmp_path: Path, snapshot_kind: str):
    snapshot_by_kind = {
        "empty": "{}",
        "malformed": "not-json",
        "duplicate_key": (
            '{"attempt_id":"attempt","chat_id":"chat","user_message_id":"user",'
            '"backend_id":"openai_compatible_http","model":"qwen-local",'
            '"prompt":"current user message","provider_id":"local_openai",'
            '"base_url":"http://127.0.0.1:9000/v1",'
            '"api_key_env":"DUPLICATE_SECRET_SENTINEL","api_key_env":null}'
        ),
    }
    store, chat, user, assistant, attempt = _store_attempt_fixture(
        tmp_path,
        snapshot=snapshot_by_kind.get(snapshot_kind),
    )
    if snapshot_kind == "missing_base_url":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot.pop("base_url")
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    elif snapshot_kind == "wrong_provider":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot["provider_id"] = "openrouter"
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    elif snapshot_kind == "unknown_credential":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot["access_token"] = "NOT_A_REAL_SECRET_AUDIT_SENTINEL"
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    elif snapshot_kind == "whitespace_url":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot["base_url"] = " http://127.0.0.1:9000/v1"
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    elif snapshot_kind == "noncanonical_url":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot["base_url"] = "HTTP://LOCALHOST:080/v1/../api"
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    elif snapshot_kind == "malformed_host":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot["base_url"] = "http://exa mple.invalid/v1"
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    elif snapshot_kind == "malformed_percent_escape":
        snapshot = json.loads(attempt.request_snapshot)
        snapshot["base_url"] = "http://example.invalid/%ZZ"
        attempt = replace(attempt, request_snapshot=json.dumps(snapshot, sort_keys=True))
    try:
        with pytest.raises(StateError, match="request snapshot"):
            _persist_fixture(store, chat, user, assistant, attempt)
        assert store.list_generation_attempts(chat.id) == ()
        assert store.list_messages(chat.id) == ()
    finally:
        store.close()


def test_authoritative_store_accepts_valid_phase3_request_snapshot(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        stored = store.list_generation_attempts(chat.id)
        assert len(stored) == 1
        assert json.loads(stored[0].request_snapshot)["base_url"] == "http://127.0.0.1:9000/v1"
    finally:
        store.close()


def test_authoritative_store_rejects_unknown_remote_outcome_truth_for_phase3(
    tmp_path: Path,
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(StateError, match="explicit remote outcome truth"):
            store.update_attempt(replace(attempt, remote_outcome_unknown=None))
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE generation_attempts "
                        "SET remote_outcome_unknown = NULL WHERE id = :id"
                    ),
                    {"id": attempt.id},
                )
        with pytest.raises(StateError, match="explicit remote outcome truth"):
            store.finalize_generation(
                replace(assistant, state=MessageState.ABORTED, content="partial text"),
                replace(
                    attempt,
                    state=AttemptState.ABORTED,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    error_type="aborted",
                    error_message="generation was cancelled",
                    remote_outcome_unknown=None,
                ),
            )
        assert store.list_generation_attempts(chat.id)[0].remote_outcome_unknown is False
    finally:
        store.close()


def test_sqlite_trigger_rejects_invalid_phase3_outcome_metadata(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(DatabaseError):
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE generation_attempts SET request_id = '' WHERE id = :id"
                    ),
                    {"id": attempt.id},
                )
    finally:
        store.close()


def test_historical_phase1_phase2_attempt_snapshot_remains_readable(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(
        tmp_path,
        snapshot="{}",
        provider_id=None,
        backend_id="fake",
    )
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        assert store.list_generation_attempts(chat.id)[0].request_snapshot == "{}"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("snapshot", "expected", "backend_id"),
    [
        (
            '{"base_url":"http://legacy.invalid/v1/",'
            '"legacy_field":"first","legacy_field":"last",'
            '"api_key":"legacy-placeholder"}',
            {
                "base_url": "http://legacy.invalid/v1/",
                "legacy_field": "last",
                "api_key": "legacy-placeholder",
            },
            "fake",
        ),
        (
            '{"provider_id":"legacy-provider","legacy_field":"harmless"}',
            {"provider_id": "legacy-provider", "legacy_field": "harmless"},
            "fake",
        ),
        (
            '{"base_url":"legacy opaque value","legacy_field":"harmless"}',
            {"base_url": "legacy opaque value", "legacy_field": "harmless"},
            "fake",
        ),
        (
            "{}",
            {},
            "openai_compatible_http",
        ),
    ],
    ids=[
        "trailing-slash-and-legacy-fields",
        "snapshot-provider-field",
        "opaque-base-url",
        "backend-id-collision",
    ],
)
def test_historical_phase1_phase2_snapshot_migrates_and_remains_readable(
    tmp_path: Path, snapshot: str, expected: dict[str, str], backend_id: str
):
    database = tmp_path / "legacy.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at) "
                    "VALUES ('chat', 'Legacy', '2026-09-04T00:00:00.000Z', "
                    "'2026-09-04T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, chat_id, parent_id, sequence, role, state, content, created_at) "
                    "VALUES ('user', 'chat', NULL, 1, 'user', 'sent', 'hello', "
                    "'2026-09-04T00:00:00.000Z'), "
                    "('assistant', 'chat', 'user', 2, 'assistant', 'complete', 'answer', "
                    "'2026-09-04T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO generation_attempts "
                    "(id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
                    "state, request_snapshot, started_at, ended_at) VALUES "
                    "('attempt', 'chat', 'user', 'assistant', :backend_id, 'fake-v0.1', 'complete', "
                    ":snapshot, "
                    "'2026-09-04T00:00:00.000Z', '2026-09-04T00:00:01.000Z')"
                ),
                {"backend_id": backend_id, "snapshot": snapshot},
            )
    finally:
        engine.dispose()

    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        assert json.loads(store.list_generation_attempts("chat")[0].request_snapshot) == expected
    finally:
        store.close()


def test_historical_backend_id_collision_reconciles_on_restart(tmp_path: Path):
    database = tmp_path / "legacy-running.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at) "
                    "VALUES ('chat', 'Legacy', '2026-09-05T00:00:00.000Z', "
                    "'2026-09-05T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, chat_id, parent_id, sequence, role, state, content, created_at) "
                    "VALUES ('user', 'chat', NULL, 1, 'user', 'sent', 'hello', "
                    "'2026-09-05T00:00:00.000Z'), "
                    "('assistant', 'chat', 'user', 2, 'assistant', 'streaming', 'partial', "
                    "'2026-09-05T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO generation_attempts "
                    "(id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
                    "state, request_snapshot, started_at) VALUES "
                    "('attempt', 'chat', 'user', 'assistant', 'openai_compatible_http', "
                    "'fake-v0.1', 'running', '{}', '2026-09-05T00:00:00.000Z')"
                )
            )
    finally:
        engine.dispose()

    upgrade_database(database)

    async def scenario():
        store = SQLiteAppStateStore.open(database)
        assert store.list_generation_attempts("chat")[0].state is AttemptState.RUNNING
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            store,
            EventBus(clock, ids, queue_size=8),
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        try:
            attempts = await application.list_generation_attempts("chat")
            assert attempts[0].state is AttemptState.ABORTED
            assert attempts[0].provider_id is None
            assert attempts[0].remote_outcome_unknown is True
            _, messages = await application.open_chat("chat")
            assert messages[-1].state is MessageState.ABORTED
            assert messages[-1].content == "partial"
        finally:
            await application.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("finish_reason", ["length", None, 42])
def test_authoritative_store_rejects_phase3_complete_without_exact_stop(
    tmp_path: Path, finish_reason: object
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        finalized_attempt = replace(
            attempt,
            state=AttemptState.COMPLETE,
            ended_at=attempt.started_at + timedelta(seconds=1),
            finish_reason=finish_reason,
        )
        finalized_message = replace(
            assistant,
            state=MessageState.COMPLETE,
            content="complete text",
        )
        with pytest.raises(StateError, match="finish_reason"):
            store.finalize_generation(finalized_message, finalized_attempt)
        assert store.list_generation_attempts(chat.id)[0].state is AttemptState.RUNNING
        assert store.list_messages(chat.id)[-1].state is MessageState.STREAMING
    finally:
        store.close()


def test_authoritative_store_accepts_exact_stop_completion(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        store.finalize_generation(
            replace(assistant, state=MessageState.COMPLETE, content="complete text"),
            replace(
                attempt,
                state=AttemptState.COMPLETE,
                ended_at=attempt.started_at + timedelta(seconds=1),
                finish_reason="stop",
            ),
        )
        stored = store.list_generation_attempts(chat.id)[0]
        assert stored.state is AttemptState.COMPLETE
        assert stored.finish_reason == "stop"
    finally:
        store.close()


def test_authoritative_store_accepts_non_stop_incomplete_terminal_state(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        store.finalize_generation(
            replace(assistant, state=MessageState.TRUNCATED, content="partial text"),
            replace(
                attempt,
                state=AttemptState.INCOMPLETE,
                ended_at=attempt.started_at + timedelta(seconds=1),
                finish_reason="length",
            ),
        )
        stored = store.list_generation_attempts(chat.id)[0]
        assert stored.state is AttemptState.INCOMPLETE
        assert stored.finish_reason == "length"
        assert store.list_messages(chat.id)[-1].state is MessageState.TRUNCATED
    finally:
        store.close()


def test_authoritative_store_rejects_known_finish_with_remote_unknown(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(StateError, match="unknown outcome"):
            store.finalize_generation(
                replace(assistant, state=MessageState.TRUNCATED, content="partial text"),
                replace(
                    attempt,
                    state=AttemptState.INCOMPLETE,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    finish_reason="length",
                    remote_outcome_unknown=True,
                ),
            )
        assert store.list_generation_attempts(chat.id)[0].state is AttemptState.RUNNING
    finally:
        store.close()


def test_authoritative_store_preserves_dispatch_uncertainty_for_running_and_aborted(
    tmp_path: Path,
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        dispatched = replace(attempt, remote_outcome_unknown=True)
        store.update_attempt(dispatched)
        with pytest.raises(StateError, match="uncertainty"):
            store.update_attempt(replace(dispatched, remote_outcome_unknown=False))
        with pytest.raises(StateError, match="uncertainty"):
            store.finalize_generation(
                replace(assistant, state=MessageState.ABORTED, content="partial text"),
                replace(
                    dispatched,
                    state=AttemptState.ABORTED,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    error_type="aborted",
                    error_message="generation was cancelled",
                    remote_outcome_unknown=False,
                ),
            )
        store.finalize_generation(
            replace(assistant, state=MessageState.ABORTED, content="partial text"),
            replace(
                dispatched,
                state=AttemptState.ABORTED,
                ended_at=attempt.started_at + timedelta(seconds=1),
                error_type="aborted",
                error_message="generation was cancelled",
                remote_outcome_unknown=True,
            ),
        )
        stored = store.list_generation_attempts(chat.id)[0]
        assert stored.state is AttemptState.ABORTED
        assert stored.remote_outcome_unknown is True
    finally:
        store.close()


def test_authoritative_store_requires_known_finish_to_resolve_incomplete_uncertainty(
    tmp_path: Path,
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        dispatched = replace(attempt, remote_outcome_unknown=True)
        store.update_attempt(dispatched)
        with pytest.raises(StateError, match="known terminal finish"):
            store.finalize_generation(
                replace(assistant, state=MessageState.INCOMPLETE, content="partial text"),
                replace(
                    dispatched,
                    state=AttemptState.INCOMPLETE,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    error_type="missing_terminal_event",
                    error_message="generation stream ended without a terminal event",
                    finish_reason=None,
                    remote_outcome_unknown=False,
                ),
            )
        stored = store.list_generation_attempts(chat.id)[0]
        assert stored.state is AttemptState.RUNNING
        assert stored.remote_outcome_unknown is True
    finally:
        store.close()


def test_authoritative_store_rejects_running_or_complete_failure_metadata(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(StateError, match="failure metadata"):
            store.update_attempt(replace(attempt, error_type="unexpected"))
        with pytest.raises(StateError, match="failure metadata"):
            store.finalize_generation(
                replace(assistant, state=MessageState.COMPLETE, content="complete text"),
                replace(
                    attempt,
                    state=AttemptState.COMPLETE,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    finish_reason="stop",
                    error_message="unexpected",
                ),
            )
        assert store.list_generation_attempts(chat.id)[0].state is AttemptState.RUNNING
    finally:
        store.close()


def test_authoritative_store_accepts_aborted_remote_uncertain_state(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        store.finalize_generation(
            replace(assistant, state=MessageState.ABORTED, content="partial text"),
            replace(
                attempt,
                state=AttemptState.ABORTED,
                ended_at=attempt.started_at + timedelta(seconds=1),
                remote_outcome_unknown=True,
            ),
        )
        stored = store.list_generation_attempts(chat.id)[0]
        assert stored.state is AttemptState.ABORTED
        assert stored.remote_outcome_unknown is True
        assert store.list_messages(chat.id)[-1].content == "partial text"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("attempt_state", "message_state"),
    [
        (AttemptState.FAILED, MessageState.FAILED),
        (AttemptState.ABORTED, MessageState.ABORTED),
    ],
)
def test_authoritative_store_rejects_stop_finish_for_failed_or_aborted_phase3(
    tmp_path: Path, attempt_state: AttemptState, message_state: MessageState
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(StateError, match="finish_reason"):
            store.finalize_generation(
                replace(assistant, state=message_state),
                replace(
                    attempt,
                    state=attempt_state,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    finish_reason="stop",
                ),
            )
        assert store.list_generation_attempts(chat.id)[0].state is AttemptState.RUNNING
    finally:
        store.close()


def test_authoritative_store_rejects_unknown_complete_outcome(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(StateError, match="unknown outcome"):
            store.finalize_generation(
                replace(assistant, state=MessageState.COMPLETE),
                replace(
                    attempt,
                    state=AttemptState.COMPLETE,
                    ended_at=attempt.started_at + timedelta(seconds=1),
                    finish_reason="stop",
                    remote_outcome_unknown=True,
                ),
            )
        assert store.list_generation_attempts(chat.id)[0].state is AttemptState.RUNNING
    finally:
        store.close()


def test_authoritative_store_rejects_finish_reason_on_running_phase3(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        with pytest.raises(StateError, match="finish_reason"):
            store.update_attempt(replace(attempt, finish_reason="stop"))
        assert store.list_generation_attempts(chat.id)[0].finish_reason is None
    finally:
        store.close()


def _tamper_current_schema(database: Path, statement: str, parameters: dict[str, object]) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER generation_attempt_validate_update")
        connection.execute("DROP TRIGGER generation_attempt_snapshot_immutable")
        connection.execute(statement, parameters)


def test_live_attempt_read_rejects_snapshot_prompt_contradiction(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    try:
        _persist_fixture(store, chat, user, assistant, attempt)
        database = tmp_path / "state.sqlite3"
        _tamper_current_schema(
            database,
            "UPDATE generation_attempts SET request_snapshot = :snapshot WHERE id = :id",
            {
                "snapshot": json.dumps(
                    {
                        **json.loads(attempt.request_snapshot),
                        "prompt": "different user message",
                    },
                    sort_keys=True,
                ),
                "id": attempt.id,
            },
        )
        with pytest.raises(StateError, match="prompt"):
            store.list_generation_attempts(chat.id)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", "openrouter"),
        ("backend_id", "different_backend"),
        ("model", "different-model"),
    ],
)
def test_current_schema_open_rejects_contradictory_phase3_provenance(
    tmp_path: Path, field: str, value: str
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    _tamper_current_schema(
        database,
        f"UPDATE generation_attempts SET {field} = :value WHERE id = :id",
        {"value": value, "id": attempt.id},
    )
    with pytest.raises(
        RuntimeError,
        match="invalid request snapshot|generation request snapshot|provider ID|backend ID",
    ):
        SQLiteAppStateStore.open(database)


@pytest.mark.parametrize("snapshot_field", ["model", "prompt"])
def test_current_schema_open_rejects_snapshot_provenance_contradiction(
    tmp_path: Path, snapshot_field: str
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    snapshot = json.loads(attempt.request_snapshot)
    snapshot[snapshot_field] = (
        "different-model" if snapshot_field == "model" else "different user message"
    )
    _tamper_current_schema(
        database,
        "UPDATE generation_attempts SET request_snapshot = :snapshot WHERE id = :id",
        {"snapshot": json.dumps(snapshot, sort_keys=True), "id": attempt.id},
    )
    with pytest.raises(RuntimeError, match="invalid request snapshot|generation request snapshot"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_malformed_phase3_host(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    snapshot = json.loads(attempt.request_snapshot)
    snapshot["base_url"] = "http://exa mple.invalid/v1"
    _tamper_current_schema(
        database,
        "UPDATE generation_attempts SET request_snapshot = :snapshot WHERE id = :id",
        {"snapshot": json.dumps(snapshot, sort_keys=True), "id": attempt.id},
    )
    with pytest.raises(RuntimeError, match="request snapshot|base URL"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_malformed_percent_escape(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    snapshot = json.loads(attempt.request_snapshot)
    snapshot["base_url"] = "http://example.invalid/%ZZ"
    _tamper_current_schema(
        database,
        "UPDATE generation_attempts SET request_snapshot = :snapshot WHERE id = :id",
        {"snapshot": json.dumps(snapshot, sort_keys=True), "id": attempt.id},
    )
    with pytest.raises(RuntimeError, match="request snapshot|base URL"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_erased_phase3_provider_identity(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    _tamper_current_schema(
        database,
        "UPDATE generation_attempts SET provider_id = NULL WHERE id = :id",
        {"id": attempt.id},
    )
    with pytest.raises(RuntimeError, match="provider ID|request snapshot"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_accepts_valid_phase3_state(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    reopened = SQLiteAppStateStore.open(database)
    try:
        assert reopened.list_generation_attempts(chat.id)[0].provider_id == "local_openai"
    finally:
        reopened.close()


def test_current_schema_open_rejects_missing_phase3_columns(tmp_path: Path):
    database = tmp_path / "missing-outcomes.sqlite3"
    _upgrade_to(database, "0004_integrity_guard_function")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = '0005_generation_outcomes'"
        )
    with pytest.raises(RuntimeError, match="missing generation outcome columns"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_non_nullable_phase3_columns(tmp_path: Path):
    database = tmp_path / "non-null-outcomes.sqlite3"
    _upgrade_to(database, "0004_integrity_guard_function")
    columns = [
        ("provider_id", "TEXT", "'local_openai'"),
        ("returned_model", "TEXT", "''"),
        ("request_id", "TEXT", "''"),
        ("finish_reason", "TEXT", "''"),
        ("prompt_tokens", "INTEGER", "0"),
        ("completion_tokens", "INTEGER", "0"),
        ("reasoning_tokens", "INTEGER", "0"),
        ("total_tokens", "INTEGER", "0"),
        ("known_cost_usd", "TEXT", "''"),
        ("remote_outcome_unknown", "INTEGER", "0"),
    ]
    with sqlite3.connect(database) as connection:
        for name, type_name, default in columns:
            connection.execute(
                f"ALTER TABLE generation_attempts ADD COLUMN {name} {type_name} "
                f"NOT NULL DEFAULT {default}"
            )
        connection.execute(
            "UPDATE alembic_version SET version_num = '0005_generation_outcomes'"
        )
    with pytest.raises(RuntimeError, match="must be nullable"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_wrong_phase3_column_types(tmp_path: Path):
    database = tmp_path / "wrong-outcome-types.sqlite3"
    _upgrade_to(database, "0004_integrity_guard_function")
    columns = [
        ("provider_id", "VARCHAR(64)"),
        ("returned_model", "TEXT"),
        ("request_id", "TEXT"),
        ("finish_reason", "VARCHAR(128)"),
        ("prompt_tokens", "TEXT"),
        ("completion_tokens", "INTEGER"),
        ("reasoning_tokens", "INTEGER"),
        ("total_tokens", "INTEGER"),
        ("known_cost_usd", "TEXT"),
        ("remote_outcome_unknown", "BOOLEAN"),
    ]
    with sqlite3.connect(database) as connection:
        for name, type_name in columns:
            connection.execute(
                f"ALTER TABLE generation_attempts ADD COLUMN {name} {type_name}"
            )
        connection.execute(
            "UPDATE alembic_version SET version_num = '0005_generation_outcomes'"
        )
    with pytest.raises(RuntimeError, match="invalid declared types"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_contradictory_phase3_outcome_state(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    _tamper_current_schema(
        database,
        "UPDATE generation_attempts SET finish_reason = 'stop' WHERE id = :id",
        {"id": attempt.id},
    )
    with pytest.raises(RuntimeError, match="finish reason|finish_reason|outcome"):
        SQLiteAppStateStore.open(database)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE generation_attempts SET finish_reason = 'length', remote_outcome_unknown = 1 WHERE id = :id",
        "UPDATE generation_attempts SET error_type = 'unexpected' WHERE id = :id",
    ],
)
def test_current_schema_open_rejects_contradictory_phase3_outcome_metadata(
    tmp_path: Path, statement: str
):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    _tamper_current_schema(database, statement, {"id": attempt.id})
    with pytest.raises(RuntimeError, match="outcome|unknown|failure metadata"):
        SQLiteAppStateStore.open(database)


def test_current_schema_open_rejects_duplicate_phase3_snapshot_keys(tmp_path: Path):
    store, chat, user, assistant, attempt = _store_attempt_fixture(tmp_path)
    _persist_fixture(store, chat, user, assistant, attempt)
    database = tmp_path / "state.sqlite3"
    store.close()
    duplicate_snapshot = (
        '{"attempt_id":"attempt","chat_id":"chat","user_message_id":"user",'
        '"backend_id":"openai_compatible_http","model":"qwen-local",'
        '"prompt":"current user message","provider_id":"local_openai",'
        '"base_url":"http://127.0.0.1:9000/v1",'
        '"api_key_env":"DUPLICATE_SECRET_SENTINEL","api_key_env":null}'
    )
    _tamper_current_schema(
        database,
        "UPDATE generation_attempts SET request_snapshot = :snapshot WHERE id = :id",
        {"snapshot": duplicate_snapshot, "id": attempt.id},
    )
    with pytest.raises(RuntimeError, match="request snapshot|duplicate"):
        SQLiteAppStateStore.open(database)


def test_streaming_backend_uses_one_ordinary_stream_request_and_persists_telemetry(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("PHASE3_LOCAL_KEY", "secret-value-that-must-not-persist")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        lines = [
            {
                "id": "local-request",
                "model": "qwen-local-returned",
                "choices": [{"delta": {"content": "hello"}, "finish_reason": None}],
            },
            {
                "choices": [
                    {"delta": {"content": " world"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                    "cost": "0.0012",
                },
            },
            {"choices": [], "usage": {"total_tokens": 6}},
        ]
        body = "".join(f"data: {json.dumps(line)}\n\n" for line in lines)
        body += "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9000/v1/",
        api_key_env="PHASE3_LOCAL_KEY",
        _transport=httpx.MockTransport(handler),
    )
    backend = OpenAICompatibleStreamingBackend(
        provider,
        provider_id="local_openai",
        base_url=provider.base_url,
        api_key_env=provider.api_key_env,
    )

    async def scenario():
        application = _application(
            tmp_path,
            backend,
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
        )
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            returned = await application.send_message(chat.id, "exact current message")
            kinds = await _terminal(subscription, returned.id)
            attempt = (await application.list_generation_attempts(chat.id))[0]
            _, messages = await application.open_chat(chat.id)
        finally:
            await application.close()

        snapshot = json.loads(attempt.request_snapshot)
        assert kinds[:3] == ["message_sent", "generation_started", "generation_dispatched"]
        assert messages[-1].state == MessageState.COMPLETE
        assert messages[-1].content == "hello world"
        assert attempt.state == AttemptState.COMPLETE
        assert attempt.provider_id == "local_openai"
        assert attempt.backend_id == "openai_compatible_http"
        assert attempt.finish_reason == "stop"
        assert attempt.returned_model == "qwen-local-returned"
        assert attempt.request_id == "local-request"
        assert attempt.prompt_tokens == 4
        assert attempt.completion_tokens == 2
        assert attempt.total_tokens == 6
        assert str(attempt.known_cost_usd) == "0.0012"
        assert attempt.remote_outcome_unknown is False
        assert snapshot["base_url"] == "http://127.0.0.1:9000/v1"
        assert snapshot["api_key_env"] == "PHASE3_LOCAL_KEY"
        assert snapshot["prompt"] == "exact current message"
        assert "secret-value-that-must-not-persist" not in attempt.request_snapshot
        payload = seen["payload"]
        assert payload["stream"] is True
        assert payload["messages"] == [
            {"role": "user", "content": "exact current message"}
        ]
        assert "stream_options" not in payload
        assert seen["url"] == "http://127.0.0.1:9000/v1/chat/completions"
        assert seen["headers"]["authorization"] == "Bearer secret-value-that-must-not-persist"

    asyncio.run(scenario())


def test_missing_stream_usage_remains_unknown(tmp_path: Path):
    async def handler(request: httpx.Request):
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
    )
    backend = OpenAICompatibleStreamingBackend(
        provider,
        provider_id="local_openai",
        base_url=provider.base_url,
    )

    async def scenario():
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            returned = await application.send_message(chat.id, "hello")
            await _terminal(subscription, returned.id)
            attempt = (await application.list_generation_attempts(chat.id))[0]
            assert attempt.prompt_tokens is None
            assert attempt.completion_tokens is None
            assert attempt.total_tokens is None
            assert attempt.known_cost_usd is None
        finally:
            await application.close()

    asyncio.run(scenario())


def test_out_of_range_stream_usage_remains_unknown_without_stranding_generation(
    tmp_path: Path,
):
    async def handler(request: httpx.Request):
        line = {
            "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10**100},
        }
        return httpx.Response(
            200,
            text=f"data: {json.dumps(line)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
    )
    backend = OpenAICompatibleStreamingBackend(
        provider,
        provider_id="local_openai",
        base_url=provider.base_url,
    )

    async def scenario():
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            assert stored.state is AttemptState.COMPLETE
            assert stored.finish_reason == "stop"
            assert stored.prompt_tokens is None
        finally:
            await application.close()

    asyncio.run(scenario())


def test_streaming_provider_rejects_empty_finish_reason():
    async def handler(request: httpx.Request):
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":""}]}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
    )

    async def scenario():
        with pytest.raises(ProviderResponseError, match="malformed"):
            async for _ in provider.stream(
                CompletionRequest(
                    model="qwen-local",
                    system="",
                    user="hello",
                    temperature=0.0,
                    max_output_tokens=16,
                    timeout_seconds=0.0,
                )
            ):
                pass

    asyncio.run(scenario())


def test_provider_normalizes_effective_base_url_before_dispatch():
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    provider = OpenAICompatibleProvider(
        "HTTP://LOCALHOST:080/v1/../api",
        _transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.complete(
        CompletionRequest(
            model="qwen-local",
            system="",
            user="hello",
            temperature=0.0,
            max_output_tokens=16,
            timeout_seconds=0.0,
        )
    ))
    assert result.output_text == "ok"
    assert provider.base_url == "http://localhost/api"
    assert seen["url"] == "http://localhost/api/chat/completions"


@pytest.mark.parametrize("provider_kind", ["local_openai", "openrouter"])
def test_nonstreaming_provider_preserves_empty_system_message(provider_kind: str):
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    if provider_kind == "local_openai":
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
        )
    else:
        provider = OpenRouterProvider(
            "test-secret",
            _transport=httpx.MockTransport(handler),
        )
    asyncio.run(
        provider.complete(
            CompletionRequest(
                model="qwen-local",
                system="",
                user="hello",
                temperature=0.0,
                max_output_tokens=16,
                timeout_seconds=0.0,
            )
        )
    )
    assert seen["payload"] == {
        "model": "qwen-local",
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": "hello"},
        ],
        "temperature": 0.0,
        "max_tokens": 16,
        "stream": False,
    }


def test_provider_idna_normalizes_effective_base_url_before_dispatch():
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    provider = OpenAICompatibleProvider(
        "http://éxample.invalid/v1",
        _transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.complete(
        CompletionRequest(
            model="qwen-local",
            system="",
            user="hello",
            temperature=0.0,
            max_output_tokens=16,
            timeout_seconds=0.0,
        )
    ))
    assert result.output_text == "ok"
    assert provider.base_url == "http://xn--xample-9ua.invalid/v1"
    assert seen["url"] == "http://xn--xample-9ua.invalid/v1/chat/completions"


class _KnownIncompleteBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        yield GenerationCompleted(request.attempt_id, finish_reason="length")


class _MalformedFinishBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        yield GenerationCompleted(request.attempt_id, finish_reason="")


class _ContradictoryTerminalBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        yield GenerationCompleted(
            request.attempt_id,
            finish_reason="length",
            remote_outcome_unknown=True,
        )


class _InvalidTerminalMetadataBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        yield GenerationCompleted(
            request.attempt_id,
            finish_reason="stop",
            prompt_tokens=-1,
        )


def test_malformed_terminal_finish_becomes_durable_failure(tmp_path: Path):
    async def scenario():
        application = _application(tmp_path, _MalformedFinishBackend())
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            kinds = await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            _, messages = await application.open_chat(chat.id)
            assert kinds[-1] == "generation_failed"
            assert stored.state is AttemptState.FAILED
            assert stored.finish_reason is None
            assert messages[-1].state is MessageState.FAILED
            assert messages[-1].content == "partial"
        finally:
            await application.close()

        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            assert reopened.list_generation_attempts(chat.id)[0].state is AttemptState.FAILED
            assert reopened.list_messages(chat.id)[-1].state is MessageState.FAILED
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_rejected_terminal_metadata_becomes_durable_failure(tmp_path: Path):
    async def scenario():
        application = _application(tmp_path, _ContradictoryTerminalBackend())
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            kinds = await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            _, messages = await application.open_chat(chat.id)
            assert kinds[-1] == "generation_failed"
            assert stored.state is AttemptState.FAILED
            assert stored.finish_reason is None
            assert stored.remote_outcome_unknown is True
            assert stored.error_type == "StateError"
            assert messages[-1].state is MessageState.FAILED
            assert messages[-1].content == "partial"
        finally:
            await application.close()

        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            assert reopened.list_generation_attempts(chat.id)[0].state is AttemptState.FAILED
            assert reopened.list_generation_attempts(chat.id)[0].finish_reason is None
            assert reopened.list_messages(chat.id)[-1].state is MessageState.FAILED
            assert reopened.list_messages(chat.id)[-1].content == "partial"
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_rejected_terminal_telemetry_falls_back_to_durable_failure(tmp_path: Path):
    async def scenario():
        application = _application(tmp_path, _InvalidTerminalMetadataBackend())
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            kinds = await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            _, messages = await application.open_chat(chat.id)
            assert kinds[-1] == "generation_failed"
            assert stored.state is AttemptState.FAILED
            assert stored.finish_reason is None
            assert stored.prompt_tokens is None
            assert stored.remote_outcome_unknown is True
            assert messages[-1].state is MessageState.FAILED
            assert messages[-1].content == "partial"
        finally:
            await application.close()

        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            stored = reopened.list_generation_attempts(chat.id)[0]
            assert stored.state is AttemptState.FAILED
            assert stored.prompt_tokens is None
            assert reopened.list_messages(chat.id)[-1].content == "partial"
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_empty_streamed_stop_becomes_durable_failure(tmp_path: Path):
    async def handler(request: httpx.Request):
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async def scenario():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
        )
        backend = OpenAICompatibleStreamingBackend(
            provider,
            provider_id="local_openai",
            base_url=provider.base_url,
        )
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            kinds = await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            _, messages = await application.open_chat(chat.id)
            assert kinds[-1] == "generation_failed"
            assert stored.state is AttemptState.FAILED
            assert stored.error_type == "empty_model_response"
            assert stored.finish_reason is None
            assert messages[-1].state is MessageState.FAILED
            assert messages[-1].content == ""
        finally:
            await application.close()

    asyncio.run(scenario())


def test_conflicting_stream_finish_reasons_fail_closed(tmp_path: Path):
    async def handler(request: httpx.Request):
        body = "".join(
            [
                'data: {"choices":[{"delta":{"content":"partial"},'
                '"finish_reason":"length"}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    async def scenario():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
        )
        backend = OpenAICompatibleStreamingBackend(
            provider,
            provider_id="local_openai",
            base_url=provider.base_url,
        )
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            kinds = await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            _, messages = await application.open_chat(chat.id)
            assert kinds[-1] == "generation_failed"
            assert stored.state is AttemptState.FAILED
            assert stored.finish_reason is None
            assert stored.remote_outcome_unknown is True
            assert messages[-1].state is MessageState.FAILED
            assert messages[-1].content == "partial"
        finally:
            await application.close()

        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            stored = reopened.list_generation_attempts(chat.id)[0]
            assert stored.state is AttemptState.FAILED
            assert stored.finish_reason is None
            assert reopened.list_messages(chat.id)[-1].content == "partial"
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_known_non_stop_finish_is_not_marked_remotely_unknown(tmp_path: Path):
    async def scenario():
        application = _application(tmp_path, _KnownIncompleteBackend())
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await _terminal(subscription, attempt.id)
            stored = (await application.list_generation_attempts(chat.id))[0]
            assert stored.state is AttemptState.INCOMPLETE
            assert stored.finish_reason == "length"
            assert stored.remote_outcome_unknown is False
        finally:
            await application.close()

    asyncio.run(scenario())


class _PreDispatchBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    def __init__(self):
        self.started = asyncio.Event()

    async def stream(self, request: GenerationRequest):
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield GenerationDelta(request.attempt_id, "unreachable")


def test_pre_dispatch_cancellation_is_not_remotely_unknown(tmp_path: Path):
    async def scenario():
        backend = _PreDispatchBackend()
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await backend.started.wait()
            aborted = await application.cancel_generation(attempt.id)
            assert aborted.state is AttemptState.ABORTED
            assert aborted.remote_outcome_unknown is False
            await _terminal(subscription, attempt.id)
        finally:
            await application.close()

    asyncio.run(scenario())


def test_invalid_cost_is_rejected_before_persistence(tmp_path: Path):
    async def handler(request: httpx.Request):
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async def scenario():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9000/v1", _transport=httpx.MockTransport(handler)
        )
        backend = OpenAICompatibleStreamingBackend(
            provider,
            provider_id="local_openai",
            base_url=provider.base_url,
        )
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await _terminal(subscription, attempt.id)
            with pytest.raises(StateError, match="invalid cost"):
                application._store.update_attempt(
                    replace(attempt, known_cost_usd=Decimal("-1"))
                )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_invalid_cost_is_rejected_by_sqlite_trigger(tmp_path: Path):
    async def scenario():
        backend = _PreDispatchBackend()
        application = _application(tmp_path, backend)
        try:
            chat = await application.create_chat()
            attempt = await application.send_message(chat.id, "hello")
            await backend.started.wait()
            with pytest.raises(DatabaseError):
                with application._store.engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE generation_attempts SET known_cost_usd = '-1' "
                            "WHERE id = :id"
                        ),
                        {"id": attempt.id},
                    )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_stream_metadata_is_durable_before_delta_and_survives_abort(tmp_path: Path):
    async def scenario():
        release = asyncio.Event()

        class Provider:
            async def stream(self, request: CompletionRequest):
                yield CompletionStreamEvent(
                    text="partial",
                    returned_model="returned-qwen",
                    request_id="request-123",
                )
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    return

        provider = Provider()
        backend = OpenAICompatibleStreamingBackend(
            provider,
            provider_id="local_openai",
            base_url="http://127.0.0.1:9000/v1",
        )
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            while True:
                event = await subscription.__anext__()
                if event.kind == "message_delta" and event.payload["attempt_id"] == attempt.id:
                    break
            during = (await application.list_generation_attempts(chat.id))[0]
            assert during.returned_model == "returned-qwen"
            assert during.request_id == "request-123"
            aborted = await application.cancel_generation(attempt.id)
            assert aborted.state is AttemptState.ABORTED
            assert aborted.returned_model == "returned-qwen"
            assert aborted.request_id == "request-123"
        finally:
            await application.close()

    asyncio.run(scenario())


def test_cleanly_consumed_cancellation_still_aborts(tmp_path: Path):
    class CleanCancelBackend:
        backend_id = "openai_compatible_http"
        provider_id = "local_openai"
        base_url = "http://127.0.0.1:9000/v1"
        api_key_env = None

        def __init__(self):
            self.started = asyncio.Event()

        async def stream(self, request: GenerationRequest):
            yield GenerationDispatched(request.attempt_id)
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    async def scenario():
        backend = CleanCancelBackend()
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await backend.started.wait()
            aborted = await application.cancel_generation(attempt.id)
            assert aborted.state is AttemptState.ABORTED
            assert aborted.remote_outcome_unknown is True
            await _terminal(subscription, attempt.id)
        finally:
            await application.close()

    asyncio.run(scenario())


class _BackpressuredCancelBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    def __init__(self):
        self.delta_started = asyncio.Event()

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        self.delta_started.set()
        yield GenerationDelta(request.attempt_id, "partial")
        await asyncio.Event().wait()


def test_cancellation_returns_after_durable_abort_under_full_event_backpressure(tmp_path: Path):
    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        backend = _BackpressuredCancelBackend()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids, queue_size=3),
            backend,
            ids=ids,
            clock=clock,
            backend_id=backend.backend_id,
            model="qwen-local",
            provider_id=backend.provider_id,
            base_url=backend.base_url,
        )
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await backend.delta_started.wait()

            aborted = await asyncio.wait_for(
                application.cancel_generation(attempt.id),
                timeout=1,
            )
            assert aborted.state is AttemptState.ABORTED
            assert aborted.remote_outcome_unknown is True
            stored = (await application.list_generation_attempts(chat.id))[0]
            assert stored.state is AttemptState.ABORTED

            terminal = None
            for _ in range(4):
                event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
                if event.kind == "generation_aborted":
                    terminal = event
            assert terminal is not None
        finally:
            await application.close()

    asyncio.run(scenario())


class _LateCancellationBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    def __init__(self):
        self.completed = asyncio.Event()

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        self.completed.set()
        yield GenerationCompleted(request.attempt_id)


def test_late_cancellation_does_not_drop_durable_terminal_event(tmp_path: Path):
    async def scenario():
        database = tmp_path / "state.sqlite3"
        upgrade_database(database)
        ids = Uuid7Factory()
        clock = SystemClock()
        backend = _LateCancellationBackend()
        application = BotsApplication(
            SQLiteAppStateStore.open(database),
            EventBus(clock, ids, queue_size=4),
            backend,
            ids=ids,
            clock=clock,
            backend_id=backend.backend_id,
            model="qwen-local",
            provider_id=backend.provider_id,
            base_url=backend.base_url,
        )
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await backend.completed.wait()

            for _ in range(100):
                stored = (await application.list_generation_attempts(chat.id))[0]
                if stored.state is AttemptState.COMPLETE:
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("completion was not durable")

            cancelled = await asyncio.wait_for(
                application.cancel_generation(attempt.id),
                timeout=1,
            )
            assert cancelled.state is AttemptState.COMPLETE
            assert cancelled.finish_reason == "stop"

            terminal = None
            for _ in range(5):
                event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
                if event.kind == "generation_completed":
                    terminal = event
            assert terminal is not None
        finally:
            await application.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "base_url",
    [
        "https://url-user:url-password@example.invalid/v1",
        "https://openrouter.ai/api/v1?token=url-secret",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai/api/v1?",
        "https://openrouter.ai/api/v1#",
    ],
)
def test_openrouter_rejects_secret_bearing_or_non_normalized_base_url(base_url):
    with pytest.raises(ProviderError, match="HTTP/HTTPS"):
        OpenRouterProvider("test-secret", base_url=base_url)


def test_terminal_outcome_metadata_is_immutable(tmp_path: Path):
    async def scenario():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9000/v1",
            _transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )
        backend = OpenAICompatibleStreamingBackend(
            provider,
            provider_id="local_openai",
            base_url=provider.base_url,
        )
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            attempt = await application.send_message(chat.id, "hello")
            await _terminal(subscription, attempt.id)
            with pytest.raises(DatabaseError):
                with application._store.engine.begin() as connection:
                    connection.execute(
                        text("UPDATE generation_attempts SET request_id = 'tampered' WHERE id = :id"),
                        {"id": attempt.id},
                    )
        finally:
            await application.close()

    asyncio.run(scenario())


class _LateDeltaBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    def __init__(self):
        self.partial_persisted = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        self.partial_persisted.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            yield GenerationDelta(request.attempt_id, "late")
            yield GenerationCompleted(request.attempt_id)
        finally:
            self.closed.set()


def test_explicit_cancellation_aborts_and_preserves_partial_output(tmp_path: Path):
    async def scenario():
        backend = _LateDeltaBackend()
        application = _application(tmp_path, backend)
        subscription = application.subscribe()
        try:
            chat = await application.create_chat()
            await subscription.__anext__()
            returned = await application.send_message(chat.id, "hello")
            await backend.partial_persisted.wait()
            aborted = await application.cancel_generation(returned.id)
            assert aborted.state == AttemptState.ABORTED
            assert aborted.remote_outcome_unknown is True
            _, messages = await application.open_chat(chat.id)
            assert messages[-1].state == MessageState.ABORTED
            assert messages[-1].content == "partial"
            kinds = await _terminal(subscription, returned.id)
            assert kinds[-1] == "generation_aborted"
            assert "message_delta" in kinds
            assert backend.closed.is_set()
        finally:
            await application.close()

    asyncio.run(scenario())
