"""Real persistence and projection coverage for Phase 8 inspection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bots5.core.application import BotsApplication
from bots5.domain.models import WorkspaceWindowState
from tests._authority_test_support import SQLiteAppStateStore, upgrade_to
from tests.test_phase5_provider_model import _configured_application as phase5_application
from tests.test_phase6_context_attachments import (
    _configured_application as phase6_application,
    _finish,
)


PHASE7_HEAD = "0010_phase7_search_navigation"
PHASE8_HEAD = "0011_phase8_inspector_state"


def _fields(projection) -> dict[str, str]:
    return {field.name: field.value for field in projection.fields}


async def _complete(application: BotsApplication, attempt_id: str) -> None:
    await _finish(application, attempt_id)


def test_0010_upgrade_reopens_with_safe_inspector_defaults(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_to(database, PHASE7_HEAD)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO workspace_windows "
            "(window_id, ordinal, geometry_json, selected_chat_id, rail_collapsed, "
            "restore_open, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("window", 4, "[10,20,800,600]", None, 1, 1, "2026-09-14T00:00:00Z"),
        )

    store = SQLiteAppStateStore.open(database)
    try:
        state = store.list_workspace_windows()
        assert state == (
            WorkspaceWindowState(
                window_id="window",
                ordinal=4,
                geometry=(10, 20, 800, 600),
                selected_chat_id=None,
                rail_collapsed=True,
                restore_open=True,
                updated_at=state[0].updated_at,
            ),
        )
    finally:
        store.close()

    reopened = SQLiteAppStateStore.open(database)
    try:
        assert reopened.list_workspace_windows() == state
    finally:
        reopened.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PHASE8_HEAD,
        )
        assert connection.execute(
            "SELECT inspector_open, inspector_message_id, inspector_leaf_message_id "
            "FROM workspace_windows"
        ).fetchone() == (0, None, None)


def test_workspace_inspector_state_persists_across_reopen(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_to(database, PHASE8_HEAD)
    expected = WorkspaceWindowState(
        window_id="window",
        ordinal=4,
        geometry=(10, 20, 800, 600),
        selected_chat_id=None,
        rail_collapsed=True,
        restore_open=True,
        updated_at=datetime(2026, 9, 14, tzinfo=UTC),
        inspector_open=True,
        inspector_message_id="assistant",
        inspector_leaf_message_id="historical-leaf",
    )

    store = SQLiteAppStateStore.open(database)
    try:
        store.save_workspace_window(expected)
    finally:
        store.close()

    reopened = SQLiteAppStateStore.open(database)
    try:
        assert reopened.list_workspace_windows() == (expected,)
    finally:
        reopened.close()


def test_real_v2_inspection_retains_provider_model_and_settings_truth(tmp_path: Path):
    async def scenario() -> None:
        application, store = phase5_application(tmp_path)
        try:
            chat = await application.create_chat("v2 inspection")
            model = store.list_model_catalogue_entries()[0]
            await application.select_model(chat.id, model.id)
            attempt = await application.send_message(chat.id, "frozen v2 request")
            await _complete(application, attempt.id)
            projection = await application.inspect_chat(
                chat.id, message_id=attempt.assistant_message_id
            )
            fields = _fields(projection)
            snapshot = json.loads(attempt.request_snapshot)
            assert projection.status == "available"
            assert fields["Attempt 1 Snapshot"] == "v2"
            assert fields["Attempt 1 Frozen model"] == "fake-v0.1"
            assert fields["Attempt 1 Settings provenance"] != "not recorded"
            assert fields["Attempt 1 Capabilities"] != "not recorded"
        finally:
            await application.close()

    asyncio.run(scenario())


def test_real_v3_inspection_retains_context_and_attachment_provenance(tmp_path: Path):
    async def scenario() -> None:
        application, store, authority = phase6_application(tmp_path / "root")
        try:
            source = tmp_path / "evidence.txt"
            source.write_text("phase eight provenance", encoding="utf-8")
            attachment = store.ingest_attachment(source)
            chat = await application.create_chat("v3 inspection")
            await application.stage_attachment(chat.id, attachment.id)
            attempt = await application.send_message(chat.id, "frozen v3 request")
            await _complete(application, attempt.id)
            projection = await application.inspect_chat(
                chat.id,
                message_id=attempt.assistant_message_id,
                historical_leaf_message_id=attempt.assistant_message_id,
            )
            fields = _fields(projection)
            snapshot = json.loads(attempt.request_snapshot)
            assert projection.status == "available"
            assert projection.historical_leaf_message_id == attempt.assistant_message_id
            assert fields["Attempt 1 Snapshot"] == "v3"
            assert fields["Attempt 1 Context digest"] == snapshot["context_plan"]["canonical_digest"]
            assert attachment.filename in fields["Attempt 1 attachment 1"]
            assert attachment.blob_digest in fields["Attempt 1 attachment 1"]
            stale = await application.inspect_chat(
                chat.id,
                message_id=attempt.assistant_message_id,
                historical_leaf_message_id="deleted-historical-leaf",
            )
            assert stale.historical_leaf_message_id is None
        finally:
            await application.close()
            if not store.closed:
                authority.close()

    asyncio.run(scenario())


def test_inspection_reads_attachment_metadata_without_opening_user_or_attempt_payloads(
    tmp_path: Path, monkeypatch
):
    async def scenario() -> None:
        application, store, authority = phase6_application(tmp_path / "root")
        try:
            source = tmp_path / "inspection-metadata.txt"
            source.write_text("metadata only", encoding="utf-8")
            attachment = store.ingest_attachment(source)
            chat = await application.create_chat("inspection metadata")
            await application.stage_attachment(chat.id, attachment.id)
            attempt = await application.send_message(chat.id, "inspect attachment")
            await _complete(application, attempt.id)

            payload_reads: list[tuple[object, object]] = []

            def payload_read(_manager, *args, **kwargs):
                payload_reads.append((args, kwargs))
                raise AssertionError("Inspector opened attachment payload bytes")

            monkeypatch.setattr(type(store._attachment_manager), "read_verified", payload_read)

            user_projection = await application.inspect_chat(
                chat.id, message_id=attempt.user_message_id
            )
            assistant_projection = await application.inspect_chat(
                chat.id, message_id=attempt.assistant_message_id
            )

            user_fields = _fields(user_projection)
            assistant_fields = _fields(assistant_projection)
            assert attachment.filename in user_fields["Message attachment 1"]
            assert attachment.blob_digest in user_fields["Attempt 1 attachment 1"]
            assert attachment.filename in assistant_fields["Attempt 1 attachment 1"]
            assert payload_reads == []
        finally:
            await application.close()
            if not store.closed:
                authority.close()

    asyncio.run(scenario())


def test_inspection_binds_message_and_attempts_to_resolved_historical_or_active_branch(
    tmp_path: Path,
):
    async def scenario() -> None:
        application, store = phase5_application(tmp_path)
        try:
            chat = await application.create_chat("branch-bound inspection")
            model = store.list_model_catalogue_entries()[0]
            await application.select_model(chat.id, model.id)
            original = await application.send_message(chat.id, "original branch")
            await _complete(application, original.id)
            edited = await application.edit_message(
                chat.id, original.user_message_id, "edited branch"
            )
            await _complete(application, edited.id)

            incoherent = await application.inspect_chat(
                chat.id,
                message_id=edited.assistant_message_id,
                historical_leaf_message_id=original.assistant_message_id,
            )
            incoherent_fields = _fields(incoherent)
            incoherent_values = {field.value for field in incoherent.fields}
            assert incoherent.historical_leaf_message_id is None
            assert incoherent.selected_message_id == edited.assistant_message_id
            assert incoherent_fields["Attempt 1 ID"] == edited.id
            assert edited.id in incoherent_values
            assert original.id not in incoherent_values

            exact_historical = await application.inspect_chat(
                chat.id,
                message_id=original.assistant_message_id,
                historical_leaf_message_id=original.assistant_message_id,
            )
            historical_fields = _fields(exact_historical)
            assert exact_historical.selected_message_id == original.assistant_message_id
            assert historical_fields["Attempt 1 ID"] == original.id

            exact_active = await application.inspect_chat(
                chat.id, message_id=edited.assistant_message_id
            )
            active_fields = _fields(exact_active)
            assert exact_active.historical_leaf_message_id is None
            assert exact_active.selected_message_id == edited.assistant_message_id
            assert active_fields["Attempt 1 ID"] == edited.id

            stale = await application.inspect_chat(
                chat.id,
                message_id=original.assistant_message_id,
                historical_leaf_message_id="deleted-historical-leaf",
            )
            stale_values = {field.value for field in stale.fields}
            assert stale.historical_leaf_message_id is None
            assert stale.selected_message_id is None
            assert edited.id in stale_values
            assert original.id in stale_values
        finally:
            await application.close()

    asyncio.run(scenario())


def test_inspection_filters_shared_user_attempts_to_the_resolved_regeneration_branch(
    tmp_path: Path,
):
    async def scenario() -> None:
        application, store = phase5_application(tmp_path)
        try:
            chat = await application.create_chat("shared-user regeneration inspection")
            model = store.list_model_catalogue_entries()[0]
            await application.select_model(chat.id, model.id)
            original = await application.send_message(chat.id, "shared user")
            await _complete(application, original.id)
            regenerated = await application.regenerate_message(
                chat.id, original.assistant_message_id
            )
            await _complete(application, regenerated.id)

            historical_user = await application.inspect_chat(
                chat.id,
                message_id=original.user_message_id,
                historical_leaf_message_id=original.assistant_message_id,
            )
            active_user = await application.inspect_chat(
                chat.id, message_id=original.user_message_id
            )
            historical_assistant = await application.inspect_chat(
                chat.id,
                message_id=original.assistant_message_id,
                historical_leaf_message_id=original.assistant_message_id,
            )
            active_assistant = await application.inspect_chat(
                chat.id, message_id=regenerated.assistant_message_id
            )
            chat_history = await application.inspect_chat(chat.id)

            historical_fields = _fields(historical_user)
            active_fields = _fields(active_user)
            historical_assistant_fields = _fields(historical_assistant)
            active_assistant_fields = _fields(active_assistant)
            history_values = {field.value for field in chat_history.fields}

            assert historical_fields["Attempt 1 ID"] == original.id
            assert "Attempt 2 ID" not in historical_fields
            assert active_fields["Attempt 1 ID"] == regenerated.id
            assert "Attempt 2 ID" not in active_fields
            assert historical_assistant_fields["Attempt 1 ID"] == original.id
            assert active_assistant_fields["Attempt 1 ID"] == regenerated.id
            assert original.id in history_values
            assert regenerated.id in history_values
        finally:
            await application.close()

    asyncio.run(scenario())
