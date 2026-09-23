from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, text

from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.models import Chat
from tests._authority_test_support import SQLiteAppStateStore, upgrade_database


def test_real_migration_creates_state_schema_and_enables_sqlite_safety(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        with store.command_admission():
            assert set(inspect(store.engine).get_table_names()) == {
                "alembic_version",
                "application_generation_config",
                "capability_facts",
                "capability_observations",
                "capability_overrides",
                "chats",
                "chat_model_generation_config",
                "chat_model_selection",
                "generation_attempts",
                "messages",
                "model_catalogue_entries",
                "model_generation_config",
                "provider_connections",
                "catalogue_refresh_state",
                "attachment_blobs",
                "attachments",
                "message_attachments",
                "attempt_attachments",
                "archive_continuation_anchors",
                "archive_continuation_branches",
                "archive_continuation_choices",
                "archive_continuation_requirement_candidates",
                "archive_continuation_requirements",
                "archive_import_attachment_refs",
                "archive_import_attempt_attachment_refs",
                "archive_import_chats",
                "archive_import_journal",
                "archive_import_lineages",
                "archive_import_message_attachment_refs",
                "archive_import_messages",
                "archive_import_operations",
                "archive_import_payload_reservations",
                "archive_import_queue",
                "archive_import_queue_control",
                "archive_imported_attempts",
                "archive_imported_branch_choices",
                "archive_imported_context_plans",
                "archive_lineage_nodes",
                "archive_object_derivations",
                "context_plans",
                "workspace_windows",
                "search_source_state",
                "search_index_state",
                "search_document_keys",
                "search_fts",
                "search_fts_config",
                "search_fts_content",
                "search_fts_data",
                "search_fts_docsize",
                "search_fts_idx",
            }
            with store.engine.connect() as connection:
                assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
                assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "delete"
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0012_phase9_archive_import"

        now = datetime.now(timezone.utc)
        chat = Chat(str(uuid4()), "Test", now, now)
        store.create_chat(chat)
        persisted = store.list_chats()[0]
        assert persisted.id == chat.id
        assert persisted.title == chat.title
        assert persisted.created_at == parse_utc(utc_iso(chat.created_at))
        assert persisted.updated_at == parse_utc(utc_iso(chat.updated_at))
    finally:
        store.close()
